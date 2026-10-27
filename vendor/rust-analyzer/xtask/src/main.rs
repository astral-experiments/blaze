//! See <https://github.com/matklad/cargo-xtask/>.
//!
//! Generates compiler sources and checks the vendored workspace.

#![warn(rust_2018_idioms, unused_lifetimes)]
#![allow(
    clippy::print_stderr,
    clippy::print_stdout,
    clippy::disallowed_methods,
    // 1. We want to be dependency-light
    // 2. Speed of hash maps doesn't matter, as xtask is dev tooling
    clippy::disallowed_types
)]

mod flags;

mod codegen;
mod tidy;
mod util;

use anyhow::bail;
use std::{env, path::PathBuf};
use xshell::{Shell, cmd};

fn main() -> anyhow::Result<()> {
    let flags = flags::Xtask::from_env_or_exit();

    let sh = &Shell::new()?;
    sh.change_dir(project_root());

    match flags.subcommand {
        flags::XtaskCmd::FuzzTests(_) => run_fuzzer(sh),
        flags::XtaskCmd::Codegen(cmd) => cmd.run(sh),
        flags::XtaskCmd::Tidy(cmd) => cmd.run(sh),
    }
}

/// Returns the path to the root directory of `rust-analyzer` project.
fn project_root() -> PathBuf {
    let dir =
        env::var("CARGO_MANIFEST_DIR").unwrap_or_else(|_| env!("CARGO_MANIFEST_DIR").to_owned());
    PathBuf::from(dir).parent().unwrap().to_owned()
}

fn run_fuzzer(sh: &Shell) -> anyhow::Result<()> {
    let _d = sh.push_dir("./crates/syntax");
    let _e = sh.push_env("RUSTUP_TOOLCHAIN", "nightly");
    if cmd!(sh, "cargo fuzz --help").read().is_err() {
        cmd!(sh, "cargo install --locked cargo-fuzz").run()?;
    };

    // Expecting nightly rustc
    let out = cmd!(sh, "rustc --version").read()?;
    if !out.contains("nightly") {
        bail!("fuzz tests require nightly rustc")
    }

    cmd!(sh, "cargo fuzz run parser").run()?;
    Ok(())
}
