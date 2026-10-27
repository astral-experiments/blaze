#!/usr/bin/env python3
"""renamedstatics.py: a static with state that LLVM renamed in rustc's object at opt-level 1
(`SERVING` to `SERVING.0`) is one static for rustc's code and the daemon's. Linux and macOS: on
Windows `cargo ra-jit` hands builds to cargo. The allocator also registers the address of a
read-only static. The daemon-built caller must see the same address.

usage: renamedstatics.py [--not-incremental]    (RA_JIT_BIN names the ra-jit built;
                                                 cargo-ra-jit and ra-jit-rustc are found next
                                                 to it)

rustc keeps a static that the code of one codegen unit uses to that unit, and LLVM's global
optimizer splits such a static of one field (an `AtomicBool`) into that field, named after the
static with `.0`: an incremental build has it as a hidden symbol `SERVING.0`, one that is not as
a symbol private to the object. Here the program's `#[global_allocator]` sets the static, and
rustc's allocator shim, which is always rustc's, has that inlined; `serving()` reads it, and an
edit to what `serving()` inlines has the daemon compile it. The daemon looked the static up
under the symbol rustc gave it, found none, and gave its code a copy of its own that nothing
ever set, so the program printed `false` (the space game's `eye_state::SERVING`, whose every
`eye.*` reading went empty under blaze).

The daemon has to share rustc's copy: under its own name where the object says it is the whole
static, in a copy of rustc's object that makes it linkable where it was private to the object
(macOS and Linux both). The harness first checks that the program has the renamed symbol, so
that it tests what it is about.

Python 3, stdlib only.
"""
import glob
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
os.environ.setdefault("RUSTUP_TOOLCHAIN", open(os.path.join(HERE, "toolchain")).read().strip())
TIMEOUT = 900
FILES = {
    "ws/Cargo.toml": '[workspace]\nmembers = ["app"]\nresolver = "2"\n\n[profile.dev]\nopt-level = 1\n',
    "ws/app/Cargo.toml": '[package]\nname = "rapp"\nversion = "0.1.0"\nedition = "2021"\n',
    "ws/app/src/main.rs": """use std::alloc::{GlobalAlloc, Layout, System};
use std::sync::atomic::{AtomicBool, AtomicPtr, Ordering};

static SERVING: AtomicBool = AtomicBool::new(false);
static RULE: &str = "invalid-assignment";
static REGISTERED: AtomicPtr<&str> = AtomicPtr::new(std::ptr::null_mut());

struct Serving;

unsafe impl GlobalAlloc for Serving {
    unsafe fn alloc(&self, layout: Layout) -> *mut u8 {
        SERVING.store(true, Ordering::Relaxed);
        REGISTERED.store(std::ptr::from_ref(&RULE).cast_mut(), Ordering::Relaxed);
        unsafe { System.alloc(layout) }
    }

    unsafe fn dealloc(&self, ptr: *mut u8, layout: Layout) {
        unsafe { System.dealloc(ptr, layout) }
    }
}

#[global_allocator]
static ALLOCATOR: Serving = Serving;

fn value() -> usize {
    1
}

#[inline(never)]
fn serving() -> bool {
    SERVING.load(Ordering::Relaxed) && value() > 0
}

fn main() {
    let numbers: Vec<usize> = (0..std::env::args().count() + 3).collect();
    assert!(std::ptr::eq(REGISTERED.load(Ordering::Relaxed), &RULE));
    let registered = unsafe { *REGISTERED.load(Ordering::Relaxed) };
    assert_eq!(registered, RULE);
    println!("{} {} {}", serving(), numbers.len() + value(), registered);
}
""",
}
# (what the step is, (old, new) in app/src/main.rs, what the program prints after it)
COLD_EDIT = "a later edit with a fresh worker"
STEPS = [
    ("a value `serving()` inlines", ("    1\n", "    2\n"), "true 6 invalid-assignment"),
    ("and again", ("    2\n", "    5\n"), "true 9 invalid-assignment"),
    ("change a shared readonly initializer", ('= "invalid-assignment";', '= "changed rule";'), "true 9 changed rule"),
    ("change code alongside the new value", ("    5\n", "    6\n"), "true 10 changed rule"),
    ("restore its initializer", ('= "changed rule";', '= "invalid-assignment";'), "true 10 invalid-assignment"),
    ("change the initializer before a restart", ('= "invalid-assignment";', '= "cold rule";'), "true 10 cold rule"),
    (COLD_EDIT, ("    6\n", "    7\n"), "true 11 cold rule"),
]


def build(ws, env, step):
    ran = subprocess.run(["cargo", "ra-jit", "build"], cwd=ws, env=env, capture_output=True, text=True,
                         timeout=TIMEOUT)
    said = ran.stdout + ran.stderr
    if ran.returncode != 0:
        raise SystemExit(f"{step}: the build failed:\n{said[-3000:]}")
    return said


def renamed(program):
    """The program's symbols of `SERVING` with something LLVM put after the name rustc gave it."""
    listed = subprocess.run(["nm", "-a", program], capture_output=True, text=True, timeout=60).stdout
    return [line.split()[-1] for line in listed.splitlines() if re.search(r"7SERVING\.\d", line)]


def main():
    incremental = "--not-incremental" not in sys.argv[1:]
    bin_dir = os.path.dirname(os.path.abspath(os.environ["RA_JIT_BIN"]))
    if not glob.glob(os.path.join(bin_dir, "cargo-ra-jit*")):
        raise SystemExit(f"no cargo-ra-jit beside {os.environ['RA_JIT_BIN']}")
    # The daemon's sockets are under its home, and a socket's path has to be short: macOS's
    # temporary directory is too long a start.
    root = tempfile.mkdtemp(prefix="ra-jit-renamedstatics-", dir="/tmp")
    ws, home = os.path.join(root, "ws"), os.path.join(root, "h")
    for path, text in FILES.items():
        os.makedirs(os.path.dirname(os.path.join(root, path)), exist_ok=True)
        open(os.path.join(root, path), "w").write(text)
    # A store of its own, so that no function compiled by another build of ra-jit is found.
    env = dict(os.environ, PATH=f"{bin_dir}{os.pathsep}{os.environ['PATH']}", RA_JIT_HOME=home,
               RA_JIT_CACHE=os.path.join(root, "store"))
    for name in ("CARGO_TARGET_DIR", "CARGO_BUILD_BUILD_DIR", "RUSTFLAGS", "CARGO_ENCODED_RUSTFLAGS"):
        env.pop(name, None)
    env["CARGO_INCREMENTAL"] = "1" if incremental else "0"
    program, source = os.path.join(ws, "target", "debug", "rapp"), os.path.join(ws, "app", "src", "main.rs")
    wrong = 0
    try:
        build(ws, env, "first build")
        found = renamed(program)
        if not found:
            raise SystemExit(f"rustc's build has no `SERVING.<n>`: the harness tests nothing; under {root}")
        print(f"rustc's copy: {' '.join(found)} ({'' if incremental else 'not '}incremental)", flush=True)
        for _ in range(240):
            if "[ra-jit]" in build(ws, env, "wait for the daemon"):
                break
            time.sleep(0.5)
        else:
            raise SystemExit(f"no build said `[ra-jit]`; the daemon's log is in {home}")
        for what, (old, new), expected in STEPS:
            text = open(source).read()
            assert old in text, (what, old)
            open(source, "w").write(text.replace(old, new, 1))
            cold = what == COLD_EDIT
            if cold:
                subprocess.run(["cargo", "ra-jit", "stop"], cwd=ws, env=env, check=True,
                               capture_output=True, timeout=120)
            said = build(ws, env, what)
            printed = subprocess.run([program], capture_output=True, text=True, timeout=60).stdout.strip()
            verdict = "ok"
            if printed != expected:
                verdict = f"PRINTED {printed!r}, NOT {expected!r}"
            # A fresh worker may need Cargo to rebuild the stale native allocation.
            elif not cold and "[ra-jit]" not in said:
                why = [line for line in said.splitlines() if "ra-jit" in line]
                verdict = "NOT SERVED BY THE DAEMON: " + " | ".join(why)[-600:]
            wrong += verdict != "ok"
            print(f"{what:42} {verdict}", flush=True)
    finally:
        subprocess.run(["cargo", "ra-jit", "stop"], cwd=ws, env=env, capture_output=True, timeout=120)
    print(f"{len(STEPS) - wrong} of {len(STEPS)} right; under {root}")
    if wrong:
        sys.exit(1)
    shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    main()
