//! Reuse a function across source revisions after checking the Rust MIR its producer saw.
//! Function requests, source spans and variables are rebound to the database doing the build.

mod proof;

use std::sync::LazyLock;

use cranelift_codegen::ir::Signature;
use hir_def::{FunctionId, HasModule};
use hir_ty::{
    ParamEnvAndCrate,
    db::HirDatabase,
    mir::{MirBody, TerminatorKind},
    next_solver::GenericArgs,
};
use serde_derive::{Deserialize, Serialize};
use sha2::{Digest, Sha256};

use super::{
    Backend, CompiledFunction, ConstBlob, DataIdentity, DataRequest, FuncRequest, FuncTarget,
    Instance, InstanceDef, MirSpan, Produced, Requests, Result, Variable, backend,
    cached::CachedCode, panics_abort, store::Store,
};
use proof::{Policy, Proof};

/// Only functions with a bounded, verified set of annotated workspace callees can expose
/// their bodies to an optimizing LLVM edit worker. Other functions keep ordinary calls.
pub(super) fn edit_inlining() -> bool {
    backend::edits_follow()
        && backend::optimizing()
        && super::tier::requested_across() != Some(false)
}

/// Distinguishes the inputs offered to per-function LLVM in edit and whole-program builds.
pub(super) fn llvm_mode() -> [u8; 3] {
    #[cfg(feature = "llvm")]
    let imports = super::imports::enabled();
    #[cfg(not(feature = "llvm"))]
    let imports = false;
    [edit_inlining() as u8, super::tier::across() as u8, imports as u8]
}

/// The source functions an edit compile can ask LLVM to inline, including calls made by
/// an inline body. The same rule applies while building a body for another function.
fn edit_bodies(proof: &Proof) -> Vec<FunctionId> {
    proof.bodies.iter().map(|&index| proof.callees[index as usize]).collect()
}

/// Looking for inline callees in a leaf needs no proof of its source or literal tables.
fn has_calls(body: &MirBody<'_>) -> bool {
    body.basic_blocks.iter().any(|(_, block)| {
        block.terminator.as_ref().is_some_and(|t| matches!(t.kind, TerminatorKind::Call { .. }))
    })
}

/// Applies the edit worker's body proof while building a small callee for LLVM.
pub(super) fn nested_edit_bodies<'db>(
    db: &'db dyn HirDatabase,
    func: FunctionId,
    body: &MirBody<'db>,
    env: ParamEnvAndCrate<'db>,
    checked: bool,
) -> Vec<FunctionId> {
    if edit_inlining() && func.krate(db).data(db).origin.is_local() && has_calls(body) {
        return proof::prove(db, func, body, env, checked, Policy::LlvmAnnotated)
            .as_ref()
            .map(edit_bodies)
            .unwrap_or_default();
    }
    Vec::new()
}

/// Run the usual lowering unless a record for these validated inputs can be rebound in full.
pub(super) fn compile<'db>(
    db: &'db dyn HirDatabase,
    func: FunctionId,
    body: &MirBody<'db>,
    env: ParamEnvAndCrate<'db>,
    checked: bool,
    lower: impl FnOnce(Vec<FunctionId>) -> Result<Produced>,
) -> Result<Produced> {
    static TRACE: LazyLock<bool> =
        LazyLock::new(|| std::env::var_os("RA_JIT_TRACE_FUNCTION_CACHE").is_some());
    let Ok(producer) = Backend::from_env() else { return lower(Vec::new()) };
    if !func.krate(db).data(db).origin.is_local() {
        return lower(Vec::new());
    }
    #[cfg(feature = "llvm")]
    if super::units::enabled() {
        return lower(Vec::new());
    }
    let policy = match producer {
        Backend::Cranelift => Policy::Cranelift,
        Backend::Llvm(_) if edit_inlining() => Policy::LlvmAnnotated,
        Backend::Llvm(_) if super::tier::across() => Policy::LlvmAcross,
        Backend::Llvm(_) => Policy::LlvmCalls,
    };
    let store = Store::global();
    if store.is_none() && !*TRACE && policy != Policy::LlvmAnnotated {
        return lower(Vec::new());
    }
    let trace = |action| {
        if *TRACE {
            let name = super::backtrace_name(db, &super::root_instance(db, func));
            eprintln!("function cache: {action} {name}");
        }
    };
    static CHECKING: LazyLock<bool> = LazyLock::new(|| {
        [
            "RA_JIT_CACHE_CHECK",
            "RA_JIT_CHECK_ALL",
            "RA_JIT_LAYOUTS",
            "RA_JIT_DUMP_CLIF",
            "RA_JIT_DUMP_LLVM",
            "RA_JIT_DUMP_TIER",
        ]
        .iter()
        .any(|name| std::env::var_os(name).is_some())
    });
    let store = store.filter(|_| !*CHECKING);
    let proof = if store.is_some() || (policy == Policy::LlvmAnnotated && has_calls(body)) {
        proof::prove(db, func, body, env, checked, policy)
    } else {
        None
    };
    let candidate = store.zip(proof.as_ref()).and_then(|(store, proof)| {
        let target = super::selected_target().ok().filter(|it| !it.is_wasm())?;
        let mut hash = Sha256::new();
        hash.update(b"blaze MIR fragment 2");
        hash.update(producer.to_string());
        if matches!(producer, Backend::Llvm(_)) {
            hash.update(llvm_mode());
            hash.update([backend::whole_program() as u8]);
        }
        hash.update(proof.digest);
        hash.update([
            checked as u8,
            panics_abort(db, env.krate) as u8,
            super::mir_opt_level().number(),
            *super::PROMOTE as u8,
            backend::frame_pointers() as u8,
            backend::sections_of_their_own() as u8,
        ]);
        hash.update(super::regalloc().as_bytes());
        hash.update([0]);
        hash.update(target.triple().as_bytes());
        Some((store, proof, hash.finalize().into()))
    });
    if let Some((store, proof, key)) = candidate
        && let Some(bytes) = store.lookup(&key)
        && let Ok(record) = postcard::from_bytes::<Record>(&bytes)
        && let Some(compiled) = record.restore(proof, func, body, env, checked)
    {
        trace("reused");
        return Ok(Produced::Code(compiled, Some(producer)));
    }
    let shown = proof.as_ref().filter(|_| policy == Policy::LlvmAnnotated).map(edit_bodies);
    let output = lower(shown.unwrap_or_default())?;
    trace("lowered");
    if let Some((store, proof, key)) = candidate
        && let Produced::Code(compiled, Some(actual)) = &output
        && *actual == producer
        && let Some(record) = Record::new(proof, func, body, env, checked, compiled)
        && let Ok(bytes) = postcard::to_allocvec(&record)
    {
        store.insert(&key, &bytes);
    }
    Ok(output)
}

#[derive(Serialize, Deserialize)]
struct Record {
    code: CachedCode,
    /// In relocation order, with the index of the call target in the validated MIR.
    calls: Vec<(u32, Signature)>,
    /// Validated bodies shown to LLVM. Their code can matter even after a call disappears.
    inlined: Vec<u32>,
    /// Read-only literals in dependency order. Their pointers name earlier entries.
    literals: Vec<ConstBlob>,
    /// Indices into the current body's source locations.
    spans: Vec<u32>,
    /// The MIR local held by each named stack slot.
    variables: Vec<(u32, u32)>,
}

impl Record {
    /// Each direct call must be represented by a rebindable request or a validated body.
    fn new(
        proof: &Proof,
        func: FunctionId,
        body: &MirBody<'_>,
        env: ParamEnvAndCrate<'_>,
        checked: bool,
        compiled: &CompiledFunction,
    ) -> Option<Self> {
        let mut inlined = Vec::new();
        for instance in &compiled.requests.inlined {
            let InstanceDef::Fn(func) = instance.def else { return None };
            let i = proof.callees.iter().position(|&id| id == func)? as u32;
            if !instance.args.as_ref().is_empty()
                || instance.checked != checked
                || !proof.bodies.contains(&i)
            {
                return None;
            }
            inlined.push(i);
        }
        let mut literals = Vec::new();
        for data in &compiled.requests.datas {
            let DataRequest::Blob { blob, identity: DataIdentity::Shared } = data else {
                return None;
            };
            if !literal_is_sound(blob, &literals) {
                return None;
            }
            literals.push(blob.clone());
        }
        let mut calls = Vec::new();
        let mut called = vec![false; proof.callees.len()];
        for request in &compiled.requests.funcs {
            let FuncTarget::Instance(instance, from) = &request.target else { return None };
            let InstanceDef::Fn(callee) = instance.def else { return None };
            if !instance.args.as_ref().is_empty()
                || instance.checked != checked
                || *from != env.store()
            {
                return None;
            }
            let index = proof.callees.iter().position(|&id| id == callee)?;
            called[index] = true;
            calls.push((index as u32, request.sig.clone()));
        }
        if (0..proof.direct).any(|i| !called[i] && !proof.bodies.contains(&(i as u32))) {
            return None;
        }
        let (owner, spans) = compiled.source.as_ref()?;
        if *owner != func.into() || compiled.closure.is_some() || !compiled.layouts.is_empty() {
            return None;
        }
        let places: super::FxHashMap<_, _> =
            places(body).enumerate().map(|(i, span)| (span, i as u32)).collect();
        let spans = spans.iter().map(|s| places.get(s).copied()).collect::<Option<_>>()?;
        let mut variables = Vec::new();
        if let Some((from, vars)) = &compiled.variables {
            if *from != env.store() {
                return None;
            }
            let bindings = body.local_to_binding_map();
            for var in vars {
                let local = *body.binding_locals.get(var.binding)?;
                if bindings.get(local) != Some(&var.binding)
                    || var.ty != body.locals[local].ty
                    || var.param != body.param_locals.contains(&local)
                    || var.pointer.is_some()
                {
                    return None;
                }
                variables.push((local.into_raw().into_u32(), var.slot));
            }
        }
        Some(Self {
            code: CachedCode::from(&compiled.code),
            calls,
            inlined,
            literals,
            spans,
            variables,
        })
    }

    /// Resolve relocations and debugger references against the newly checked body.
    fn restore(
        self,
        proof: &Proof,
        func: FunctionId,
        body: &MirBody<'_>,
        env: ParamEnvAndCrate<'_>,
        checked: bool,
    ) -> Option<CompiledFunction> {
        if !self
            .literals
            .iter()
            .enumerate()
            .all(|(i, blob)| literal_is_sound(blob, &self.literals[..i]))
            || !self.code.sound(|ns| match ns {
                0 => self.calls.len(),
                1 => self.literals.len(),
                _ => 0,
            })
        {
            return None;
        }
        let datas = self
            .literals
            .into_iter()
            .map(|blob| DataRequest::Blob { blob, identity: DataIdentity::Shared })
            .collect();
        let mut requests = Requests { datas, ..Requests::default() };
        let instance = |index: u32| {
            let def = InstanceDef::Fn(*proof.callees.get(index as usize)?);
            Some(Instance { def, args: GenericArgs::new_from_slice(&[]).store(), checked })
        };
        for index in self.inlined {
            if !proof.bodies.contains(&index) {
                return None;
            }
            requests.inlined.push(instance(index)?);
        }
        for (index, sig) in self.calls {
            let instance = instance(index)?;
            requests
                .funcs
                .push(FuncRequest { target: FuncTarget::Instance(instance, env.store()), sig });
        }
        let places: Vec<_> = places(body).collect();
        let spans =
            self.spans.iter().map(|&i| places.get(i as usize).copied()).collect::<Option<_>>()?;
        let bindings = body.local_to_binding_map();
        let mut vars = Vec::new();
        for (local, slot) in self.variables {
            let local = super::LocalId::from_raw(la_arena::RawIdx::from_u32(local));
            let binding = *bindings.get(local)?;
            vars.push(Variable {
                binding,
                slot,
                param: body.param_locals.contains(&local),
                ty: body.locals[local].ty.clone(),
                pointer: None,
            });
        }
        Some(CompiledFunction {
            code: self.code.code(),
            requests,
            source: Some((func.into(), spans)),
            closure: None,
            variables: (!vars.is_empty()).then(|| (env.store(), vars)),
            layouts: Vec::new(),
        })
    }
}

/// A literal can refer to bytes in an earlier shared allocation. Stitching rebinds those
/// indices in order, so no function, type or data address from the producer can escape.
fn literal_is_sound(blob: &ConstBlob, earlier: &[ConstBlob]) -> bool {
    blob.type_ids.is_empty()
        && blob.sound(0, earlier.len())
        && blob.relocs.iter().all(|&(_, target, addend)| {
            usize::try_from(addend)
                .is_ok_and(|offset| offset <= earlier[target.as_u32() as usize].bytes.len())
        })
}

#[test]
fn literal_pointers_stay_in_an_earlier_allocation() {
    let earlier = [ConstBlob { bytes: vec![1, 2], ..ConstBlob::default() }];
    let mut blob = ConstBlob {
        bytes: vec![0; super::word()],
        relocs: vec![(0, super::DataId::from_u32(0), 1)],
        ..ConstBlob::default()
    };
    assert!(literal_is_sound(&blob, &earlier));
    assert!(!literal_is_sound(&blob, &[]));
    for offset in [-1, 3] {
        blob.relocs[0].2 = offset;
        assert!(!literal_is_sound(&blob, &earlier));
    }
}

/// Source locations in MIR order. An entry never persists an expression or binding ID.
fn places<'a>(body: &'a MirBody<'_>) -> impl Iterator<Item = MirSpan> + 'a {
    body.basic_blocks.iter().flat_map(|(_, block)| {
        block.statements.iter().map(|s| s.span).chain(block.terminator.iter().map(|t| t.span))
    })
}
