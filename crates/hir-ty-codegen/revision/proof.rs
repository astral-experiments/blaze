//! Stable evidence for the MIR and dependencies a function can use before building native IR.

mod table;

use crate::*;
use hir_ty::mir::{SwitchTargets, type_name::rustc_function_name};
use rustc_type_ir::{IntTy, UintTy};
use sha2::{Digest, Sha256};
use syntax::ast::{HasAttrs, HasGenericParams};

pub(super) struct Proof {
    pub(super) digest: [u8; 32],
    /// The root's direct calls first, followed by calls found in the bodies checked for LLVM.
    pub(super) callees: Vec<FunctionId>,
    pub(super) direct: usize,
    /// Indices of callees whose full bodies contribute to the digest.
    pub(super) bodies: Vec<u32>,
}

/// Which bodies the regular, per-function compile may show its producer.
#[derive(Clone, Copy, PartialEq, Eq)]
pub(super) enum Policy {
    Cranelift,
    LlvmCalls,
    LlvmAnnotated,
    LlvmAcross,
}

impl Policy {
    fn includes(self, flags: AttrFlags) -> bool {
        !flags.contains(AttrFlags::INLINE_NEVER)
            && (self == Self::LlvmAcross
                || self == Self::LlvmAnnotated && flags.contains(AttrFlags::IS_INLINE))
    }
}

/// Fingerprints the root and each body the current LLVM inlining policy may incorporate.
pub(super) fn prove<'db>(
    db: &'db dyn HirDatabase,
    func: FunctionId,
    body: &MirBody<'db>,
    env: ParamEnvAndCrate<'db>,
    checked: bool,
    policy: Policy,
) -> Option<Proof> {
    let (digest, callees) = prove_body(db, func, body, env, policy)?;
    let direct = callees.len();
    let proof = Proof { digest, callees, direct, bodies: Vec::new() };
    if direct == 0 || matches!(policy, Policy::Cranelift | Policy::LlvmCalls) {
        return Some(proof);
    }
    if direct > Dependencies::MAX_CALLEES {
        return None;
    }
    let mut deps =
        Dependencies { db, env, checked, policy, proof, path: vec![func], hash: Sha256::new() };
    deps.hash.update(digest);
    for index in 0..direct {
        deps.visit(index)?;
    }
    deps.proof.digest = deps.hash.finalize().into();
    Some(deps.proof)
}

struct Dependencies<'db> {
    db: &'db dyn HirDatabase,
    env: ParamEnvAndCrate<'db>,
    checked: bool,
    policy: Policy,
    proof: Proof,
    path: Vec<FunctionId>,
    hash: Sha256,
}

impl Dependencies<'_> {
    // Decline large dependency graphs rather than making a cache lookup compile the program.
    const MAX_CALLEES: usize = 32;
    const MAX_DEPTH: usize = 8;

    fn visit(&mut self, index: usize) -> Option<()> {
        let func = self.proof.callees[index];
        if !self.policy.includes(AttrFlags::query(self.db, func.into())) {
            return Some(());
        }
        if self.path.contains(&func) || self.path.len() > Self::MAX_DEPTH {
            return None;
        }
        if self.proof.bodies.contains(&(index as u32)) {
            return Some(());
        }
        let instance = Instance {
            def: InstanceDef::Fn(func),
            args: GenericArgs::new_from_slice(&[]).store(),
            checked: self.checked,
        };
        // Inlining builds an instance in its caller's environment and overflow mode.
        let body = instance_body(self.db, &instance, self.env.store()).ok()?;
        let (digest, callees) = prove_body(self.db, func, &body, self.env, self.policy)?;
        self.hash.update((index as u32).to_le_bytes());
        self.hash.update(digest);
        self.hash.update((callees.len() as u32).to_le_bytes());
        self.path.push(func);
        for callee in callees {
            let callees = &mut self.proof.callees;
            let child = match callees.iter().position(|&it| it == callee) {
                Some(child) => child,
                None if callees.len() == Self::MAX_CALLEES => return None,
                None => {
                    callees.push(callee);
                    callees.len() - 1
                }
            };
            self.hash.update((child as u32).to_le_bytes());
            self.visit(child)?;
        }
        self.path.pop();
        self.proof.bodies.push(index as u32);
        Some(())
    }
}

/// Fingerprints only MIR whose operands, types and references can all be named across revisions.
fn prove_body<'db>(
    db: &'db dyn HirDatabase,
    func: FunctionId,
    body: &MirBody<'db>,
    env: ParamEnvAndCrate<'db>,
    policy: Policy,
) -> Option<([u8; 32], Vec<FunctionId>)> {
    if !matches!(body.owner, InferBodyId::DefWithBodyId(DefWithBodyId::FunctionId(it)) if it == func)
        || body.coroutine.is_some()
        || !body.closures.is_empty()
        || !body.upvar_locals.is_empty()
        || !body.upvars_in_place.is_empty()
        || !body.promoted_locals.is_empty()
        || !body.promoted_constants.is_empty()
        || !body.panic_unwinds.is_empty()
        || body.inherits_overflow_checks
        || body.phase != MirPhase::Runtime
    {
        return None;
    }
    let mut proof = Evidence {
        db,
        body,
        env,
        hash: Sha256::new(),
        callees: Vec::new(),
        policy,
        receiver: None,
        table_types: table::candidate(body).then(Vec::new),
    };
    // Most functions have a pointer or a Rust aggregate. Decline them before reading source.
    for (id, local) in body.locals.iter() {
        if proof.table_types.is_some() {
            proof.table_type(local.ty.as_ref())?;
        } else if let Some(ty) = integer(local.ty.as_ref()) {
            proof.field(ty);
        } else {
            proof.receiver = Some(proof.shared_receiver(func, id, local.ty.as_ref())?);
        }
    }
    let source = proof.signature(func)?;
    // Span and binding indices are local to a written body. A table constructor also records
    // the resolved layout, variant and literal of every active assignment under `#[cfg]`.
    let syntax = source.body()?;
    if syntax.syntax().descendants().any(|it| {
        ast::MacroCall::can_cast(it.kind())
            || ast::Attr::cast(it.clone()).is_some_and(|attr| {
                proof.table_types.is_none()
                    || !attr.simple_name().is_some_and(|it| it.as_str() == "cfg")
            })
            || ast::Item::can_cast(it.kind())
    }) {
        return None;
    }
    proof.field(source.syntax().text().to_string().as_bytes());
    proof.field(b"params");
    for id in &body.param_locals {
        proof.index(id.into_raw().into_u32());
    }
    proof.field(b"bindings");
    for (binding, local) in body.binding_locals.iter() {
        proof.index(binding.into_raw().into_u32());
        proof.index(local.into_raw().into_u32());
    }
    proof.field(b"scope ends");
    for id in &body.scope_ends {
        proof.index(id.into_raw().into_u32());
    }
    proof.field(b"unreached");
    for id in &body.unreached {
        proof.index(id.into_raw().into_u32());
    }
    proof.field(b"start");
    proof.index(body.start_block.into_raw().into_u32());
    for (_, block) in body.basic_blocks.iter() {
        proof.field(if block.is_cleanup { b"cleanup" } else { b"block" });
        for stmt in &block.statements {
            proof.span(stmt.span);
            match &stmt.kind {
                StatementKind::Assign(place, value) => {
                    proof.field(b"assign");
                    let local = proof.place(place)?;
                    if proof.table_types.is_some() {
                        proof.table_assignment(body.locals[local].ty.as_ref(), value)?;
                    } else {
                        proof.rvalue(value)?;
                    }
                }
                StatementKind::StorageLive(local) => {
                    proof.field(b"live");
                    proof.index(local.into_raw().into_u32());
                }
                StatementKind::StorageDead(local) => {
                    proof.field(b"dead");
                    proof.index(local.into_raw().into_u32());
                }
                StatementKind::FakeRead(place) => {
                    proof.field(b"read");
                    proof.place(place)?;
                }
                StatementKind::Nop => proof.field(b"nop"),
                _ => return None,
            }
        }
        let term = block.terminator.as_ref()?;
        proof.span(term.span);
        match &term.kind {
            TerminatorKind::Goto { target } => {
                proof.field(b"goto");
                proof.index(target.into_raw().into_u32());
            }
            TerminatorKind::SwitchInt { discr, targets } if proof.table_types.is_none() => {
                proof.field(b"switch");
                let ty = proof.operand(discr)?;
                if ty.is_unit() {
                    return None;
                }
                proof.field(integer(ty)?);
                hash_switch_targets(&mut proof.hash, targets);
            }
            TerminatorKind::Call {
                func,
                args,
                destination,
                target,
                cleanup,
                from_hir_call: true,
            } if proof.receiver.is_none() && proof.table_types.is_none() => {
                proof.field(b"call");
                proof.callee(func)?;
                for arg in args {
                    proof.operand(arg)?;
                }
                proof.field(b"destination");
                proof.place(destination)?;
                proof.index((*target)?.into_raw().into_u32());
                if let Some(block) = cleanup {
                    proof.field(b"unwind to");
                    proof.index(block.into_raw().into_u32());
                }
            }
            TerminatorKind::Return => proof.field(b"return"),
            TerminatorKind::UnwindResume => proof.field(b"resume"),
            TerminatorKind::Unreachable => proof.field(b"unreachable"),
            _ => return None,
        }
    }
    if proof.receiver.is_some_and(|receiver| !receiver.read) {
        return None;
    }
    Some((proof.hash.finalize().into(), proof.callees))
}

/// The primitive Rust types whose representation is fixed by the target. Signedness matters
/// even where the native call passes two integer types in the same register.
fn integer(ty: Ty<'_>) -> Option<&'static [u8]> {
    Some(match ty.kind() {
        TyKind::Int(ty) if ty != IntTy::I128 => ty.name_str().as_bytes(),
        TyKind::Uint(ty) if ty != UintTy::U128 => ty.name_str().as_bytes(),
        TyKind::Bool => b"bool",
        TyKind::Tuple(types) if types.is_empty() => b"()",
        _ => return None,
    })
}

/// Each MIR case is a full-width value paired with a block. The final block is the default.
fn hash_switch_targets(hash: &mut Sha256, targets: &SwitchTargets) {
    hash.update((targets.all_targets().len() as u64).to_le_bytes());
    for (value, block) in targets.iter() {
        hash.update(value.to_le_bytes());
        hash.update(block.into_raw().into_u32().to_le_bytes());
    }
    hash.update(targets.otherwise().into_raw().into_u32().to_le_bytes());
}

struct Evidence<'a, 'db> {
    db: &'db dyn HirDatabase,
    body: &'a MirBody<'db>,
    env: ParamEnvAndCrate<'db>,
    hash: Sha256,
    callees: Vec<FunctionId>,
    policy: Policy,
    receiver: Option<SharedReceiver<'db>>,
    table_types: Option<Vec<Ty<'db>>>,
}

struct SharedReceiver<'db> {
    local: LocalId,
    pointee: Ty<'db>,
    fields: Vec<Ty<'db>>,
    read: bool,
}

impl<'db> Evidence<'_, 'db> {
    fn field(&mut self, bytes: &[u8]) {
        self.hash.update((bytes.len() as u64).to_le_bytes());
        self.hash.update(bytes);
    }

    fn index(&mut self, id: u32) {
        self.field(&id.to_le_bytes());
    }

    fn span(&mut self, span: MirSpan) {
        let (kind, id): (&[u8], _) = match span {
            MirSpan::ExprId(id) => (b"expr", id.into_raw().into_u32()),
            MirSpan::ExprEnd(id) => (b"end", id.into_raw().into_u32()),
            MirSpan::PatId(id) => (b"pat", id.into_raw().into_u32()),
            MirSpan::BindingId(id) => (b"binding", id.into_raw().into_u32()),
            MirSpan::Unknown => return self.field(b"unknown"),
            MirSpan::SelfParam => return self.field(b"self"),
        };
        self.field(kind);
        self.index(id);
    }

    /// A shared receiver made entirely of primitive fields. LLVM's pointee attributes
    /// depend on its size, alignment and `Freeze`, even if only one field is read.
    fn shared_receiver(
        &mut self,
        func: FunctionId,
        local: LocalId,
        ty: Ty<'db>,
    ) -> Option<SharedReceiver<'db>> {
        if self.body.param_locals.first() != Some(&local) {
            return None;
        }
        let TyKind::Ref(_, pointee, Mutability::Not) = ty.kind() else { return None };
        let TyKind::Adt(def, args) = pointee.kind() else { return None };
        let AdtId::StructId(id) = def.def_id() else { return None };
        let db = self.db;
        let ItemContainerId::ImplId(impl_) = func.lookup(db).container else { return None };
        if !args.is_empty()
            || self
                .body
                .locals
                .iter()
                .any(|(id, other)| id != local && integer(other.ty.as_ref()).is_none())
            || !FunctionSignature::of(db, func).has_self_param()
            || db.impl_trait(impl_).is_some()
            || !id.krate(db).data(db).origin.is_local()
        {
            return None;
        }
        let interner = DbInterner::new_with(db, self.env.krate);
        let decls = id.fields(db).fields();
        let types = db.field_types(id.into());
        let mut fields = Vec::with_capacity(decls.len());
        self.field(b"shared scalar fields");
        for (decl, _) in decls.iter() {
            let ty = types.get(decl)?.ty().instantiate(interner, args).skip_norm_wip();
            if ty.is_unit() {
                return None;
            }
            self.field(integer(ty)?);
            fields.push(ty);
        }
        if impl_.lookup(db).source(db).value.generic_param_list().is_some()
            || db.impl_self_ty(impl_).instantiate(interner, args).skip_norm_wip() != pointee
            || !hir_ty::drop::is_freeze(db, interner, pointee)
        {
            return None;
        }
        let layout = db.layout_of_ty(pointee.store(), self.env.store()).ok()?;
        if !matches!(layout.variants, Variants::Single { .. })
            || layout.fields.count() != fields.len()
        {
            return None;
        }
        self.field(&layout.size.bytes().to_le_bytes());
        self.field(&layout.align.abi.bytes().to_le_bytes());
        for index in 0..fields.len() {
            self.field(&layout.fields.offset(index).bytes().to_le_bytes());
        }
        Some(SharedReceiver { local, pointee, fields, read: false })
    }

    fn signature(&mut self, func: FunctionId) -> Option<ast::Fn> {
        let db = self.db;
        let loc = func.lookup(db);
        let sig = FunctionSignature::of(db, func);
        if self.receiver.is_none()
            && !matches!(loc.container, ItemContainerId::ModuleId(_))
            && !(self.table_types.is_some() && table::inherent(db, loc.container))
            || !func.krate(db).data(db).origin.is_local()
            || sig.abi != ExternAbi::Rust
            || sig.is_varargs()
            || sig.is_async()
            || sig.is_gen()
            || !sig.has_body()
            || track_caller(db, func)
            || FunctionSignature::is_intrinsic(db, func)
            || fncache::is_code(db, func)
        {
            return None;
        }
        let source = loc.source(db);
        if source.file_id.is_macro()
            || source.value.generic_param_list().is_some()
            || source
                .value
                .syntax()
                .ancestors()
                .skip(1)
                .any(|it| ast::BlockExpr::can_cast(it.kind()))
            || source.value.attrs().any(|it| {
                !it.simple_name().is_some_and(|name| {
                    matches!(
                        name.as_str(),
                        "inline"
                            | "export_name"
                            | "no_mangle"
                            | "allow"
                            | "warn"
                            | "deny"
                            | "cfg"
                            | "doc"
                    )
                })
            })
        {
            return None;
        }
        let types = db.callable_item_signature(func.into()).skip_binder().skip_binder();
        self.field(rustc_function_name(db, func, &[])?.as_bytes());
        if self.table_types.is_some() {
            return (types.inputs().is_empty()
                && types.output() == self.body.locals[returned_local(self.body)].ty.as_ref())
            .then_some(source.value);
        }
        let inputs = if let Some(receiver) = &self.receiver {
            let (arg, rest) = types.inputs().split_first()?;
            if !matches!(arg.kind(), TyKind::Ref(_, ty, Mutability::Not) if ty == receiver.pointee)
            {
                return None;
            }
            rest
        } else {
            types.inputs()
        };
        for ty in inputs {
            self.field(integer(*ty)?);
        }
        self.field(b"returns");
        self.field(integer(types.output())?);
        Some(source.value)
    }

    fn callee(&mut self, operand: &Operand) -> Option<()> {
        let db = self.db;
        self.operand_span(operand);
        let ty = match &operand.kind {
            OperandKind::Constant { ty, .. } => ty.as_ref(),
            OperandKind::Allocation { allocation } => allocation.as_ref().ty,
            _ => return None,
        };
        let TyKind::FnDef(def, args) = ty.kind() else { return None };
        let CallableDefId::FunctionId(func) = def.0 else { return None };
        if !args.is_empty() {
            return None;
        }
        let index = match self.callees.iter().position(|&it| it == func) {
            Some(index) => index,
            None => {
                let flags = AttrFlags::query(db, func.into());
                if self.policy == Policy::Cranelift {
                    if flags.contains(AttrFlags::IS_INLINE) {
                        return None;
                    }
                } else {
                    self.field(&[
                        flags.contains(AttrFlags::INLINE_NEVER) as u8,
                        flags.contains(AttrFlags::INLINE_ALWAYS) as u8,
                        flags.contains(AttrFlags::IS_INLINE) as u8,
                    ]);
                }
                self.signature(func)?;
                self.callees.push(func);
                self.callees.len() - 1
            }
        };
        self.index(index.try_into().ok()?);
        Some(())
    }

    fn place(&mut self, place: &StoredPlace) -> Option<LocalId> {
        let local = place.as_ref().as_local()?;
        if self.receiver.as_ref().is_some_and(|receiver| receiver.local == local) {
            return None;
        }
        self.index(local.into_raw().into_u32());
        Some(local)
    }

    fn operand(&mut self, operand: &Operand) -> Option<Ty<'db>> {
        self.operand_span(operand);
        Some(match &operand.kind {
            OperandKind::Copy(p) => {
                self.field(b"copy");
                if let [ProjectionElem::Deref, ProjectionElem::Field(field)] =
                    p.projection.as_slice()
                {
                    let receiver = self.receiver.as_mut()?;
                    if p.local != receiver.local {
                        return None;
                    }
                    let ty = *receiver.fields.get(field.0 as usize)?;
                    receiver.read = true;
                    self.field(b"receiver field");
                    self.index(field.0);
                    ty
                } else {
                    let local = self.place(p)?;
                    self.body.locals[local].ty.as_ref()
                }
            }
            OperandKind::Move(p) => {
                self.field(b"move");
                let local = self.place(p)?;
                self.body.locals[local].ty.as_ref()
            }
            OperandKind::Allocation { allocation } => {
                let value = allocation.as_ref();
                if !matches!(value.memory_map, MemoryMap::Empty) {
                    return None;
                }
                self.field(b"bytes");
                self.field(integer(value.ty)?);
                self.field(&value.memory);
                value.ty
            }
            OperandKind::Constant { konst, ty } => {
                let ty = ty.as_ref();
                self.field(b"constant");
                self.field(integer(ty)?);
                match konst.as_ref().kind() {
                    ConstKind::Value(value) => {
                        let scalar = value.try_to_leaf()?;
                        self.field(integer(value.ty)?);
                        self.field(&scalar.size().bytes().to_le_bytes());
                        self.field(&scalar.to_bits_unchecked().to_le_bytes());
                    }
                    ConstKind::Unevaluated(UnevaluatedConst { def, args }) => {
                        // A module constant in a match can change without editing its consumer.
                        let GeneralConstId::ConstId(id) = def.0 else { return None };
                        if !args.is_empty()
                            || ty.is_unit()
                            || !id.krate(self.db).data(self.db).origin.is_local()
                            || !matches!(id.lookup(self.db).container, ItemContainerId::ModuleId(_))
                        {
                            return None;
                        }
                        let value = self.db.const_eval(id, args, Some(self.env)).ok()?;
                        if value.ty != ty || !matches!(value.memory_map, MemoryMap::Empty) {
                            return None;
                        }
                        self.field(b"module const");
                        self.field(&value.memory);
                    }
                    _ => return None,
                }
                ty
            }
            _ => return None,
        })
    }

    fn operand_span(&mut self, operand: &Operand) {
        match operand.span() {
            Some(span) => self.span(span),
            None => self.field(b"no span"),
        }
    }

    fn rvalue(&mut self, value: &Rvalue) -> Option<()> {
        match value {
            Rvalue::Use(op) => {
                self.field(b"use");
                self.operand(op)?;
            }
            Rvalue::Cast(CastKind::IntToInt, op, ty) => {
                self.field(b"int cast");
                self.field(integer(ty.as_ref())?);
                self.operand(op)?;
            }
            Rvalue::BinaryOp(
                op @ (BinOp::Add
                | BinOp::Sub
                | BinOp::Mul
                | BinOp::BitAnd
                | BinOp::BitOr
                | BinOp::BitXor
                | BinOp::Eq
                | BinOp::Ne
                | BinOp::Lt
                | BinOp::Le
                | BinOp::Gt
                | BinOp::Ge),
                left,
                right,
            ) => {
                self.field(op.to_string().as_bytes());
                let left = self.operand(left)?;
                let right = self.operand(right)?;
                if left != right || left.is_unit() {
                    return None;
                }
            }
            _ => return None,
        }
        Some(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn switch_cases_and_default_contribute_to_the_proof() {
        let block = |n| BasicBlockId::from_raw(la_arena::RawIdx::from_u32(n));
        let digest = |cases: [(u128, u32); 2], otherwise| {
            let mut hash = Sha256::new();
            let cases = cases.into_iter().map(|(v, target)| (v, block(target)));
            hash_switch_targets(&mut hash, &SwitchTargets::new(cases, block(otherwise)));
            hash.finalize()
        };
        let cases = [(1, 2), (u64::MAX as u128, 3)];
        let original = digest(cases, 4);
        assert_ne!(original, digest([(1, 2), (u32::MAX as u128, 3)], 4));
        assert_ne!(original, digest([(1, 2), (u128::MAX, 3)], 4));
        assert_ne!(original, digest([(1, 3), (u64::MAX as u128, 2)], 4));
        assert_ne!(original, digest(cases, 2));
    }
}
