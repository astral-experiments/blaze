//! Stack slots for locals with disjoint storage lifetimes in MIR.

use std::{cmp::Reverse, ops::RangeInclusive};

use rustc_index::bit_set::DenseBitSet;

use super::{
    Arc, BasicBlockId, FunctionCx, FxHashMap, Layout, LocalId, LocalSlot, MirBody, StatementKind,
    Value, returned_local,
};

/// Allocates the ordinary locals of a synchronous function. For frames large enough to
/// span a page, a slot can be reused outside a local's MIR storage lifetime.
pub(super) fn allocate(
    fx: &mut FunctionCx<'_, '_>,
    body: &MirBody<'_>,
    forwarded: &FxHashMap<LocalId, LocalId>,
    mut locals: Vec<(LocalId, Arc<Layout>)>,
) {
    let mut lifetimes: FxHashMap<LocalId, Lifetime> = FxHashMap::default();
    let mut offsets = vec![0];
    if locals.iter().map(|(_, layout)| layout.size.bytes()).sum::<u64>() >= 4096 {
        for (block, data) in body.basic_blocks.iter() {
            for (at, statement) in data.statements.iter().enumerate() {
                match statement.kind {
                    StatementKind::StorageLive(local) => {
                        lifetimes.entry(local).or_default().starts.push((block, at));
                    }
                    StatementKind::StorageDead(local) => {
                        lifetimes.entry(local).or_default().ends.entry(block).or_default().push(at);
                    }
                    _ => {}
                }
            }
            offsets.push(offsets.last().unwrap() + data.statements.len() + 1);
        }
        // A forwarded temporary uses its destination's address before that destination's
        // own storage starts. Arguments and the return place come from the caller.
        for local in forwarded.values().chain(&body.param_locals) {
            lifetimes.remove(local);
        }
        lifetimes.remove(&returned_local(body));
        locals.sort_by_key(|(_, layout)| Reverse((layout.size, layout.align.abi)));
    }

    let mut pool: Vec<Slot> = Vec::new();
    for (local, layout) in locals {
        let life = lifetimes.remove(&local).filter(|it| !it.starts.is_empty());
        let addr = match life {
            Some(life) => {
                let ranges = life.ranges(body, &offsets);
                let existing = pool.iter_mut().rev().find(|slot| {
                    slot.size >= layout.size.bytes()
                        && slot.align >= layout.align.abi.bytes()
                        && !ranges.iter().any(|at| slot.occupied.contains_any(at.clone()))
                });
                let slot = match existing {
                    Some(slot) => slot,
                    None => {
                        pool.push(Slot {
                            addr: fx.stack_slot(&layout),
                            size: layout.size.bytes(),
                            align: layout.align.abi.bytes(),
                            occupied: DenseBitSet::new_empty(*offsets.last().unwrap()),
                        });
                        pool.last_mut().unwrap()
                    }
                };
                for at in ranges {
                    slot.occupied.insert_range(at);
                }
                slot.addr
            }
            None => fx.stack_slot(&layout),
        };
        fx.locals.insert(local, LocalSlot { addr, borrowed: false });
    }
}

struct Slot {
    addr: Value,
    size: u64,
    align: u64,
    occupied: DenseBitSet<usize>,
}

#[derive(Default)]
struct Lifetime {
    starts: Vec<(BasicBlockId, usize)>,
    ends: FxHashMap<BasicBlockId, Vec<usize>>,
}

impl Lifetime {
    /// Walks from each StorageLive to the first StorageDead on every normal and unwind
    /// path. Each block has one location per statement and one for its terminator.
    fn ranges(self, body: &MirBody<'_>, offsets: &[usize]) -> Vec<RangeInclusive<usize>> {
        let mut ranges = Vec::new();
        let mut entered = DenseBitSet::new_empty(body.basic_blocks.len());
        let mut pending = self.starts;
        while let Some((block, start)) = pending.pop() {
            let index = block.into_raw().into_u32() as usize;
            if start == 0 && !entered.insert(index) {
                continue;
            }
            let data = &body.basic_blocks[block];
            let end = self
                .ends
                .get(&block)
                .and_then(|ends| ends.get(ends.partition_point(|&i| i < start)));
            ranges.push(
                offsets[index] + start
                    ..=offsets[index] + end.copied().unwrap_or(data.statements.len()),
            );
            if end.is_none()
                && let Some(terminator) = &data.terminator
            {
                let next = terminator.kind.successors().into_iter().chain(terminator.kind.unwind());
                pending.extend(next.map(|next| (next, 0)));
            }
        }
        ranges
    }
}
