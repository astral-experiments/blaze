#!/usr/bin/env python3
"""Check per-function disk reuse when a fresh Blaze worker starts after an edit or its undo.

    RA_JIT_BIN=.../ra-jit python3 harness/storerevisions.py [case ...]

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
LIB = """pub mod fields;
pub mod tables;
mod tags;
use cache_leaf as selected;

impl fields::Header {
    #[inline(never)]
    pub fn cache_field(&self) -> u8 { self.value }
    #[inline(never)]
    pub fn cache_contains(&self, byte: u8) -> bool { self.value <= byte && byte <= self.prefix }
}

impl tables::Folder {
    #[inline(never)]
    pub fn cache_folder() -> Result<Self, tables::FoldError> {
        Ok(Self { table: tables::FOLDS, last: None, flag: tables::Flag::Idle, next: 0 })
    }
}

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
#[inline(never)]
pub fn cache_if(x: i32) -> i32 { if x < 0 { x - 9 } else { x + 31 } }
#[inline(never)]
pub fn cache_switch(x: i32) -> i32 {
    match x { tags::TAG => x + 41, -1 => x - 11, _ => x + 50 }
}
#[inline(never)]
pub fn cache_arm(x: i32) -> i32 { if x < 0 { cache_inline(x) + 20 } else { 0 } }
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
    "lib/src/fields.rs": "#[repr(C)] pub struct Header { pub prefix: u8, pub value: u8, pub tail: u32 }\n",
    "lib/src/tables.rs": """pub const FOLDS: &[(char, &[char])] = &[('K', &['k', 'K']), ('S', &['s', 'ſ'])];
#[repr(u8)]
pub enum Flag { Idle = 1, Other = 7 }
#[repr(C)]
pub struct Folder { pub last: Option<char>, pub flag: Flag, pub table: &'static [(char, &'static [char])], pub next: usize }
pub struct FoldError(());
""",
    "lib/src/tags.rs": "pub const TAG: i32 = 1;\n",
    "src/main.rs": """#[inline(never)]
fn cache_public_caller(x: u8) -> i32 { store_dep::cache_public(x) as i32 }
fn main() {
    let x = std::env::args().len() as i32;
    let signed = x - 2;
    // Both layouts are eight bytes of integers. Only the repr(C) value is at byte one.
    let mut bytes = [x as u8 + 100; 8];
    bytes[1] = x as u8 + 20;
    bytes[4] = x as u8 + 150;
    let header: store_dep::fields::Header = unsafe { std::mem::transmute(bytes) };
    let needle = if x == 1 { 50 } else { 125 };
    let Ok(folder) = store_dep::tables::Folder::cache_folder() else { std::process::exit(2) };
    let folded = folder.table[folder.next].1[1] as u32;
    println!("{} {} {} {} {} {} {} {} {} {} {} {} {} {}", store_dep::cache_leaf_caller(x), store_dep::cache_inline_caller(x),
             header.cache_field(), header.cache_contains(needle),
             folded, std::mem::offset_of!(store_dep::tables::Folder, table), folder.flag as u8,
             cache_public_caller(x as u8), store_dep::cache_sibling(x), store_dep::cache_if(signed),
             store_dep::cache_switch(x), store_dep::cache_switch(signed), store_dep::cache_arm(signed),
             store_dep::cache_embedded());
}
""",
}
# Functions which the worker must lower, and which it must actually restore for this edit.
# The leaf's siblings live in the edited file. The public signature's caller lives in the other crate.
CASES = {
    "leaf": ("{ x + 1 }", "{ x + 4 }", {"cache_leaf"},
             {"cache_sibling", "cache_leaf_caller", "cache_if", "cache_switch", "cache_field", "cache_contains"}),
    "inline": ("{ x + 2 }", "{ x + 7 }", {"cache_inline_caller", "cache_embedded", "cache_arm"}, {"cache_sibling"}),
    "signature": ("cache_public(x: u8) -> u8", "cache_public(x: u8) -> i8",
                  {"cache_public", "cache_public_caller"}, {"cache_sibling"}),
    "binding": ("use cache_leaf as selected;", "use cache_sibling as selected;",
                {"cache_leaf_caller"}, {"cache_sibling"}),
    "tag": ("TAG: i32 = 1", "TAG: i32 = 2", {"cache_switch"}, {"cache_if", "cache_sibling"}),
    "field": ("{ self.value }", "{ self.prefix }", {"cache_field"}, {"cache_sibling", "cache_contains"}),
    "layout": ("#[repr(C)]", "#[repr(Rust)]", {"cache_field", "cache_contains"}, {"cache_sibling"}),
    "table": ("'K'", "'Å'", {"cache_folder"}, {"cache_sibling"}),
    "table-layout": ("#[repr(C)]", "#[repr(Rust)]", {"cache_folder"}, {"cache_sibling"}),
    "table-tag": ("Idle = 1, Other = 7", "Idle = 7, Other = 1", {"cache_folder"}, {"cache_sibling"}),
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


def check_inline_caller(executable, delta, function="cache_inline_caller"):
    # A runtime argument enters this caller. LLVM folds the helper and the caller's own add
    # into one offset, using add or lea on x86-64 and add on AArch64.
    body = instructions(executable, function)
    assert not re.search(r"\b(callq?|bl|blr)\b", body), body
    if function == "cache_inline_caller":
        assert not re.search(r"\b(jmpq?|b|br)\b", body), body
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
            check_inline_caller(executable, delta, "cache_arm")
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
    header = "<store_dep::fields::Header>"
    paths = {"cache_public_caller": "store_app::cache_public_caller",
             "cache_folder": "<store_dep::tables::Folder>::cache_folder",
             "cache_field": f"{header}::cache_field", "cache_contains": f"{header}::cache_contains"}
    events = {"lowered": set(), "reused": set()}
    for action, path in re.findall(r"^function cache: (lowered|reused) (.+)$", log, re.M):
        events[action].add(path)
    for action, wanted in (("lowered", lowered), ("reused", reused)):
        for name in sorted(wanted):
            other = "lowered" if action == "reused" else "reused"
            path = paths.get(name, f"store_dep::{name}")
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
        if "cache_folder" not in lowered:
            reused = reused | {"cache_folder"}
        if llvm and case != "inline":
            reused = reused | {"cache_inline_caller", "cache_arm"}
        filename = {"tag": "lib/src/tags.rs", "layout": "lib/src/fields.rs",
                    "table": "lib/src/tables.rs", "table-layout": "lib/src/tables.rs",
                    "table-tag": "lib/src/tables.rs"}.get(case, "lib/src/lib.rs")
        contents = FILES[filename]
        assert contents.count(old) == 1
        edited = checkout(ws, f"{case}-edit")
        (edited / filename).write_text(contents.replace(old, new))
        forward = root / f"{case}-from-A"
        shutil.copytree(first_store, forward)
        changed, log = build(edited, forward, f"{case}-edit", inline=case == "inline")
        assert all(a != b for a, b in zip(original, changed)), f"{case}: edit did not change the Cargo oracle"
        if case in ("field", "layout"):
            assert [int(s.split()[2]) for s in original] == [21, 22]
            assert [int(s.split()[2]) for s in changed] == [101, 102]
        if case == "layout":
            assert [s.split()[3] for s in original] == ["true", "false"]
            assert [s.split()[3] for s in changed] == ["false", "true"]
        if case == "table":
            assert all(int(s.split()[4]) == ord("K") for s in original)
            assert all(int(s.split()[4]) == ord("Å") for s in changed)
        if case == "table-layout":
            assert all(a.split()[4] == b.split()[4] and a.split()[5] != b.split()[5]
                       for a, b in zip(original, changed))
        if case == "table-tag":
            assert all(a.split()[6] == "1" and b.split()[6] == "7" for a, b in zip(original, changed))
        if case == "inline":
            assert [a.split()[-2] != b.split()[-2] for a, b in zip(original, changed)] == [True, False]
        if case == "tag":
            # x is 1 or 2. The other switch invocation takes the fixed -1 arm or the default.
            assert all(a.split()[-4] != b.split()[-4] and a.split()[-3] == b.split()[-3]
                       for a, b in zip(original, changed))
        decisions(log, f"{case} edit", lowered, reused)
        reverse = root / f"{case}-only-B"
        seed = checkout(edited, f"{case}-seed-B")
        build(seed, reverse, f"{case}-seed-B")
        undo = checkout(seed, f"{case}-undo")
        (undo / filename).write_text(contents)
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
