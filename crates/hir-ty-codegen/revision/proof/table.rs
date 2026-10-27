//! Constructors whose only external data comes from immutable module constants made of literals.

use super::*;
use hir_def::signatures::ConstSignature;
use hir_ty::mir::{BorrowKind, type_name::rustc_type_name};
use std::hash::{Hash, Hasher};

/// Screen out other aggregate functions before asking for layouts or walking any source.
pub(super) fn candidate(body: &MirBody<'_>) -> bool {
    body.param_locals.is_empty()
        && matches!(body.locals[returned_local(body)].ty.as_ref().kind(), TyKind::Adt(..))
        && body.locals.len() <= 32
        && body.basic_blocks.len() <= 8
        && body.basic_blocks.iter().any(|(_, block)| {
            block.statements.iter().any(|stmt| {
                matches!(&stmt.kind, StatementKind::Assign(_, Rvalue::Use(Operand {
                kind: OperandKind::Constant { konst, ty }, ..
            })) if shared_slice(ty.as_ref()).is_some() && matches!(konst.as_ref().kind(),
                ConstKind::Unevaluated(UnevaluatedConst { def, args })
                if matches!(def.0, GeneralConstId::ConstId(_)) && args.is_empty()))
            })
        })
}

pub(super) fn inherent(db: &dyn HirDatabase, container: ItemContainerId) -> bool {
    matches!(container, ItemContainerId::ImplId(id)
        if db.impl_trait(id).is_none() && id.lookup(db).source(db).value.generic_param_list().is_none())
}

fn shared_slice(ty: Ty<'_>) -> Option<Ty<'_>> {
    let TyKind::Ref(_, pointee, Mutability::Not) = ty.kind() else { return None };
    let TyKind::Slice(element) = pointee.kind() else { return None };
    Some(element)
}

impl<'db> Evidence<'_, 'db> {
    /// Includes the native layout of locals, variant fields and pointed-to slice elements.
    /// The ordinals below are local to this traversal. No database IDs reach the digest.
    pub(super) fn table_type(&mut self, ty: Ty<'db>) -> Option<()> {
        let types = self.table_types.as_mut()?;
        if let Some(index) = types.iter().position(|&it| it == ty) {
            self.field(b"known table type");
            self.index(index as u32);
            return Some(());
        }
        if types.len() == 32 {
            return None;
        }
        types.push(ty);
        let db = self.db;
        self.field(b"table type");
        self.field(rustc_type_name(db, ty)?.as_bytes());
        // LayoutData uses field and variant ordinals, byte offsets, target ABI primitives and
        // niche encodings. Its Hash representation is scoped to the compiler build by the store.
        let layout = db.layout_of_ty(ty.store(), self.env.store()).ok()?;
        layout.hash(&mut LayoutHash(&mut self.hash));
        match ty.kind() {
            TyKind::Bool | TyKind::Char | TyKind::Int(_) | TyKind::Uint(_) => {}
            TyKind::Tuple(fields) if fields.len() <= 32 => {
                for field in fields.iter() {
                    self.table_type(field)?;
                }
            }
            TyKind::Ref(..) => self.table_type(shared_slice(ty)?)?,
            TyKind::Adt(def, args) => {
                let interner = DbInterner::new_with(db, self.env.krate);
                if !args.as_slice().iter().all(|arg| arg.ty().is_some())
                    || def.destructor(interner).is_some()
                {
                    return None;
                }
                let mut fields = |variant: VariantId| {
                    let decls = variant.fields(db).fields();
                    let types = db.field_types(variant);
                    if decls.len() > 32 {
                        return None;
                    }
                    for (id, _) in decls.iter() {
                        let field = types.get(id)?;
                        self.table_type(field.ty().instantiate(interner, args).skip_norm_wip())?;
                    }
                    Some(())
                };
                match def.def_id() {
                    AdtId::StructId(id) if Some(id) != interner.lang_items().UnsafeCell => {
                        fields(id.into())?
                    }
                    AdtId::EnumId(id) => {
                        let variants = &id.enum_variants(db).variants;
                        if variants.len() > 32 {
                            return None;
                        }
                        for &(variant, _) in variants.values() {
                            fields(variant.into())?;
                        }
                    }
                    _ => return None,
                }
            }
            _ => return None,
        }
        Some(())
    }

    /// Only whole-local assignments can construct the return value. Each enum write uses
    /// the current variant ordinal and discriminant, independently of its physical layout.
    pub(super) fn table_assignment(&mut self, dst: Ty<'db>, value: &Rvalue) -> Option<()> {
        let db = self.db;
        let interner = DbInterner::new_with(db, self.env.krate);
        match value {
            Rvalue::Use(operand) => {
                self.field(b"use");
                let ty = self.table_operand(operand)?;
                same_type(interner, dst, ty).then_some(())?;
            }
            Rvalue::Aggregate(AggregateKind::Adt(variant, args), operands) => {
                let TyKind::Adt(def, subst) = dst.kind() else { return None };
                if variant.adt_id(db) != def.def_id() || args.as_ref() != subst {
                    return None;
                }
                self.field(b"aggregate");
                if let VariantId::EnumVariantId(id) = variant {
                    self.field(b"variant");
                    self.index(id.index(db).try_into().ok()?);
                    self.field(&db.const_eval_discriminant(*id).ok()?.to_le_bytes());
                }
                let decls = variant.fields(db).fields();
                let fields = db.field_types(*variant);
                if decls.len() != operands.len() {
                    return None;
                }
                for ((id, _), operand) in decls.iter().zip(operands) {
                    let ty = fields.get(id)?.ty().instantiate(interner, subst).skip_norm_wip();
                    let actual = self.table_operand(operand)?;
                    same_type(interner, ty, actual).then_some(())?;
                }
            }
            // Lifetime elision in a module const can insert a shared reborrow of its slice.
            Rvalue::Ref(BorrowKind::Shared, place)
                if matches!(place.projection.as_slice(), [ProjectionElem::Deref])
                    && shared_slice(dst).is_some()
                    && same_type(interner, dst, self.body.locals[place.local].ty.as_ref()) =>
            {
                self.field(b"reborrow slice");
                self.index(place.local.into_raw().into_u32());
            }
            _ => return None,
        }
        Some(())
    }

    fn table_operand(&mut self, operand: &Operand) -> Option<Ty<'db>> {
        if let OperandKind::Constant { konst, ty } = &operand.kind
            && let ConstKind::Unevaluated(UnevaluatedConst { def, args }) = konst.as_ref().kind()
        {
            let GeneralConstId::ConstId(id) = def.0 else { return None };
            let ty = ty.as_ref();
            let db = self.db;
            let loc = id.lookup(db);
            let ItemContainerId::ModuleId(module) = loc.container else { return None };
            if !args.is_empty()
                || !id.krate(db).data(db).origin.is_local()
                || module.block(db).is_some()
                || !literal_element(shared_slice(ty)?, 0)
            {
                return None;
            }
            let source = loc.source(db);
            let interner = DbInterner::new_with(db, self.env.krate);
            if source.file_id.is_macro()
                || !same_type(
                    interner,
                    ty,
                    db.type_for_const(id).instantiate(interner, args).skip_norm_wip(),
                )
            {
                return None;
            }
            let value = source.value.body()?;
            if !value.syntax().descendants().all(literal_node) {
                return None;
            }
            self.operand_span(operand);
            self.field(b"module literal table");
            let krate = id.krate(db).extra_data(db);
            self.field(krate.display_name.as_ref()?.crate_name().as_str().as_bytes());
            self.field(krate.version.as_deref().unwrap_or_default().as_bytes());
            for at in std::iter::successors(Some(module), |at| at.containing_module(db)) {
                if let Some(name) = at.name(db) {
                    self.field(name.as_str().as_bytes());
                }
            }
            self.field(ConstSignature::of(db, id).name.as_ref()?.as_str().as_bytes());
            self.field(value.syntax().text().to_string().as_bytes());
            return Some(ty);
        }
        self.operand(operand)
    }
}

/// The table cannot reinterpret literal bytes as an ADT or borrow external storage.
fn literal_element(ty: Ty<'_>, depth: usize) -> bool {
    depth < 8
        && match ty.kind() {
            TyKind::Char | TyKind::Bool | TyKind::Int(_) | TyKind::Uint(_) => true,
            TyKind::Tuple(fields) => fields.iter().all(|ty| literal_element(ty, depth + 1)),
            _ => shared_slice(ty).is_some_and(|element| literal_element(element, depth + 1)),
        }
}

/// Checking every syntax node excludes names, macros, conditional elements and repeat counts.
/// The resolved type and the closed initializer determine the value without const evaluation.
fn literal_node(node: SyntaxNode) -> bool {
    match ast::Expr::cast(node) {
        Some(ast::Expr::Literal(literal)) => matches!(
            literal.kind(),
            ast::LiteralKind::Char(_)
                | ast::LiteralKind::Byte(_)
                | ast::LiteralKind::Bool(_)
                | ast::LiteralKind::IntNumber(_)
        ),
        Some(ast::Expr::RefExpr(r)) => r.raw_token().is_none() && r.mut_token().is_none(),
        Some(ast::Expr::ArrayExpr(a)) => a.semicolon_token().is_none(),
        Some(ast::Expr::TupleExpr(_) | ast::Expr::ParenExpr(_)) => true,
        _ => false,
    }
}

fn same_type<'db>(interner: DbInterner<'db>, left: Ty<'db>, right: Ty<'db>) -> bool {
    if left == right {
        return true;
    }
    let types = erase_regions(interner, GenericArgs::new_from_slice(&[left.into(), right.into()]));
    types.as_slice()[0] == types.as_slice()[1]
}

struct LayoutHash<'a>(&'a mut Sha256);

impl Hasher for LayoutHash<'_> {
    fn write(&mut self, bytes: &[u8]) {
        self.0.update(bytes);
    }

    fn finish(&self) -> u64 {
        u64::from_le_bytes(self.0.clone().finalize()[..8].try_into().unwrap())
    }
}

#[test]
fn a_literal_table_has_no_external_inputs() {
    let closed = |text| {
        let expr = ast::Expr::parse(text, syntax::Edition::CURRENT).ok().unwrap();
        expr.syntax().descendants().all(literal_node)
    };
    assert!(closed("&[('K', &['k', 'K']), ('S', &[])]"));
    for source in [
        "&[include!(\"chars.rs\")]",
        "&[crate::OTHER]",
        "&[#[cfg(any())] 'k']",
        "&[0; COUNT]",
        "&[&raw const ADDRESS]",
    ] {
        assert!(!closed(source), "{source}");
    }
}
