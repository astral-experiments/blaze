#!/usr/bin/env python3
"""Read an exported static on the first build after its initializer changes while the worker is stopped."""
import os
from pathlib import Path
import subprocess
import tempfile


def check(root, attribute):
    (root / "src").mkdir(parents=True)
    (root / "Cargo.toml").write_text('[package]\nname="read-export"\nversion="0.1.0"\nedition="2021"\n[profile.dev]\nopt-level=1\n')
    (root / "src/main.rs").write_text(f"""
mod input;
#[unsafe({attribute})]
pub static META: &str = input::TAG;
fn main() {{
    println!("{{}} {{}}", std::hint::black_box(&META), input::TAG);
}}
""")
    env = dict(os.environ, RA_JIT_HOME=str(root / "h"), RA_JIT_CACHE=str(root / "store"),
               RA_JIT_RUSTC_BASE="0", RA_JIT_SPARE="0", CARGO_TARGET_DIR=str(root / "target"),
               PATH=str(Path(os.environ["RA_JIT_BIN"]).resolve().parent) + os.pathsep + os.environ["PATH"])
    for tag in ("blue", "gray"):
        (root / "src/input.rs").write_text(f'pub const TAG: &str = "{tag}";\n')
        try:
            subprocess.run(["cargo", "ra-jit", "build"], cwd=root, env=env, check=True, timeout=900)
            actual = subprocess.check_output([root / "target/debug/read-export"], env=env, text=True).strip()
            if actual != f"{tag} {tag}":
                raise SystemExit(f"{attribute} with {tag}: got {actual!r}")
        finally:
            subprocess.run(["cargo", "ra-jit", "stop"], cwd=root, env=env, check=True, capture_output=True)


if __name__ == "__main__":
    with tempfile.TemporaryDirectory(prefix="bz-exports-", dir="/tmp") as tmp:
        for i, attr in enumerate(("no_mangle", 'export_name="blaze_metadata"')):
            check(Path(tmp) / str(i), attr)
