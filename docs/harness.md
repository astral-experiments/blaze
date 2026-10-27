# The harnesses

ra-jit is tested by comparison with rustc. Almost every script under `harness/` builds
something with cargo and rustc, builds it again with ra-jit's code in place of rustc's for
some crates, runs both, and compares standard output, standard error and exit status. A
difference is a failure. So is a function ra-jit refused, in the harnesses that count them.

There are also Rust unit tests in `crates/ra-jit` and in the code generator; the harnesses
are what carries the weight.

## Running them

You need Python 3, the pinned nightly (`harness/toolchain`, `nightly-2026-10-25`) with
`rust-src`, a C compiler and linker, and a release build of ra-jit. Some harnesses need
more: `debuginfo.py` needs gdb and `llvm-dwarfdump`,
`buildmodel.py` needs the `aarch64-unknown-linux-gnu` and `x86_64-unknown-linux-musl`
targets with their cross tools and qemu, `remote.py` needs a MinIO or SeaweedFS binary,
`storefull.py` needs passwordless `sudo` to mount a tmpfs.

    cargo +"$(cat harness/toolchain)" build -p ra-jit --release
    cd harness
    export RA_JIT_BIN=$PWD/../target/release/ra-jit

`RA_JIT_BIN` names the ra-jit under test and defaults to `../target/release/ra-jit`. The
scripts pick the toolchain from `harness/toolchain` unless `RUSTUP_TOOLCHAIN` is set. They
also default `RUSTFLAGS` to `-C link-dead-code`, which `cargo ra-jit` does not use, and
`CARGO_INCREMENTAL` to 0. Scratch directories go under `TMPDIR`; several of the harnesses
need a few gigabytes there.

### The gate

Luna selects regressions for pull requests and pushes to main. The full harness runs nightly
on Linux and macOS ([CI](#ci)).
`harness/ci.py` is the one list of it: which harness runs in which part, in which tier, on
which hosts and under which backend.

    python3 ci.py --list            # every harness, its part, its quick part, its environment
    python3 ci.py --parts           # the parts that have harnesses on this machine
    python3 ci.py --parts fast      # the quick shards on this machine
    python3 ci.py core              # run one part here, as CI does
    RA_JIT_BACKEND=llvm python3 ci.py test:cached-type-ids

A part runs all of its harnesses even when one fails, then names the ones that failed and how
long each took. Alongside the harnesses, CI runs `cargo fmt --all -- --check`,
`python3 harness/imports.py`, which imports every script of the harness, and
`python3 harness/unittests.py`, which runs the Rust unit tests and holds them to its `OPEN`
list (below). It runs cargo twice: the root workspace's crates, then the rust-analyzer fork's
(`hir-ty`, `hir-def`, `hir-ide` and the rest) in `vendor/rust-analyzer` (there, not with `--manifest-path`: cargo reads
`.cargo/config.toml` from where it runs, and the fork's sets `CARGO_WORKSPACE_DIR`, where
expect-test finds the parser's `.rast` files),
since the root excludes `vendor/rust-analyzer` so that `cargo install --git` builds ra-jit
(#293) and cargo only tests a member of the workspace it runs in. That workspace patches salsa
to `vendor/salsa*` as the root does, so the fork's tests run against the same salsa.

### Under Cranelift

Cranelift is the default. Either leave `RA_JIT_BACKEND` unset or set it:

    export RA_JIT_BACKEND=cranelift

### Under LLVM

Build with the `llvm` feature in the flake's dev shell, which has the LLVM 20 it pins,
then select the backend. The gate is run at `-O2` with fast instruction selection, and that is the
only LLVM configuration it covers.

    nix develop -c cargo build -p ra-jit --release --features llvm
    export RA_JIT_BACKEND=llvm RA_JIT_OPT=2 RA_JIT_EMIT=fast

A binary built with `--features llvm` still uses Cranelift unless `RA_JIT_BACKEND=llvm` is
set, so one build serves both runs. Under LLVM also run

    python3 backends.py corpus runner cases --bin runner --levels 2f

which reports how many functions LLVM compiled itself and how many it left to Cranelift,
with reasons. The daemon's log says `backend: llvm -O2 fast emit` when LLVM is in use.

## What each one checks

### Correctness of generated code

| Script | What it checks |
|---|---|
| `difftest.py <workspace> --bin <pkg> --crate <name>` | The base comparison. Builds the binary with cargo, compiles the named crates with ra-jit, links ra-jit's object ahead of rustc's rlibs, runs both. `--layouts` also compares the size and alignment of every type ra-jit laid out with rustc's `-Zprint-type-sizes`. `--bisect` links each ra-jit function in on its own to name the ones that change behaviour. `--mixed N` runs N builds with a random half of the functions left to rustc. `--memcheck` runs under valgrind |
| `harness/corpus/` | The workspace `difftest.py corpus` runs: a library with one module per area of the code generator and a runner that calls every function and prints the result |
| `breakers.py` | Every reduction under `harness/breakers/`, each run on its own so a crash in one does not hide the others. These are minimal cases of bugs found in real crates, rustc's tests and the fuzzer |
| `unwinds.py` | Edits to functions a panic unwinds through |
| `growth.py` | That the code of one function grows in a straight line with its source: the `serialize` `#[derive(Serialize)]` gives an internally tagged enum, a `?` in every arm, at 160 and 320 arms (issue #5); and a `load` filling a struct of 200 and 400 fields, each a call's `?` while a value with a destructor is live, as x11-dl opens Xlib's 781 functions (the way out of each `?` once dropped every field before it on its own: 2,986,013 blocks, and 364 GB for bevy) |
| `asmedits.py` | Edits to `asm!` blocks and the code around them; with `RA_JIT_ASSEMBLER=cc`, assembled by the C compiler, as a build without LLVM does |
| `answers.py` | That ra-jit's own answers to rustc's questions (`-vV`, `--print cfg`, cargo's probe of file names, `target-spec-json`) are rustc's, byte for byte, on every target ra-jit has them of; `--write` regenerates them from the pinned rustc ([no-rustc.md](no-rustc.md), step B) |
| `scripts.py [name ...]` | Build scripts run by `crates/blaze-build-script` against the pinned nightly's cargo's runs of them, over the applications of `apps/apps.json` and `harness/scripts/` (openssl-sys, zstd-sys, libz-sys, libgit2-sys, ring, rustix and the scripts that read their metadata): for each run, the environment blaze computes against the one cargo gave, blaze's reading of cargo's output against cargo's own, and a run by blaze against cargo's. `--json` writes every difference ([no-rustc.md](no-rustc.md), step I) |
| `toolchain_pins.py` | That the sha256 and size of each file of blaze's own Rust (`cargo/toolchain/pins.txt`) are what the release's manifest at static.rust-lang.org says, for every host blaze ships for; `--write` regenerates them when blaze moves to another release ([cargo.md](https://github.com/astral-experiments/blaze/blob/1a855cab876f5c092ca2b411354bd66c3ff3c44d/docs/cargo.md)). Needs the network |
| `norustc.py` | How often rustc and rustdoc run when `harness/norustc/fixture` is built, edited, tested, doctested and checked with `cargo ra-jit`, through a copy of the toolchain that logs every run; a count above `norustc/baseline.json` fails ([no-rustc.md](no-rustc.md), step A). `--record` lowers the baseline |
| `fuzz/fuzz.py` | Generated programs, see below |
| `uitest.py <rust checkout>` | rustc's own `tests/ui` run-pass tests. Every file is either run or left out with a counted reason |
| `libsuite.py <rust checkout> coretests\|alloctests\|stdtests` | The standard library's test suites, cut down to what builds on stable |
| `cratetest.py <crate-dir>` | A crate's own integration tests: each test binary is relinked with ra-jit's objects and every test must report the same result |
| `cratesweep.sh`, `vendor.sh`, `sweepreport.py` | `cratetest.py` over a list of crates.io releases, and a summary |
| `bintest.py <name>` | A real program from `breakers/bins.json`: the same command lines given to cargo's binary and ra-jit's |
| `alone.py` | A program whose workspace crates rustc compiles nothing of, so no refused function can hide behind rustc's copy |
| `allrajit.py [whole] [unit] [stored]` | Programs with nothing rustc built in them, the standard library compiled by ra-jit ([no-rustc.md](no-rustc.md), steps F and G): a hello and one of collections, panics, threads and channels, each built with the library in the same build, over the library built alone (`--prebuilt`), and on a store and a remote that start empty, where the first build compiles and keeps the library and the later ones, one on a second store, take it. Each has to print and exit as cargo's build does. Linux |
| `macroserver.py` | The release's proc-macro server (`vendor/proc-macro-srv`) built by ra-jit, and the libraries ra-jit links of serde_derive, thiserror-impl, tokio-macros and clap_derive ([no-rustc.md](no-rustc.md), step J1): every call of a package's derives (with helper attributes), attributes and function-like macros is expanded by the toolchain's server with rustc's library and by ra-jit's with ra-jit's, and the outputs have to be equal, spans included; the package is then built whole with its proc macros by rustc and by ra-jit. `macroserver.py <app>` (or `all`) builds a bench app (fd, bat, hyperfine, just, nushell, helix) whole with `RA_JIT_PROC_MACRO_SERVER`, so ra-jit links every proc macro, again into an empty target directory over that build's store (no library may be linked again), and again without, expanding every call by both servers; the counts, the calls not compared and why each proc macro was rustc's are printed, and both programs have to print the same. Needs crates.io. Linux |
| `frozenwarm.py` | Frozen def maps (#40, #161) of a dependency whose items, and a `macro_rules!` it exports, are in files `include!` reads and the VFS does not load at first, as the icu `*_data` crates': built on one store cold, warm twice, warm with `RA_JIT_FROZEN=check` and `=verify`, and warm after an included file changed at the same path, which has its def map refused. Each has to print what cargo's build does. Linux |
| `wholeprogram.py` | Programs ra-jit builds whole, with its own `main` (as `runbench.py` builds them): one on the default allocator, one with a `#[global_allocator]` of its own, one with a library's, one whose allocator is under a `#[cfg]` that is false. Each has to print and exit as cargo's build does; `--release` builds both in the release profile |
| `bun_repros.py` | Nightly fixtures, one per thing bun's whole-program build found ra-jit doing wrong (`harness/bun-repros/`: `adt_const_params`, `core::mem::type_info`, macros, exported statics, frame pointers, …). Each compares output with cargo's binary and checks the report's refusals against the case's. A case can set its own `rustflags` and `env` (the frame-pointer case runs LLVM), and `alone` builds it as runbench builds bun, the link taking only ra-jit's objects. It builds with the pinned nightly of `harness/toolchain`, as everything else does. |
| `backends.py` | One program compiled by each code producer: counts, time per function, and what LLVM left to Cranelift |
| `lints_ui.py <rust checkout>` | The lints rust-analyzer finds at the level a source denies them, against rustc's UI tests that `deny` or `forbid` one: for each lint, the errors (lint and line) ra-jit reports that rustc does too (right), that rustc does not (wrong) and that only rustc reports (missed). A lint ra-jit fails builds with (`DENIED` in main.rs) has none wrong; one in a macro's expansion is not counted, as it fails no build. It scores against the pinned nightly's rustc; `--toolchain nightly-YYYY-MM-DD` against another. `lints_ui.py harness/lints_probes` scores the units written for the lints with few UI tests, one for each place a lint applies. `--deny LINT` scores one lint on every UI test, each read with `#![deny(LINT)]` first, for a lint few tests deny (`unused_mut`) |
| `borrowck_ui.py <rust checkout>` | The borrow checker ([borrowck.md](https://github.com/astral-experiments/blaze/blob/1a855cab876f5c092ca2b411354bd66c3ff3c44d/docs/borrowck.md)) against rustc's borrow-check UI tests: each unit passes when ra-jit reports the errors (code and line) the pinned nightly rustc reports, under Polonius and NLL. `--min-pass N` fails below N Polonius passes; a panic, or an error on a unit rustc accepts, fails it too |
| `scoreboard.py` | Runs the targets in `targets.json` and fails when behaviour differs or coverage drops below the committed `scoreboard.json` |
| `mirparity.py <workspace> --crate <name> [--level N] [--rustc-level 1\|2]` | ra-jit's MIR (`ra-jit mir`, at `RA_JIT_MIR_OPT` level N) against rustc's `-Zunpretty=mir`, function by function: the share that match in shape, statement counts, statements and types, and the most frequent differences. Not a gate: the numbers are in [research/mir-gap.md](https://github.com/astral-experiments/blaze/blob/1a855cab876f5c092ca2b411354bd66c3ff3c44d/docs/research/mir-gap.md) |

### Reading the workspace as cargo does

| Script | What it checks |
|---|---|
| `resolve.py [bench\|crates\|workspaces]` | `crates/blaze-manifest` against the pinned nightly's cargo (or `CARGO_ORACLE`) on `cargo metadata --no-deps --format-version 1`: the projects of `bench.py`, 300 crates.io crates and 72 workspace repositories at pinned commits (`resolve/corpus.json`), the JSON compared as text after the root and `CARGO_HOME` are replaced, key order included, and the error text when both refuse a workspace. Prints the first difference of each workspace and the counts per part; [resolve.md](https://github.com/astral-experiments/blaze/blob/1a855cab876f5c092ca2b411354bd66c3ff3c44d/docs/resolve.md) has them. `resolve.py DIR...` compares the workspaces at DIR... only. Needs the network |
| `resolve.py config` | The configuration as the pinned nightly's cargo merges it: `cargo -Zunstable-options config get` against blaze-manifest's `config` example, seven ways each, on the trees of `crates/blaze-manifest/tests/config-fixtures` and the root of every bench project and workspace repository; exit code, stdout and stderr compared as text |
| `resolve.py profiles` | Profiles as the pinned nightly's cargo gives them to units: the `profile` of every unit of `cargo <command> --unit-graph -Zunstable-options` (`RUSTC_BOOTSTRAP=1`) against blaze-manifest's `profiles` example given the same graph, for `build`, `build --release`, `test`, `bench`, `check` and a `--target` that is not the host, on the trees of `crates/blaze-manifest/tests/profile-fixtures` and every bench project and workspace repository; the warnings about profiles and cargo's error when it refuses them compared too |

The workspaces of `crates/blaze-manifest/tests/fixtures` (inheritance, members and targets
discovered, a package outside its workspace's members, config and manifest errors), the
config trees of `tests/config-fixtures` and the profile trees of `tests/profile-fixtures` (with
the unit graphs cargo printed) have cargo's output committed next to them, so
`cargo test -p blaze-manifest` checks them without cargo or the network; `resolve.py --fixtures`
writes them again.

### The daemon following edits

| Script | What it checks |
|---|---|
| `stress.py` | 100 edits of the kinds people make (bodies, signatures, new items, moved lines, broken code, reverts, an editor's edit while another client's build runs) applied in order to a three-crate workspace under one warm daemon, each compared with `cargo build` of the same source. The first edit cargo builds after broken code has to be a patch of the build before it ([#31](https://github.com/indexable-inc/ra-jit/issues/31)), and the edit sent during a build has to be taken ([#32](https://github.com/indexable-inc/ra-jit/issues/32)); it exits 1 when a step is wrong |
| `programs.py` | Two programs of one workspace built by one daemon. With `RA_JIT_ALONE=1` rustc builds stand-ins for the workspace's crates and the daemon compiles every function of both programs, `main` too: under LLVM an edit of what both use has to reach each ([#63](https://github.com/indexable-inc/ra-jit/issues/63)) |
| `worktrees.py` | Several checkouts of one workspace built by one daemon, edited independently |
| `includes.py` | Edits to a file only `include!` names |
| `latelisted.py` | What a file named by `include!` through a build script's variable declares is listed in its crate though the daemon listed the crate beside cargo's build before it read the file, as bun's generated bindings are: an edit that adds a function then patches a few functions, not every one the file declares |
| `aheadready.py` | A function the daemon compiles ahead while it reads the symbols rustc built (held with `RA_JIT_SYMBOLS_PAUSE_MS`, each such function made to take ten minutes with `RA_JIT_AHEAD_PAUSE_MS`) is given up on once they are read: the daemon is ready within two minutes of the build, and the builds after it, an edit's too, are its own and run as cargo's. bevy's derived `release_state` of a read-only query struct took two minutes and held the second warm-store run's readiness to 148 s. Linux and macOS |
| `rustsrc.py` | A toolchain without `rust-src` is turned down, saying how to install it |
| `splices.py` | Edits that are hard on the splice: thread-locals, statics, vtables, function pointers, closures, panics, foreign calls. Counts how each step was linked |
| `farsplices.py` | Splices into a binary whose code is further from its end than an AArch64 branch reaches |
| `farfile.py` | Edits of a program whose file is 2.3 GB of a section nothing loads, as a debug build of bevy's is mostly debug info: bodies, a caller, a new function and taking it back each have to be spliced, which a patch placed as far from the code as it is behind the file is not ([#79](https://github.com/indexable-inc/ra-jit/issues/79)). Linux |
| `bigbinary.py` | Edits of a program larger than any place of the splice is sized for: 220 MB of code, over 16 MB of frames the unwinder reads from `__eh_frame` or `.eh_frame`, and on Linux 4.2 GB of `.debug_abbrev`. A function shorter than the long jump, one that panics through its replacement, 3,000 functions added and a new function each have to be spliced ([architecture.md](architecture.md#no-size-is-a-reason-to-link-in-full)). Nightly, Linux and macOS |
| `unreached.py` | Edits under `cargo ra-jit` that call instances of another crate's generic function the executable did not reach when it was linked: each is spliced, not linked in full. The daemon's own links, with `RA_JIT_RUSTC_BASE=0`. Linux and macOS; on Windows `cargo ra-jit` hands builds to cargo |
| `anonstatics.py` | Edits under `cargo ra-jit` that reach, through code rustc inlined at opt-level 1, a `static mut` inside a `const _: () = { .. }` of a crate rustc built (the shape of objc2's `declare_class!` ivar offsets, which winit's frame function reads): the daemon serves each build and refers to rustc's static, found under its `_` segment. Linux and macOS; on Windows `cargo ra-jit` hands builds to cargo |
| `renamedstatics.py` | Edits under `cargo ra-jit` at opt-level 1 of a function that reads a static with state which rustc's allocator shim writes, and which LLVM renamed `SERVING.0` in rustc's object (a hidden symbol in an incremental build; with `--not-incremental`, one private to the object): the program prints what rustc's code wrote, so the daemon's code shares rustc's copy (made linkable on macOS) or, where it can't (Linux, private), leaves the code that refers to it to rustc. It first checks that rustc's build has the renamed symbol. Linux and macOS |
| `loadedits.py` | A file written while a worker loads, after the worker read it and before it took its timestamp (held there with `RA_JIT_LOAD_PAUSE_MS`), is built as written: the next build, and the one after an edit of another crate, print the new text ([#53](https://github.com/indexable-inc/ra-jit/issues/53)). Linux and macOS |
| `agents.py` | Random edits of the kind coding agents make, on a real workspace |
| `invalidation.py` | How much the database redoes after different kinds of edit |
| `testselect.py` | Checks which unit and integration tests run after edits under `RA_JIT_TESTS=affected`, including changes in a workspace dependency, new or ignored tests, and failures. File readers and writers run on every invocation, and the writer's changes persist in the package under both `affected` and `all`. Linux and macOS |
| `doctestonce.py` | `cargo ra-jit test --doc` on a workspace of three libraries against `cargo test --doc`: the workspace is loaded once for all of them, each library's results, `Doc-tests` line and cargo's lines of what failed are cargo's (every example passing, two failing with and without `--no-fail-fast`, one failing with it), and each example reads the directory, variables and dev-dependencies it has under rustdoc. Linux and macOS (where std's writable statics still leave the examples to rustc, #255) |
| `movedlines.py` | A panic reports its new line after lines are added above its function (seven ways to panic, each against cargo), and moving a function compiles nothing |
| `buildmodel.py` | One small cargo project per feature of cargo's build model, under both backends, with steps that change one input at a time |
| `cargo_ra_jit.py` | `cargo ra-jit` against plain cargo on seven real projects, 22 flag combinations, and 11 daemon lifecycle checks. Needs the network and about 20 minutes; not in CI |
| `debuginfo.py` | Backtraces, `llvm-dwarfdump --verify`, gdb stopping and stepping, locals |
| `live_e2e.py` | Edits of a running program that links `ra-jit-live`, each applied as a patch; then another program built meanwhile, a start again from the executable, and a panic that unwinds through the patched code. Linux x86-64 and macOS AArch64 |
| `live_fallback.py` | A workspace of two programs built together, where an edit of the library both use is a patch of the running one and has cargo build the other (`main` left to rustc calls rustc's copy of the edited function): the running program is not told to restart, the other prints the new answer, and the next edit of the running program's own code is a patch too. Linux x86-64 and macOS AArch64 |
| `live_types.py` | A running program's component types changed as a game changes them, on the daemon's own base: `V2` added beside `V1` with a derive, a static id cache and a trait impl, the code moved to it with the values converted and a static new in the edit, `V1` deleted, each a patch whose answer matches rustc's build and whose statics keep their values; then a field of `V2` changed, which restarts naming `struct V2`. Linux x86-64 and macOS AArch64 ([live.md](https://github.com/astral-experiments/blaze/blob/1a855cab876f5c092ca2b411354bd66c3ff3c44d/docs/live.md#types)) |
| `live_latency.py` | Edit-to-running time of live edits on `harness/live`, `demo/life`, `harness/live-bevy` and `harness/live-egui`: isolated edits, bursts of valid keystrokes, real typing replayed key by key (most texts in between do not compile; a program told to restart is started again), and a session's restarts against `cargo run`. Writes p50 to p99 and every sample, with the machine and its battery, to `LATENCY_JSON`. Measures; checks nothing; macOS on AArch64 ([live.md](https://github.com/astral-experiments/blaze/blob/1a855cab876f5c092ca2b411354bd66c3ff3c44d/docs/live.md#latency-under-typing)) |
| `fncache.py` | `#[ra_jit::cache]` ([fn-cache.md](https://github.com/astral-experiments/blaze/blob/1a855cab876f5c092ca2b411354bd66c3ff3c44d/docs/fn-cache.md)): a cold run computes and a warm one finds the result on disk; comment, format, moved-line and unrelated edits keep it, an edit of a function it reaches through another drops it; generic instances are kept apart; impure functions are refused saying where; a restarted daemon finds the same results; `cargo ra-jit cache` lists them by function and `clear --stale` forgets the older code's alone; cargo's build caches nothing. `--live`: a live edit computes again and drops the old results, macOS on AArch64 |
| `spaces.py` | A two-crate workspace under a directory whose name has spaces, `$$`, `#` and quotes, built and edited with `cargo ra-jit build`; every edit has to be applied by the daemon against rustc's symbols ([#14](https://github.com/indexable-inc/ra-jit/issues/14)) |
| `rustcbase.py` | The first edits after a cold `cargo ra-jit build`, spliced into the executable rustc linked ([#11](https://github.com/indexable-inc/ra-jit/issues/11)), against plain cargo: bodies, closures, a dependency crate, function-local statics, two edits in a row, a new function of the program's crate, also in a package with a library it uses, a body of a program whose library has its name, and a `#[ra_jit::cache]` function and what it reaches have to be patches, also a function that hands `iter.map(Ok)` to a library asking its `TypeId` through `dyn Any`, as axum's `Body::from_stream` does (`typeid-ctor`, [#369](https://github.com/indexable-inc/blaze/issues/369)); generic, `#[inline]` and default trait functions, statics, a new function of a crate another depends on or of a name the crate already writes, other new items, `async`, `impl Trait` and `include_str!` edits have to be built in full and say why; and the daemon's own base, built while nothing waits, takes the edits after it; an edit that comes as it builds is a patch of rustc's executable, or of the base when it had begun to link ([#79](https://github.com/indexable-inc/ra-jit/issues/79)), also when a crate it loads includes a file no build script wrote (`unbuilt-member`), and is given up once it holds more memory than `RA_JIT_OWN_BASE_MEMORY` (`own-base-budget`), after which an edit built in full is spliced into rustc's executable and the patches after it chain onto that (`own-base-budget-full`, [#159](https://github.com/indexable-inc/ra-jit/issues/159)). On Linux each build in full of the scenarios is spliced whole into what rustc linked, not linked. Every edit's output and exit status against cargo's, macOS and Linux |
| `nocompact.py` | A program linked with `-no_compact_unwind`, as bun is, which has `__eh_frame` and no `__unwind_info`: its edits are spliced, and the unwinder finds the frame of the edited function where its symbol now is, behind the program's frames and, once a thousand new functions outgrow that room, with all of them moved into the header's ([#33](https://github.com/indexable-inc/ra-jit/issues/33)). macOS on AArch64 |
| `headerpad.py` | A program whose unwind table does not fit the room its link leaves after the load commands: the first link is done again with room, the edits after it are spliced, and a restarted daemon starts from the room kept beside the executable ([#13](https://github.com/indexable-inc/ra-jit/issues/13)). It is built for macOS 14, as nix's shell builds, which Apple's linker gives chained fixups unless told not to ([#30](https://github.com/indexable-inc/ra-jit/issues/30)). The daemon's own links, with `RA_JIT_RUSTC_BASE=0`. macOS on AArch64 |
| `tiers.py` | Tiers ([tiers.md](https://github.com/astral-experiments/blaze/blob/1a855cab876f5c092ca2b411354bd66c3ff3c44d/docs/tiers.md)): `demo/lifebench` running under the daemon reaches levels 2 and 3 with output identical to cargo's, an edit of a tiered function demotes it and is applied at once, and `RA_JIT_TIER=off` changes nothing. macOS on AArch64 with `--features llvm`; it exits 0 elsewhere |

### The store and the remote

| Script | What it checks |
|---|---|
| `keys.py` | Whether the store ever hands a build code compiled from something else. One case per thing code depends on besides its file's text |
| `storekills.py` | The daemon killed at random moments while writing its store |
| `storecorrupts.py` | Packs damaged on disk, and stores of other formats |
| `storefull.py` | A store on a disk with no room |
| `storeshared.py` | Several daemons and `ra-jit cache` commands on one store at once |
| `storerevisions.py` | Fresh daemons sharing a store across edits. Compares Cranelift and LLVM cache reuse after body, inline, signature and call-target changes with fresh Cargo builds. On LLVM, it also inspects the caller’s machine code and edits its helper in a running daemon |
| `storebuilds.py` | A store one build of ra-jit wrote serves another build of the same sources: the store keys by what ra-jit was built from, not by the bytes of its executable ([#105](https://github.com/indexable-inc/blaze/issues/105)). Linux and macOS |
| `deepnames.py` | Closures nested 20 deep keep the store under 64 MB, and a second daemon finds their entries again: a type spelled out longer than 1 KB is named by its hash (issue #29) |
| `expansions.py` | A second daemon on one store finds every derive and attribute expansion the first kept, and after an edit, with `RA_JIT_PROC_MACRO_CACHE=check`, none expands otherwise than it was kept (issue #38) |
| `remote.py`, `s3fake.py` | A store shared by two machines through an S3 service, with the transfer broken, objects spoiled and manifests forged |
| `packs.py` | Not a check: the sealed pack's layout ([cache.md](https://github.com/astral-experiments/blaze/blob/ceab778b050be70b896ed84c7c536b11ce8f2971/docs/cache.md)) read and written, for `remote.py`, `storeguards.py` and `bench.py` |
| `deps.py` | cargo's builds of registry crates pushed and pulled. Builds offline from cargo's registry cache, so it does not run on a clean CI machine |

### Timing

Run `abbench.py` to compare two compiler builds on the same edits, or
`live_latency.py` to measure how quickly edits reach a running program. Each script
documents its inputs and how to reproduce a run. The correctness suites also use
`bench.py` for project setup and `runbench.py` for whole-program builds.

## The fuzzer

`fuzz/fuzz.py` builds generated programs with cargo and with ra-jit under each backend and
compares what they print. `fuzz/fuzz.py smoke` is part of the gate. How to run longer
runs, what a finding is and what the fuzzer does not cover are in
[fuzzing.md](https://github.com/astral-experiments/blaze/blob/1a855cab876f5c092ca2b411354bd66c3ff3c44d/docs/fuzzing.md).

## Adding a corpus case

The corpus is for things the code generator must handle that are small enough to state as
a function. Add a `pub fn` to the module for its area in
`harness/corpus/cases/src/lib.rs`, or a new `pub mod`. Call it from
`harness/corpus/runner/src/main.rs` inside `case!(...)`, which prints the expression and its
value. The result has to be deterministic and `Debug`. Then run

    python3 difftest.py corpus --bin runner --crate cases --layouts

It must print `MATCH`, and the count of functions from ra-jit must go up by what you added,
with none left to rustc.

## Adding a breaker

A breaker is the reduction of a bug found somewhere else. Add a file
`harness/breakers/cases/src/<name>.rs` with a `//!` comment at the top that says where the
bug was found and what the cause is, and a `pub fn run()` returning something `Debug`. Add
`<name>` to the `cases![...]` list in `harness/breakers/cases/src/lib.rs`. Then

    python3 breakers.py <name>

A breaker should fail before the fix and pass after. If you are recording a difference you
are not fixing, add the name to `OPEN` in `breakers.py`, or to `LEFT` if ra-jit refuses the
function and says so. Both lists are the project's list of known wrong or missing
behaviour, so keep the comment in the case honest about why it is open.

A bug that only shows in a crate's own tests goes in `CRATES` in `breakers.py` as a small
crate under `harness/breakers/`.

## Reproducing a failure

Start from the script's last lines. Each daemon harness prints the path of the daemon's log
and of the scratch copy of the source it was editing, for example

    99 of 99 right, 0 restarts; log /tmp/ra-jit-stress-out-XXXX/daemon.log; source /tmp/ra-jit-stress-XXXX

and the stepwise ones take `--only TEXT`, `--from N` and `--keep` (see each docstring).

For a difference in generated code:

1. Run `difftest.py` on the workspace with `--bisect`. It names the functions whose ra-jit
   version changes the output, with the first differing line.
2. `ra-jit --report FILE.tsv` writes the outcome of every function (compiled, skipped and
   why). `--deny FILE` leaves the functions listed in a file to rustc, and `--isolate DIR`
   writes one object per function.
3. `RA_JIT_DUMP_MIR=<function name>` prints the MIR of functions with exactly that name
   (`{closure}` for closures). `RA_JIT_DUMP_LLVM=1` prints the LLVM module of every function
   compiled, and `RA_JIT_DUMP_CLIF=1` the Cranelift IR of drop glue.
4. Reduce it to a function, add it as a breaker, and fix it there.

For a fuzzer finding, `fuzz.py repro SEED-ROUND` rebuilds the program and
`fuzz.py check found/FILE.rs` runs a stored reduction.

For a CI failure, the last line of the harness step lists which scripts failed.

## CI

`ra-jit.yaml` selects builds and regression tests for each pull request and main push.
The CI plan artifact records the platform, backend, and reason for each selection.
Compiler changes run formatting and Linux x86-64 unit tests. The `fork` job exercises
the vendored semantic and diagnostic crates in their own workspace. The `live` job
tests edits to a running program. The `ci` job aggregates the selected results.

The fast tier runs on demand, and the full tier runs nightly or on demand. Use
`gh workflow run ra-jit.yaml --ref <branch> -f tier=fast` or `-f tier=full`.
`claims.yaml` compares upstream rustc UI cases and checks the real-crate scoreboard.
`fuzz.yaml` compares generated programs with rustc. For an Astral workspace,
dispatch `downstream-tests.yaml` with `ruff-and-ty`, `uv` or `uv-archive-fixtures`.
