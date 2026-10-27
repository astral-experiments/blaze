#!/usr/bin/env python3
"""A lazy static must call the current version of its initializer's inline helper."""
import os
from pathlib import Path
import subprocess
import tempfile


def main():
    with tempfile.TemporaryDirectory(prefix="bz-lazy-", dir="/tmp") as tmp:
        root = Path(tmp)
        (root / "src").mkdir()
        (root / "values/src").mkdir(parents=True)
        (root / "Cargo.toml").write_text(
            '[package]\nname="app"\nversion="0.1.0"\nedition="2021"\n'
            '[workspace]\nmembers=["values"]\n[dependencies]\nvalues={path="values"}\n'
            '[profile.dev]\nopt-level=1\n'
        )
        (root / "values/Cargo.toml").write_text('[package]\nname="values"\nversion="0.1.0"\nedition="2021"\n')
        (root / "src/main.rs").write_text("""
use std::sync::LazyLock;
#[inline(never)]
fn registry() -> &'static usize {
    static REGISTRY: LazyLock<usize> = LazyLock::new(|| values::left());
    &REGISTRY
}
fn main() { println!("{} {} {}", *registry(), values::left(), values::tag()); }
""")
        env = dict(os.environ, RA_JIT_HOME=str(root / "h"), RA_JIT_CACHE=str(root / "store"),
                   RA_JIT_SPARE="0", RA_JIT_RUSTC_BASE="0", CARGO_TARGET_DIR=str(root / "target"),
                   PATH=str(Path(os.environ["RA_JIT_BIN"]).resolve().parent) + os.pathsep + os.environ["PATH"])
        try:
            for value, tag in [(10, 30), (10, 20), (11, 20)]:
                (root / "values/src/lib.rs").write_text(
                    f"#[inline(always)] pub fn left() -> usize {{ {value} }}\n"
                    f"#[inline(never)] pub fn tag() -> usize {{ {tag} }}\n"
                )
                subprocess.run(["cargo", "ra-jit", "build"], cwd=root, env=env, check=True, timeout=900)
                actual = subprocess.check_output([root / "target/debug/app"], text=True, timeout=60).strip()
                if actual != f"{value} {value} {tag}":
                    raise SystemExit(f"value={value}, tag={tag}: got {actual!r}")
        finally:
            subprocess.run(["cargo", "ra-jit", "stop"], cwd=root, env=env, capture_output=True, timeout=120)


if __name__ == "__main__":
    main()
