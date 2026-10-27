#!/usr/bin/env python3
"""Check per-function disk reuse when a fresh Blaze worker starts after an edit or its undo.

    RA_JIT_BIN=.../ra-jit python3 harness/storerevisions.py [leaf|inline|signature|binding ...]

Each forward build starts on a copy of the store containing only A. Each undo starts on a
store populated independently from B, so an exact hit for the old checkout cannot hide an
invalid dependency. The Cargo reference gets an empty target directory for every build.
"""
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parent))
from storetracked import Logs, serve


LIB = """use cache_leaf as selected;

#[inline(never)]
pub fn cache_leaf(x: i32) -> i32 { x + 1 }
#[inline(never)]
pub fn cache_leaf_caller(x: i32) -> i32 { selected(x) + 10 }
#[inline(always)]
pub const fn cache_inline(x: i32) -> i32 { x + 2 }
#[inline(never)]
pub fn cache_inline_caller(x: i32) -> i32 { cache_inline(x) + 20 }
const INLINE_COUNT: usize = cache_inline(0) as usize;
#[inline(never)]
pub fn cache_embedded() -> i32 { [0_u8; INLINE_COUNT].len() as i32 }
#[inline(never)]
pub fn cache_public(x: u8) -> u8 { (128u8 | x) as _ }
#[inline(never)]
pub fn cache_sibling(x: i32) -> i32 { x + 40 }
"""
FILES = {
    "Cargo.toml": """[package]
name = "store_app"
version = "0.1.0"
edition = "2021"
[workspace]
members = ["lib"]
[dependencies]
store_dep = { path = "lib" }
[profile.dev]
overflow-checks = false
""",
    "lib/Cargo.toml": '[package]\nname = "store_dep"\nversion = "0.1.0"\nedition = "2021"\n',
    "lib/src/lib.rs": LIB,
    "src/main.rs": """#[inline(never)]
fn cache_public_caller(x: u8) -> i32 { store_dep::cache_public(x) as i32 }
fn main() {
    let x = std::env::args().len() as i32;
    println!("{} {} {} {} {}", store_dep::cache_leaf_caller(x), store_dep::cache_inline_caller(x),
             cache_public_caller(x as u8), store_dep::cache_sibling(x), store_dep::cache_embedded());
}
""",
}
# Functions which the worker must lower, and which it must actually restore for this edit.
# The sibling lives in the edited file. The public signature's caller lives in the other crate.
CASES = {
    "leaf": ("{ x + 1 }", "{ x + 4 }", {"cache_leaf"}, {"cache_sibling", "cache_leaf_caller"}),
    "inline": ("{ x + 2 }", "{ x + 7 }", {"cache_inline_caller", "cache_embedded"}, {"cache_sibling"}),
    "signature": ("cache_public(x: u8) -> u8", "cache_public(x: u8) -> i8",
                  {"cache_public", "cache_public_caller"}, {"cache_sibling"}),
    "binding": ("use cache_leaf as selected;", "use cache_sibling as selected;",
                {"cache_leaf_caller"}, {"cache_sibling"}),
}


def output(executable):
    return [subprocess.check_output([executable, *args], text=True, timeout=30)
            for args in ([], ["second"])]


def check_embedded(executable, expected):
    # Mach-O can label the address with a co-located assembler symbol.
    symbols = subprocess.check_output(["nm", "-n", executable], text=True)
    addresses = re.findall(r"^([\da-fA-F]+)\s+[tT]\s+\S*cache_embedded\S*$", symbols, re.M)
    assert len(addresses) == 1, f"expected one cache_embedded function, saw {addresses}"
    start = int(addresses[0], 16)
    objdump = ["xcrun", "llvm-objdump"] if sys.platform == "darwin" else ["objdump"]
    assembly = subprocess.check_output([*objdump, "-d", "--no-show-raw-insn", f"--start-address={start:#x}",
                                        f"--stop-address={start + 256:#x}", executable], text=True)
    instructions, *end = re.split(r"\bretq?\b", assembly, maxsplit=1)
    assert end, f"missing return from cache_embedded: {assembly}"
    assert not re.search(r"\b(callq?|bl|blr)\b|cache_inline", instructions), instructions
    # x86-64 uses '$' for an immediate; AArch64 uses '#'.
    assert re.search(rf"[$#](?:0x)?{expected}\b", instructions), instructions


def build(ws, store, label, inline=False):
    assert not (ws / "target").exists(), f"{label}: expected a clean Blaze target"
    home = ws.parent / "home"
    env = dict(os.environ, RA_JIT_HOME=str(home), RA_JIT_CACHE=str(store), RA_JIT_RUSTC_BASE="0",
               RA_JIT_SPARE="0", RA_JIT_TRACE_FUNCTION_CACHE="1")
    for name in ("CARGO_TARGET_DIR", "RUSTFLAGS", "CARGO_ENCODED_RUSTFLAGS"):
        env.pop(name, None)
    oracle = ws.parent / "oracle"
    subprocess.run(["cargo", "build", "--quiet", "--target-dir", oracle], cwd=ws, env=env,
                   check=True, timeout=180)
    reference = oracle / "debug/store_app"
    expected = output(reference)
    if inline:
        # The fresh rustc executable runs the changed arithmetic, keeps its caller and its
        # out-of-line sibling, and has no separately callable copy of the inline helper.
        symbols = subprocess.check_output(["nm", reference], text=True)
        assert "cache_inline_caller" in symbols and re.search(r"cache_leaf(?!_caller)", symbols), label
        assert not re.search(r"cache_inline(?!_caller)", symbols), f"{label}: Cargo kept cache_inline out of line"
    logs = Logs(str(home))
    try:
        serve(str(ws), env, label)
        executable = ws / "target/debug/store_app"
        actual = output(executable)
        assert actual == expected, f"{label}: Blaze printed {actual!r}, Cargo printed {expected!r}"
        if inline:
            check_embedded(executable, expected[0].split()[-1])
        print(f"{label}: matched Cargo {actual!r}", flush=True)
        return expected, logs.text()
    finally:
        subprocess.run(["cargo", "ra-jit", "stop"], cwd=ws, env=env, capture_output=True,
                       check=True, timeout=120)


def decisions(log, label, lowered, reused):
    events = {"lowered": set(), "reused": set()}
    for action, path in re.findall(r"^function cache: (lowered|reused) (.+)$", log, re.M):
        events[action].add(path)
    for action, wanted in (("lowered", lowered), ("reused", reused)):
        for name in sorted(wanted):
            other = "lowered" if action == "reused" else "reused"
            crate = "store_app" if name == "cache_public_caller" else "store_dep"
            path = f"{crate}::{name}"
            if path not in events[action] or path in events[other]:
                raise AssertionError(f"{label}: expected {action} for {name}, saw {events}")
    print(f"{label}: lowered {', '.join(sorted(lowered))}, reused {', '.join(sorted(reused))}", flush=True)


def main():
    cases = sys.argv[1:] or list(CASES)
    if set(cases) - CASES.keys():
        raise SystemExit(f"expected a case from {', '.join(CASES)}")
    binary = Path(os.environ["RA_JIT_BIN"]).resolve()
    os.environ["PATH"] = str(binary.parent) + os.pathsep + os.environ["PATH"]
    root = Path(tempfile.mkdtemp(prefix="bz-revs-", dir="/tmp")).resolve()
    print(f"work: {root}", flush=True)
    ws = root / "seed-A/ws"
    for filename, contents in FILES.items():
        path = ws / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(contents)
    first_store = root / "only-A"
    original, _ = build(ws, first_store, "seed-A", inline="inline" in cases)

    def checkout(source, label):
        return Path(shutil.copytree(source, root / label / "ws", ignore=shutil.ignore_patterns("target")))

    for case in cases:
        old, new, lowered, reused = CASES[case]
        assert LIB.count(old) == 1
        edited = checkout(ws, f"{case}-edit")
        (edited / "lib/src/lib.rs").write_text(LIB.replace(old, new))
        forward = root / f"{case}-from-A"
        shutil.copytree(first_store, forward)
        changed, log = build(edited, forward, f"{case}-edit", inline=case == "inline")
        assert all(a != b for a, b in zip(original, changed)), f"{case}: edit did not change the Cargo oracle"
        decisions(log, f"{case} edit", lowered, reused)
        reverse = root / f"{case}-only-B"
        seed = checkout(edited, f"{case}-seed-B")
        build(seed, reverse, f"{case}-seed-B")
        undo = checkout(seed, f"{case}-undo")
        (undo / "lib/src/lib.rs").write_text(LIB)
        restored, log = build(undo, reverse, f"{case}-undo", inline=case == "inline")
        assert restored == original, f"{case}: undo did not restore the original program"
        decisions(log, f"{case} undo", lowered, reused)
    shutil.rmtree(root)


if __name__ == "__main__":
    main()
