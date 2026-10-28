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
mod label;
use std::sync::LazyLock;
#[inline(never)]
fn registry() -> &'static usize {
    static REGISTRY: LazyLock<usize> = LazyLock::new(|| values::left());
    &REGISTRY
}
fn main() { println!("{} {} {} {}", *registry(), values::left(), values::tag(), label::value()); }
""")
        env = dict(os.environ, RA_JIT_HOME=str(root / "h"), RA_JIT_CACHE=str(root / "store"),
                   RA_JIT_SPARE="0", RA_JIT_RUSTC_BASE="0", RA_JIT_STATS="1", CARGO_TARGET_DIR=str(root / "target"),
                   PATH=str(Path(os.environ["RA_JIT_BIN"]).resolve().parent) + os.pathsep + os.environ["PATH"])
        try:
            declined = "no patch: native code in a static initializer may have changed"
            for value, tag, label in [(10, 30, 1), (10, 30, 2), (10, 20, 2), (11, 20, 2)]:
                sources = {
                    "values/src/lib.rs": f"#[inline(always)] pub fn left() -> usize {{ {value} }}\n"
                                         f"#[inline(never)] pub fn tag() -> usize {{ {tag} }}\n",
                    "src/label.rs": f"pub fn value() -> usize {{ {label} }}\n",
                }
                for name, source in sources.items():
                    path = root / name
                    if not path.exists() or path.read_text() != source:
                        path.write_text(source)
                built = subprocess.run(["cargo", "ra-jit", "build"], cwd=root, env=env, check=True, timeout=900,
                                       stderr=subprocess.PIPE, text=True)
                if (value, tag, label) == (10, 30, 2):
                    logs = list((root / "h/workers").glob("*/log"))
                    if (not logs or "profile [ra-jit]" not in built.stderr
                            or any(declined in log.read_text(errors="replace") for log in logs)):
                        raise SystemExit(f"An unrelated body edit invalidated the callback\n{built.stderr}")
                actual = subprocess.check_output([root / "target/debug/app"], text=True, timeout=60).strip()
                if actual != f"{value} {value} {tag} {label}":
                    raise SystemExit(f"value={value}, tag={tag}, label={label}: got {actual!r}")
        finally:
            subprocess.run(["cargo", "ra-jit", "stop"], cwd=root, env=env, capture_output=True, timeout=120)


if __name__ == "__main__":
    main()
