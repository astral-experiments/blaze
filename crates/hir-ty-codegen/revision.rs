//! Reuse a Cranelift fragment across source revisions after checking its current Rust MIR.
//! Function requests, source spans and variables are rebound to the database doing the build.

mod proof;

use std::sync::LazyLock;

use cranelift_codegen::ir::Signature;
use hir_def::{FunctionId, HasModule};
use hir_ty::{ParamEnvAndCrate, db::HirDatabase, mir::MirBody, next_solver::GenericArgs};
use serde_derive::{Deserialize, Serialize};
use sha2::{Digest, Sha256};

use super::{
    Backend, CompiledFunction, ConstBlob, DataIdentity, DataRequest, FuncRequest, FuncTarget,
    Instance, InstanceDef, MirSpan, Produced, Requests, Result, Variable, backend,
    cached::CachedCode, panics_abort, store::Store,
};
use proof::Proof;

/// Run the usual lowering unless a record for these validated inputs can be rebound in full.
pub(super) fn compile<'db>(
    db: &'db dyn HirDatabase,
    func: FunctionId,
    body: &MirBody<'db>,
    env: ParamEnvAndCrate<'db>,
    checked: bool,
    lower: impl FnOnce() -> Result<Produced>,
) -> Result<Produced> {
    static TRACE: LazyLock<bool> =
        LazyLock::new(|| std::env::var_os("RA_JIT_TRACE_FUNCTION_CACHE").is_some());
    if Backend::from_env() != Ok(Backend::Cranelift) || !func.krate(db).data(db).origin.is_local() {
        return lower();
    }
    let store = Store::global();
    if store.is_none() && !*TRACE {
        return lower();
    }
    let trace = |action| {
        if *TRACE {
            let name = super::backtrace_name(db, &super::root_instance(db, func));
            eprintln!("function cache: {action} {name}");
        }
    };
    static CHECKING: LazyLock<bool> = LazyLock::new(|| {
        ["RA_JIT_CACHE_CHECK", "RA_JIT_CHECK_ALL", "RA_JIT_LAYOUTS", "RA_JIT_DUMP_CLIF"]
            .iter()
            .any(|name| std::env::var_os(name).is_some())
    });
    let candidate = store.filter(|_| !*CHECKING).and_then(|store| {
        let target = super::selected_target().ok().filter(|it| !it.is_wasm())?;
        let proof = proof::prove(db, func, body)?;
        let mut hash = Sha256::new();
        hash.update(b"blaze cranelift MIR fragment 1");
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
    if let Some((store, proof, key)) = &candidate
        && let Some(bytes) = store.lookup(key)
        && let Ok(record) = postcard::from_bytes::<Record>(&bytes)
        && let Some(compiled) = record.restore(proof, func, body, env, checked)
    {
        trace("reused");
        return Ok(Produced::Code(compiled));
    }
    let output = lower()?;
    trace("lowered");
    if let Some((store, proof, key)) = candidate
        && let Produced::Code(compiled) = &output
        && let Some(record) = Record::new(&proof, func, body, env, checked, compiled)
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
    /// Rust MIR can put integer literals in read-only data. These contain no pointers.
    literals: Vec<ConstBlob>,
    /// Indices into the current body's source locations.
    spans: Vec<u32>,
    /// The MIR local held by each named stack slot.
    variables: Vec<(u32, u32)>,
}

impl Record {
    /// Keep the machine code only when each MIR call has an out-of-line target to rebind and
    /// every other request can be carried into a fresh database.
    fn new(
        proof: &Proof,
        func: FunctionId,
        body: &MirBody<'_>,
        env: ParamEnvAndCrate<'_>,
        checked: bool,
        compiled: &CompiledFunction,
    ) -> Option<Self> {
        if !compiled.requests.inlined.is_empty() {
            return None;
        }
        let mut literals = Vec::new();
        for data in &compiled.requests.datas {
            let DataRequest::Blob { blob, identity: DataIdentity::Shared } = data else {
                return None;
            };
            if !blob.type_ids.is_empty() || !blob.sound(0, 0) {
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
        if called.contains(&false) {
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
        Some(Self { code: CachedCode::from(&compiled.code), calls, literals, spans, variables })
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
        if !self.literals.iter().all(|blob| blob.sound(0, 0))
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
        for (index, sig) in self.calls {
            let def = InstanceDef::Fn(*proof.callees.get(index as usize)?);
            let instance =
                Instance { def, args: GenericArgs::new_from_slice(&[]).store(), checked };
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

/// Source locations in MIR order. An entry never persists an expression or binding ID.
fn places<'a>(body: &'a MirBody<'_>) -> impl Iterator<Item = MirSpan> + 'a {
    body.basic_blocks.iter().flat_map(|(_, block)| {
        block.statements.iter().map(|s| s.span).chain(block.terminator.iter().map(|t| t.span))
    })
}
