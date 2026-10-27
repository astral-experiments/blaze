//! What a plain integer function and its direct calls mean before Cranelift IR is built.

use crate::*;
use hir_ty::mir::type_name::rustc_function_name;
use rustc_type_ir::{IntTy, UintTy};
use sha2::{Digest, Sha256};
use syntax::ast::{HasAttrs, HasGenericParams};

pub(super) struct Proof {
    pub(super) digest: [u8; 32],
    /// Each resolved function once, in the order its calls first occur in MIR.
    pub(super) callees: Vec<FunctionId>,
}

/// Fingerprints only MIR whose operands, types and references can all be named across revisions.
pub(super) fn prove(db: &dyn HirDatabase, func: FunctionId, body: &MirBody<'_>) -> Option<Proof> {
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
    let mut proof = Evidence { hash: Sha256::new(), callees: Vec::new() };
    // Most functions have a pointer or a Rust aggregate. Decline them before reading source.
    for (_, local) in body.locals.iter() {
        proof.field(integer(local.ty.as_ref())?);
    }
    let source = proof.signature(db, func)?;
    // Span and binding indices are local to a written body. Macros and conditional source
    // inside it can change their meaning even when the text of the function stays put.
    let syntax = source.body()?;
    if syntax.syntax().descendants().any(|it| {
        ast::MacroCall::can_cast(it.kind())
            || ast::Attr::can_cast(it.kind())
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
                    proof.place(place)?;
                    proof.rvalue(value)?;
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
            TerminatorKind::Call {
                func,
                args,
                destination,
                target,
                cleanup,
                from_hir_call: true,
            } => {
                proof.field(b"call");
                proof.callee(db, func)?;
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
    Some(Proof { digest: proof.hash.finalize().into(), callees: proof.callees })
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

struct Evidence {
    hash: Sha256,
    callees: Vec<FunctionId>,
}

impl Evidence {
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

    fn signature(&mut self, db: &dyn HirDatabase, func: FunctionId) -> Option<ast::Fn> {
        let loc = func.lookup(db);
        let sig = FunctionSignature::of(db, func);
        if !matches!(loc.container, ItemContainerId::ModuleId(_))
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
        for ty in types.inputs() {
            self.field(integer(*ty)?);
        }
        self.field(b"returns");
        self.field(integer(types.output())?);
        Some(source.value)
    }

    fn callee(&mut self, db: &dyn HirDatabase, operand: &Operand) -> Option<()> {
        self.operand_span(operand);
        let ty = match &operand.kind {
            OperandKind::Constant { ty, .. } => ty.as_ref(),
            OperandKind::Allocation { allocation } => allocation.as_ref().ty,
            _ => return None,
        };
        let TyKind::FnDef(def, args) = ty.kind() else { return None };
        let CallableDefId::FunctionId(func) = def.0 else { return None };
        if !args.is_empty() || AttrFlags::query(db, func.into()).contains(AttrFlags::IS_INLINE) {
            return None;
        }
        let index = match self.callees.iter().position(|&it| it == func) {
            Some(index) => index,
            None => {
                self.signature(db, func)?;
                self.callees.push(func);
                self.callees.len() - 1
            }
        };
        self.index(index.try_into().ok()?);
        Some(())
    }

    fn place(&mut self, place: &StoredPlace) -> Option<()> {
        self.index(place.as_ref().as_local()?.into_raw().into_u32());
        Some(())
    }

    fn operand(&mut self, operand: &Operand) -> Option<()> {
        self.operand_span(operand);
        match &operand.kind {
            OperandKind::Copy(p) => {
                self.field(b"copy");
                self.place(p)?;
            }
            OperandKind::Move(p) => {
                self.field(b"move");
                self.place(p)?;
            }
            OperandKind::Allocation { allocation } => {
                let value = allocation.as_ref();
                if !matches!(value.memory_map, MemoryMap::Empty) {
                    return None;
                }
                self.field(b"bytes");
                self.field(integer(value.ty)?);
                self.field(&value.memory);
            }
            OperandKind::Constant { konst, ty } => {
                let ConstKind::Value(value) = konst.as_ref().kind() else { return None };
                let scalar = value.try_to_leaf()?;
                self.field(b"constant");
                self.field(integer(ty.as_ref())?);
                self.field(integer(value.ty)?);
                self.field(&scalar.size().bytes().to_le_bytes());
                self.field(&scalar.to_bits_unchecked().to_le_bytes());
            }
            _ => return None,
        }
        Some(())
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
                | BinOp::BitXor),
                left,
                right,
            ) => {
                self.field(op.to_string().as_bytes());
                self.operand(left)?;
                self.operand(right)?;
            }
            _ => return None,
        }
        Some(())
    }
}
