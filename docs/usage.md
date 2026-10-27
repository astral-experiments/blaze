# Using blaze

Not installed yet: [install.md](install.md). Every command and setting in detail:
[cargo.md](cargo.md).

## Use it on your project

```sh
cd my-project
cargo blaze build
```

The first build runs cargo, with cargo's usual output, while a background process, the daemon,
loads your project beside it. The build then waits for the daemon to build your program, under
a second after cargo on ripgrep and about 4 seconds on nushell:

```
     Waiting for blaze to load the workspace
    Finished `dev` profile [ra-jit] target(s) in 41.20s
```

From then on, an edit builds like this:

```
$ cargo blaze build
    Finished `dev` profile [ra-jit] target(s) in 0.02s
```

`[ra-jit]` means blaze built it. The program is where cargo would have put it, under `target/`.

- `cargo blaze build`, `cargo blaze run` and `cargo blaze test` take cargo's flags and do what
  cargo's commands do: `run` passes what follows `--` to your program, and `test` runs and reports
  the tests as `cargo test` does.
- `cargo blaze check` reports the errors a build would fail with.
- `cargo blaze status` shows what the daemon has loaded, how many builds it did and how many it
  left to cargo, and why.
- `cargo blaze stop` stops the daemon. It also stops by itself after 30 minutes without a build.

Any other subcommand goes to cargo unchanged. When blaze can't be sure its result would be the
same as cargo's, it lets cargo do that build and prints one line saying why
(`ra-jit: bug: <reason>; building with cargo, ...`). Each is a bug: ra-jit is never to build
with cargo ([#84](https://github.com/indexable-inc/ra-jit/issues/84)), and the line names the
step of [the plan](no-rustc.md) that removes it. That happens after a change to `Cargo.toml`,
`Cargo.lock` or what a build script reads, when a test is added or removed, and for a few flags
such as `--message-format` and cargo's own `-Z`. The full list is in [cargo.md](cargo.md#when-cargo-builds-instead).
rustc's `-Z` flags in `RUSTFLAGS` or `rustflags`, such as `-Zthreads=8`, are another matter: rustc
still gets them, and what blaze does with each is in [support.md](support.md#-z-flags-for-rustc).

## Rebuild on every save

blaze has no watch mode of its own; any file watcher can run it. With
[watchexec](https://github.com/watchexec/watchexec):

```sh
watchexec -e rs -- cargo blaze build
```

## Make plain `cargo build` use blaze

For editors, scripts and agents that run `cargo build` themselves:

```sh
cargo blaze enable
```

From then on plain `cargo build`, `run`, `test` and `check` go through blaze, in every project of
your user. It adds one setting to `~/.cargo/config.toml`, and `cargo blaze disable` puts the file back
as it was. Edits are slower this way than with `cargo blaze`, and on some projects slower than plain
cargo ([why](cargo.md#plain-cargo-switched-on-globally)).

## Change a running program

A program that links the `ra-jit-live` crate takes each edit while it runs, and keeps its state:
useful for games, GUIs and anything slow to get back to where you were.

```toml
[dependencies]
ra-jit-live = "0.1"
```

```rust
let mut live = ra_jit_live::Live::start().ok();
loop {
    if let Some(outcome) = live.as_mut().and_then(|it| it.poll()) {
        eprintln!("ra-jit: {outcome}");
    }
    // ...one frame of your program
}
```

Start it with `cargo blaze run`. In another terminal, build after each edit with
`cargo blaze build` (or the `watchexec` line above). The next `poll` swaps in the functions that
changed, usually in under a millisecond, and prints
`ra-jit: edit ... applied: 1 functions in 745.4µs`. An edit that changes a type the program is
holding, such as a struct's fields, can't be applied to data that already exists, so `poll` asks
for a restart instead.

Live edits work on macOS on Apple silicon. Linux x86-64 support is on `main` and comes with the
next release. How it works, and how fast from a keystroke to the running program:
[live.md](live.md).

## What works and what doesn't yet

- **Platforms.** Blaze runs on macOS on Apple silicon, Linux x86-64 and Linux ARM64. On Windows every build is cargo's. Intel Macs are not supported.
- **One Rust version, the latest nightly.** blaze builds with the dated nightly it pins
  (`nightly-2026-10-25`) whatever toolchain the project pins. Stable code and `#![feature(...)]`
  code both build; code that needs what that nightly lacks, such as a feature it removed, fails
  with rustc's error (see [install.md](install.md#what-you-need) and
  [support.md](support.md#toolchain)).
- **Not every build is blaze's.** The changes listed above go to cargo, and so does code blaze
  can't compile yet. `cargo blaze status` counts both.
- **Errors.** An edit with a type error fails to build, as it would under cargo. The borrow checker
  is new: 0.1.0 has none, so an edit with a borrow error builds and runs; the one on `main` finds
  about two thirds of rustc's borrow errors. Keep `cargo check` or your CI as the final word.
- **Memory and disk.** The daemon holds several times the memory cargo uses while building, and a
  project's build keeps more on disk than cargo's does.
- **Correctness.** Every change to blaze is checked by comparing what it builds with what rustc
  builds, on rustc's own tests and on real projects, but it is young: if a program behaves
  differently under blaze, that's a bug, and an
  [issue](https://github.com/indexable-inc/blaze/issues) is welcome.

What is supported in detail: [support.md](support.md).
