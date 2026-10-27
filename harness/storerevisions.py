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
from storetracked import Logs, build as worker_build, serve


INLINE_HELPER = """#[inline(always)]
pub const fn cache_inline(x: i32) -> i32 { x + 2 }
"""
LIVE_HELPER = """#[inline]
pub fn cache_inline(x: i32) -> i32 { x + 2 }
"""
LIB = """use cache_leaf as selected;

#[inline(never)]
pub fn cache_leaf(x: i32) -> i32 { x + 1 }
#[inline(never)]
pub fn cache_leaf_caller(x: i32) -> i32 { selected(x) + 10 }
""" + INLINE_HELPER + """#[inline(never)]
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


def instructions(executable, function):
    # Mach-O can label the address with a co-located assembler symbol.
    symbols = subprocess.check_output(["nm", "-n", executable], text=True)
    addresses = re.findall(rf"^([\da-fA-F]+)\s+[tT]\s+\S*{function}\S*$", symbols, re.M)
    assert len(addresses) == 1, f"expected one {function} function, saw {addresses}"
    start = int(addresses[0], 16)
    objdump = ["xcrun", "llvm-objdump"] if sys.platform == "darwin" else ["objdump"]
    assembly = subprocess.check_output([*objdump, "-d", "--no-show-raw-insn", f"--start-address={start:#x}",
                                        f"--stop-address={start + 256:#x}", executable], text=True)
    ops = "\n".join(re.findall(r"^\s*[\da-fA-F]+:[ \t]+([^\n]+)", assembly, re.M))
    body, *end = re.split(r"\bretq?\b", ops, maxsplit=1)
    assert end, f"missing return from {function}: {assembly}"
    return body


def check_inline_caller(executable, delta):
    # A runtime argument enters this caller. LLVM folds the helper and the caller's own add
    # into one offset, using add or lea on x86-64 and add on AArch64.
    body = instructions(executable, "cache_inline_caller")
    assert not re.search(r"\b(callq?|jmpq?|bl|blr|b|br)\b", body), body
    immediate = rf"(?:0x{delta:x}|{delta})\b"
    assert re.search(rf"\b(?:add[lq]?|lea[lq]?)\b[^\n]*(?:[$#]{immediate}|{immediate}\()", body), body


def cargo(ws, env, target):
    subprocess.run(["cargo", "build", "--quiet", "--target-dir", target], cwd=ws, env=env,
                   check=True, timeout=180)
    return target / "debug/store_app"


def live_inline(ws, env, initial):
    def workers():
        status = subprocess.check_output(["cargo", "ra-jit", "status"], cwd=ws, env=env, text=True, timeout=30)
        return re.findall(r"worker: pid (\d+)", status)

    pids = workers()
    assert len(pids) == 1, f"expected one live worker, saw {pids}"
    source = ws / "lib/src/helper.rs"
    old, new, *_ = CASES["inline"]
    for step, contents in (("edit", LIVE_HELPER.replace(old, new)), ("undo", LIVE_HELPER)):
        source.write_text(contents)
        expected = output(cargo(ws, env, ws.parent / f"live-{step}-oracle"))
        assert (expected == initial) == (step == "undo"), f"live inline {step}: unexpected Cargo output {expected!r}"
        served = worker_build(str(ws), env, f"live inline {step}")
        assert "[ra-jit]" in served and workers() == pids, f"live inline {step}: {served}"
        actual = output(ws / "target/debug/store_app")
        assert actual == expected, f"live inline {step}: Blaze printed {actual!r}, Cargo printed {expected!r}"
        print(f"live inline {step}: worker {pids[0]} matched Cargo {actual!r}", flush=True)


def build(ws, store, label, inline=False, live=False):
    assert not (ws / "target").exists(), f"{label}: expected a clean Blaze target"
    home = ws.parent / "home"
    env = dict(os.environ, RA_JIT_HOME=str(home), RA_JIT_CACHE=str(store), RA_JIT_RUSTC_BASE="0",
               RA_JIT_SPARE="0", RA_JIT_TRACE_FUNCTION_CACHE="1")
    for name in ("CARGO_TARGET_DIR", "RUSTFLAGS", "CARGO_ENCODED_RUSTFLAGS"):
        env.pop(name, None)
    llvm = env.get("RA_JIT_BACKEND") == "llvm"
    if llvm:
        env.update(RA_JIT_OPT="2", RA_JIT_INLINE="all")
    if live:
        env["RA_JIT_STATS"] = "1"
    reference = cargo(ws, env, ws.parent / "oracle")
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
        log = logs.text()
        if inline:
            body = instructions(executable, "cache_embedded")
            assert not re.search(r"\b(callq?|bl|blr)\b|cache_inline", body), body
            assert re.search(rf"[$#](?:0x)?{expected[0].split()[-1]}\b", body), body
        detail = ""
        if llvm:
            assert re.search(r"^backend: llvm -O2\b", log, re.M), log
            if live:
                assert re.search(r"^base from the store: [1-9]\d* roots\b", log, re.M), log
                assert not re.search(r"^(?:function cache: \w+|  left to rustc:) store_dep::cache_inline_caller\b",
                                     log, re.M), log
            else:
                assert re.search(r"^backend: llvm compiled [1-9]\d* functions?\b", log, re.M), log
            delta = int(expected[0].split()[1]) - 1
            check_inline_caller(executable, delta)
            leaf_caller = instructions(executable, "cache_leaf_caller")
            assert re.search(r"^\s*(?:callq?|bl|blr)\s", leaf_caller, re.M), leaf_caller
            detail = f" (LLVM inlined add {delta}, cache_leaf_caller kept a call)"
        print(f"{label}: matched Cargo {actual!r}{detail}", flush=True)
        if live:
            live_inline(ws, env, expected)
        return expected, log
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

    llvm = os.environ.get("RA_JIT_BACKEND") == "llvm"
    for case in cases:
        old, new, lowered, reused = CASES[case]
        if llvm and case != "inline":
            reused = reused | {"cache_inline_caller"}
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
    if llvm and "inline" in cases:
        # An ordinary inline hint allows a body-only edit. Exact hydration must retain
        # the dependency even though the caller lives in a different file.
        seed = checkout(ws, "live-seed")
        (seed / "lib/src/helper.rs").write_text(LIVE_HELPER)
        live_lib = LIB.replace(INLINE_HELPER, "mod helper;\nuse helper::cache_inline;\n")
        (seed / "lib/src/lib.rs").write_text(live_lib.replace("cache_inline(0) as usize", "2"))
        store = root / "live-store"
        build(seed, store, "live-seed")
        build(checkout(seed, "live-restart"), store, "live-restart", live=True)
    shutil.rmtree(root)


if __name__ == "__main__":
    main()
