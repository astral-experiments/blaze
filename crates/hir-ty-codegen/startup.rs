//! Paths from the C entry point to the functions its compiled fragments request.

use std::sync::LazyLock;

use hir_def::{FunctionId, HasModule, StaticId};
use hir_ty::{db::HirDatabase, traits::StoredParamEnvAndCrate};
use indexmap::IndexMap;

use super::{
    Instance, InstanceDef, SymbolResolver, as_its_crate_compiles, backtrace_name,
    compiled_instance, compiled_root, compiled_start, compiled_static,
    fncache::static_name,
    fragment::{DataRequest, FuncTarget, Requests},
    home::Homes,
};

pub(super) static FILTER: LazyLock<Option<String>> =
    LazyLock::new(|| std::env::var("RA_JIT_TRACE_STARTUP").ok());

#[derive(PartialEq, Eq, Hash)]
enum Node {
    Function(Instance, StoredParamEnvAndCrate),
    Static(StaticId),
    Inlined(Instance),
    Symbol(String),
}

impl Node {
    fn name(&self, db: &dyn HirDatabase) -> String {
        match self {
            Self::Function(instance, _) => backtrace_name(db, instance),
            Self::Static(id) => format!("static {}", static_name(db, *id)),
            Self::Inlined(instance) => format!("{} (inlined)", backtrace_name(db, instance)),
            Self::Symbol(name) => name.clone(),
        }
    }

    /// Requests of this baseline fragment. An inlined body's relocations are already in its caller.
    fn requests<'db>(
        &self,
        db: &'db dyn HirDatabase,
        symbols: &dyn SymbolResolver,
    ) -> Option<&'db Requests> {
        let (instance, env) = match self {
            Self::Function(instance, env) => (instance, env),
            Self::Static(id) if !id.krate(db).data(db).origin.is_lang() => {
                return Some(&compiled_static(db, *id).as_ref().ok()?.requests);
            }
            _ => return None,
        };
        let named = match instance.def {
            InstanceDef::Fn(func)
                if instance.args.as_ref().iter().all(|arg| arg.region().is_some()) =>
            {
                Some(func)
            }
            _ => None,
        };
        // A non-generic sysroot function cannot name downstream code. Its caller already
        // records the address or vtable of any callback it supplies to the sysroot.
        if named.is_some_and(|func| func.krate(db).data(db).origin.is_lang()) {
            return None;
        }
        let compiled = match named {
            Some(func)
                if symbols.function(func).is_some()
                    && as_its_crate_compiles(db, symbols, instance) =>
            {
                compiled_root(db, func)
            }
            _ => compiled_instance(db, instance.clone(), env.clone()),
        };
        Some(&compiled.as_ref().ok()?.requests)
    }
}

/// Prints one shortest path from the startup wrapper to each function matching `filter`.
/// Function-address requests include the methods installed in the runtime's callback vtable.
pub(super) fn trace(
    db: &dyn HirDatabase,
    symbols: &dyn SymbolResolver,
    main: FunctionId,
    filter: &str,
) {
    let Ok(start) = compiled_start(db, main) else { return };
    let mut homes = Homes::default();
    let mut paths = IndexMap::<Node, Option<usize>>::new();
    let mut enqueue = |paths: &mut IndexMap<_, _>, parent, requests: &Requests| {
        for request in &requests.funcs {
            let node = match &request.target {
                FuncTarget::Instance(instance, env) => {
                    Node::Function(instance.clone(), homes.home(db, instance, env))
                }
                FuncTarget::Symbol(name) | FuncTarget::Path(name) => Node::Symbol(name.clone()),
                FuncTarget::Asm(_) => continue,
            };
            paths.entry(node).or_insert(parent);
        }
        for request in &requests.datas {
            if let DataRequest::Static(id) = request {
                paths.entry(Node::Static(*id)).or_insert(parent);
            }
        }
        for body in &requests.inlined {
            paths.entry(Node::Inlined(body.clone())).or_insert(parent);
        }
    };
    enqueue(&mut paths, None, &start.requests);
    for at in 0.. {
        let Some((node, parent)) = paths.get_index(at) else { break };
        let name = node.name(db);
        if name.contains(filter) {
            let mut names = vec![name];
            let mut parent = *parent;
            while let Some(index) = parent {
                let (caller, next) = paths.get_index(index).unwrap();
                names.push(caller.name(db));
                parent = *next;
            }
            names.reverse();
            eprintln!("startup dependency: startup wrapper -> {}", names.join(" -> "));
        }
        if let Some(requests) = node.requests(db, symbols) {
            enqueue(&mut paths, Some(at), requests);
        }
    }
}
