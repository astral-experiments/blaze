# Working on Blaze

- Use the rustc version pinned in `harness/toolchain` as the reference. When fixing a compiler bug, reduce the failing program and compare its behavior under Blaze and rustc. See [the test harnesses](docs/harness.md).
- When Blaze cannot compile or patch something, report what failed and where. Keep unsupported operations visible. Do not add fallbacks to Cargo, rustc, another backend, or a cached result. See [the remaining rustc dependencies](docs/no-rustc.md).
- Run tests in the sandbox by default and respect explicit opt-outs. See [test sandboxing](docs/cargo.md#every-test-runs-in-a-sandbox).
- Put language semantics, including MIR lowering, borrow checking, and type layout, in `vendor/rust-analyzer/`. Code generation, linking, the daemon, and Cargo integration belong in `crates/`. See [the architecture](docs/architecture.md).
