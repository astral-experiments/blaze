#!/usr/bin/env python3
"""Read the same atomic that rustc's allocator shim writes after restarting the worker."""
import os
from pathlib import Path
import subprocess
import tempfile


PROGRAM = """
mod input;
use std::alloc::{GlobalAlloc, Layout, System};
use std::sync::atomic::{AtomicUsize, Ordering};

static SEEN: AtomicUsize = AtomicUsize::new(0);

struct Alloc;
unsafe impl GlobalAlloc for Alloc {
    unsafe fn alloc(&self, layout: Layout) -> *mut u8 {
        SEEN.store(input::seen(), Ordering::Relaxed);
        System.alloc(layout)
    }
    unsafe fn dealloc(&self, ptr: *mut u8, layout: Layout) { System.dealloc(ptr, layout) }
}
#[global_allocator]
static GLOBAL: Alloc = Alloc;

fn main() {
    let n = Box::new(std::env::args().count());
    println!("{} {} {}", SEEN.load(Ordering::Relaxed), input::tag(), n);
}
"""


if __name__ == "__main__":
    with tempfile.TemporaryDirectory(prefix="bz-alloc-", dir="/tmp") as tmp:
        root = Path(tmp)
        (root / "src").mkdir()
        (root / "Cargo.toml").write_text('[package]\nname="observe"\nversion="0.1.0"\nedition="2021"\n[profile.dev]\nopt-level=2\ncodegen-units=1\n')
        (root / "src/main.rs").write_text(PROGRAM)
        env = dict(os.environ, RA_JIT_HOME=str(root / "h"), RA_JIT_CACHE=str(root / "store"),
                   RA_JIT_SPARE="0", RA_JIT_RUSTC_BASE="0", CARGO_TARGET_DIR=str(root / "target"),
                   PATH=str(Path(os.environ["RA_JIT_BIN"]).resolve().parent) + os.pathsep + os.environ["PATH"])
        for seen, tag in [(10, 30), (10, 20), (11, 20)]:
            (root / "src/input.rs").write_text(f'#[inline(always)] pub fn seen() -> usize {{ {seen} }}\n#[inline(never)] pub fn tag() -> usize {{ {tag} }}\n')
            try:
                subprocess.run(["cargo", "ra-jit", "build"], cwd=root, env=env, check=True, timeout=900)
                actual = subprocess.check_output([root / "target/debug/observe"], text=True, timeout=60).strip()
                if actual != f"{seen} {tag} 1":
                    raise SystemExit(f"seen={seen}, tag={tag}: got {actual!r}")
            finally:
                subprocess.run(["cargo", "ra-jit", "stop"], cwd=root, env=env, check=True, capture_output=True)
