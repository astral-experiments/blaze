# Using blaze with cargo

There are two ways to use blaze from cargo. `cargo blaze <command>` is an explicit
subcommand, described here. The second, switching blaze on globally so that plain `cargo`
goes through it, has its own section at the end.

blaze was called ra-jit until 2026-10-24. Below this section the guide still says
`cargo ra-jit`, which is the same command as `cargo blaze` and keeps working; the programs and
the daemon's messages keep the name ra-jit for now.

Everything in this file was checked against the code in `crates/ra-jit/src/cargo/` and run on
Linux x86-64. On macOS arm64 `harness/cargo_ra_jit.py --small` was run, and what is known of
it is under [macOS arm64](#macos-arm64). The design notes it was written from are in
[reports/CARGO.md](reports/CARGO.md).

## Install

`cargo blaze` is cargo's way of running a program called `cargo-blaze` on your `PATH`. That
program is built with blaze and runs the `ra-jit` executable in the same directory, blaze's
engine, so the two have to be installed side by side, with `ra-jit-rustc` and
`ra-jit-rustc-first` (and `cargo-ra-jit`, the old name of `cargo-blaze`). The
[installer](../install.sh) and `cargo install` both do that:

    curl -fsSL https://raw.githubusercontent.com/indexable-inc/blaze/main/install.sh | sh
    cargo +nightly-2026-10-25 install --locked cargo-blaze

From the release after 0.1.0, `--profile dist` builds the profile a release is built with, less
the PGO a release adds ([reports/RELEASE-BUILD.md](reports/RELEASE-BUILD.md)); 0.1.0 has no such
profile and fails with it. From a checkout, the release's own recipe builds all of it, with the profile
[pgo/profiles.json](../pgo/profiles.json) pins:

    python3 .github/scripts/build_release.py --target $(rustc +"$(cat harness/toolchain)" -vV | sed -n 's/^host: //p') --out shipped
    cp shipped/* ~/.cargo/bin/

That build has the Cranelift backend only. The LLVM one (`BLAZE_BACKEND=llvm`) is
`--features llvm` on either command, run inside `nix develop`, which has the LLVM 20 the flake
pins ([LLVM harness setup](harness.md#under-llvm)). The script reports when the selected
features differ from those used to train the profile. `cargo build -p ra-jit --release` is the quicker build the harnesses and CI use while
working on blaze; it is not what ships.

`cargo blaze` builds with the one toolchain blaze compiles, the dated nightly in
`harness/toolchain` (`nightly-2026-10-25`), whatever toolchain the project names (a
`rust-toolchain.toml`, `rustup override`, `+toolchain`, `RUSTUP_TOOLCHAIN` or rustup's
default): it runs cargo, rustc and the daemon under that nightly by naming it in
`RUSTUP_TOOLCHAIN` and `CARGO`. The nightly builds what older releases built, so a project on a
stable release or an older nightly builds as it is, `#![feature(...)]` included. Code only the
project's own toolchain builds (a feature the pinned nightly removed, a newer nightly's
library) fails with rustc's error and a note naming both toolchains. Plain `cargo` still builds with the project's toolchain, in the same target
directory: cargo keys what it builds by the rustc, so neither rebuilds the other's.
`BLAZE_OTHER_TOOLCHAIN=1` builds with the project's toolchain instead
([support.md](support.md#toolchain)).

Where the nightly comes from, in this order: `RUSTC` naming a rustc whose `rustc -V` line is
the pinned one's (`rustc 1.101.0-nightly (12433c307 2026-10-24)`: the whole line, since every
nightly of a release says `1.101.0`) with its library's source; rustup's `nightly-2026-10-25`
with `rust-src`, when rustup has it; else blaze's own copy in
`~/.blaze/toolchains/nightly-2026-10-25-<host>` (`BLAZE_HOME` moves `~/.blaze`), laid out as rustup lays
out a toolchain. blaze downloads its copy the first time it needs it, once, with a line on
stderr while it does: rustc, cargo, the standard library and its source from the Rust project's
own servers, 97 MB on macOS, 134 MB on Linux x86-64 and 118 MB on Linux ARM64 (548 MB on macOS
and 710 MB on Linux x86-64 once unpacked). Each file's sha256 is built
into blaze, from the nightly's published manifest (`harness/toolchain_pins.py` writes them into
`crates/ra-jit/src/cargo/toolchain/pins.txt`), and a file with other bytes is refused. Concurrent
builds download it once; a download that stopped goes on from where it stopped the next time.
Building with blaze's copy, blaze names its cargo, rustc and rustdoc in `CARGO`, `RUSTC` and
`RUSTDOC` and puts its `bin` first on `PATH`, for the commands it runs and not for your shell.
`RA_JIT_RUST_DIST` names a mirror of `https://static.rust-lang.org/dist` (or a directory with
the files), for a machine that cannot reach it. `cargo blaze toolchain` (or `blaze toolchain`)
says which Rust blaze builds with, downloading its copy first when there is none.

Every setting below named `RA_JIT_<NAME>` can be given as `BLAZE_<NAME>` too; when both are
set, the `BLAZE_` one wins.

## Commands

    cargo blaze build [cargo's flags]
    cargo blaze run   [cargo's flags] [-- program arguments]
    cargo blaze test  [cargo's flags] [test names] [-- test arguments]
    cargo blaze check [cargo's flags]
    cargo blaze status
    cargo blaze stop
    cargo blaze toolchain

`blaze <command>` is the same as `cargo blaze <command>`, for a machine where blaze brought
its own Rust and there is no `cargo` on the path.

`build`, `run` and `test` take the flags cargo takes and leave the same files in the same
places under `target/`. `check` is what the daemon says of the source as it builds the
programs of the build with the same flags, when it has that build loaded: the errors a build
would fail with, or `Finished`, and no rustc. Any other check is the daemon's too: it loads
the workspace as a build of those flags would, builds nothing, and says what it finds in every
file of the workspace's crates. The build scripts and proc macros are the ones cargo built
before; a workspace that never had them built has rustc build them first. A check takes
`--message-format` (each of cargo's values), `-Z`, `--exclude` and several `--target`s, a
check for each; in JSON its errors are rustc's `compiler-message`s, with their spans,
`rendered` and the failure notes, then `build-finished`, which is what rust-analyzer's
check-on-save and other editors read, so they show the daemon's verdict. What only rustc writes
is not there: the labels and the help and notes of an error, its other spans, and
`code.explanation`; the message is rust-analyzer's, worded otherwise than rustc's. E0061's
span is where the missing argument goes, not the function called. `harness/check_json.py`
compares it with `cargo check` on the same errors. `status` prints what the daemon holds, and for every workspace how many builds
the daemon served and how many went to cargo, with the reasons. `stop` ends the daemon. Any
other subcommand is passed to cargo as typed.

## What a build does

The first build of a workspace is cargo's own, run with ra-jit as the
`RUSTC_WORKSPACE_WRAPPER`. The wrapper runs rustc exactly as cargo asked and records, for
each crate of the workspace, the arguments, the files rustc wrote and, for a program, the
line the linker was run with. Diagnostics, progress and the exit status are cargo's.

The daemon is started before cargo, if it is not running, and loads the workspace while cargo
builds, with what cargo says of the build scripts it runs and the proc macros it builds, kept in
`target/ra-jit/<build>/scripts`: none of them is built or run again for it, in
`target/rust-analyzer` or anywhere else. When cargo's build succeeds, the command waits for the
daemon to have the workspace loaded and to build the programs from it, and those are the build's,
as they are of every build after it:

         Waiting for blaze to load the workspace
        Finished `dev` profile [ra-jit] target(s) in 41.20s

A program the daemon does not build then stays cargo's, and the build says why. A build asked
for while the daemon is still loading, as after it was stopped, waits for it the same way.

Once the daemon is ready, a build after an edit does not start cargo at all. The daemon
compiles the functions that changed and either splices them into the executable or links
again, and the result is put where cargo would have put it. The status line shows which path
a build took:

    Finished `dev` profile [ra-jit] target(s) in 0.24s

`[ra-jit]` means the daemon built it. A cargo build prints cargo's usual line.

With `RA_JIT_WHOLE_BUILD=1` (or `BLAZE_WHOLE_BUILD=1`), `build` and `run` from nothing are
blaze's own instead of cargo's ([no-rustc.md](no-rustc.md), step K): each program the build
asks for is built whole, every crate it depends on compiled by blaze over the shipped standard
library and linked by blaze, and put where cargo puts it (`target/debug/<name>`,
`target/release/<name>`, `examples/` for an example). No `cargo build` runs. A function the
program can reach that blaze cannot compile yet fails the build, which says which and why;
nothing is handed to cargo:

       Compiling kapp v0.3.1 (/src/k/app)
        Finished `dev` profile [ra-jit] target(s) in 18.40s

       Compiling kuses v0.1.0 (/src/k/uses)
    error: `kuses` can reach functions blaze could not compile, so it is not built:
      `khalf::halved`: unsupported: f16 and f128

Build scripts and proc macros are still built and run as the daemon's load does
(`target/rust-analyzer`). A build of libraries alone and `test` are as without the setting.
The build is the daemon's: its worker loads the workspace, builds and links the programs, and
answers once they are linked. The next build asks the same worker, which builds what was
edited since on what it loaded and links the programs again; a changed manifest or lock file
has a new worker load the workspace.

`run` then runs the program and `test` runs the test executables the way cargo does: the
same working directory, the variables cargo sets, cargo's `Running` lines and the same exit
status. Every test runs, unless `RA_JIT_TESTS=affected` says otherwise, and every test runs
in a sandbox.

### Every test runs in a sandbox

"Everything should by default be in sandbox and yea it should have sandbox support macos"
(Andrew, 2026-10-26). Every test executable `cargo ra-jit test` runs, under `all` and
`affected` alike, runs in a sandbox of its own, with libtest's threads inside it as cargo would
run them. In it the machine's files are read-only; the workspace and the target directory are
as they are, but what a test writes there is kept to the run and gone when it ends; `/tmp`,
`/var/tmp` and `/dev/shm` (on macOS the temporary directory) start empty; there is no network
but the sandbox's own loopback; and the environment is fixed. A write outside gets the error a
read-only file system gives, and a connection to anything but loopback is refused.

The sandbox records what each test reached, by name: the harness tells it, before and after
each test, which test the thread is running. Of a test executable that fails, the note under
it names each failed test the sandbox refused something, and how to run it outside:

    note: tests::writes_home failed in the sandbox, which refused it writing `/home/me/.cache/x` (the machine is read-only in a sandbox); if it needs that, run it outside: `unsandboxed-tests = ["tests::writes_home"]` under `[package.metadata.blaze.sandbox]` in /home/me/tsel/Cargo.toml (`tests = false` for every test of the package), or `RA_JIT_SANDBOX=off` for every test of the command

A package takes its tests out of the sandbox in its manifest:

```toml
[package.metadata.blaze.sandbox]
tests = false                                 # every test of the package runs outside
unsandboxed-tests = ["tests::writes_home"]    # or these, by libtest's names
```

Any other key there is an error. The tests named run in a second run of the executable,
outside, after the sandboxed one (libtest's `--exact` and `--skip` split them); `-v` says
which and why. `RA_JIT_SANDBOX=off` runs every test of the command outside, as cargo does.

Where no sandbox can be made, `cargo ra-jit test` stops before any test runs, says why and how
to fix it, and exits 101. It does not go on outside with a warning: a run that did would break,
unseen, the promise that a test can't change the machine or reach the network, and a warning
in a long log is not seen. `RA_JIT_SANDBOX=off` is the explicit way to run outside.

On Linux the sandbox is user, mount, network, IPC and PID namespaces: the machine bound
read-only, overlays for the workspace and target directory, tmpfs for the empty directories, a
network namespace with only loopback up, and a seccomp filter whose supervisor reads the paths
asked for. It needs namespaces a user can make; Ubuntu's AppArmor stops them unless
`kernel.apparmor_restrict_unprivileged_userns` is 0, which the error says. A filter is only
installed when the run is recorded, and then notifies only writes, the network and the
harness's marks unless reads are recorded too (`affected`).

On macOS the sandbox is Seatbelt, applied with `sandbox_init_with_parameters` in the child
before it runs the program (as Nix does), and needs no root: a profile that allows everything
but writes (allowed only to the sandbox's scratch directory and a few of `/dev`) and the
network (loopback and Unix sockets only). An interposer library ra-jit builds and loads with
`DYLD_INSERT_LIBRARIES` records what is read, makes the workspace and target directory
copy-on-write (`clonefile`, whiteouts for removed files, merged listings) and gives the empty
temporary directory. Seatbelt alone can neither record reads without root (`fs_usage` needs
it, and `(with report)` logs nothing a user can read) nor give private writes, hence the
interposer. Its limits: a program macOS protects (`/bin/sh`, `/usr/bin/*`) is started without
the interposer, so what it reads is not known (the test then runs every time under
`affected`, and `-v` says so) and its writes to the workspace are refused rather than kept
private; a test that makes raw system calls, or tampers with the interposer in its own process,
gets past the recording and the private writes but not Seatbelt's read-only machine or its
network rule; a sandbox inside the sandbox fails with `EPERM`.

What it costs, measured with `harness/sandboxbench.py` (one sandbox per executable against a
process per test, which ra-jit used before and dropped):

| | Linux x86-64 (dev host, 16 cores) | macOS (M5 Max, 8 at a time, loaded) |
|---|---|---|
| a trivial program, bare | 0.86 ms | 5.2 ms |
| in a sandbox | 8.0 ms | 29 ms |
| in a sandbox, recording reads | 9.3 ms | 25 ms |
| 600-test library, test phase, `RA_JIT_SANDBOX=off` | 18 ms | 20 ms |
| the same, sandboxed, `all` | 37 ms | 68 ms |
| the same, sandboxed, `affected` (every test forced to run) | 80 ms | 100 ms |
| the same, a sandbox per test (16 at a time on Linux, 8 on macOS) | 0.68 s | 2.26 s |

A sandbox per test executable costs one launch, about 8 ms on Linux, whatever its number of
tests, and about 25 ms on macOS (mostly the fork, Seatbelt and dyld); a sandbox per test cost
the 600-test library ten to thirty times as much, so there is none. On
macOS a stat under the interposer goes from 0.8 to 1.1 µs, and to 3 to 5 µs on a workspace
whose overlay has been written.

What is left: doctests run in rustdoc's process and are not sandboxed; Linux does not stop a
connection to a Unix socket outside, `io_uring` gets past seccomp, and `openat2` always counts
as a write; Windows has no sandbox yet (its CI runs with `RA_JIT_SANDBOX=off`). The plan is
[#257](https://github.com/indexable-inc/blaze/issues/257); build scripts and proc macros are
[#147](https://github.com/indexable-inc/blaze/issues/147).

### Running only the tests an edit reached

With `RA_JIT_TESTS=affected`, `cargo ra-jit test` runs, of each test executable the daemon
built, the tests an edit could have changed the end of since they last passed:

    Running unittests src/lib.rs (target/debug/deps/tsel-1f2e): 2 of 5 tests
    Skipping tests/it.rs (target/debug/deps/it-9a8b): 2 tests, unchanged since the last pass
    Skipped 5 tests, unchanged since the last pass; 1 test run every time (`-v` says why); `RA_JIT_TESTS=all` runs every test

At each build the daemon hashes every test's code with the code of every function it calls,
directly or through others, in any crate of the workspace or its dependencies, and its
attributes (`#[ignore]`, `#[should_panic]`). The standard library is named by the toolchain.
A test runs again when that hash is not the one it last passed with, when it has not passed
since it was added or failed, and every time when it reaches what its code does not decide:
a file, the environment, another process, the network, or a function ra-jit can't compile.
Of a test executable that fails, the tests libtest's summary lists as failed run again, and
the others that ran are remembered as passed; one that ends with no summary (a crash, or a
test that exits the process) has every test it ran run again. Its output is passed on through
a pipe as it comes, colored as libtest would color it on the terminal.
An integration test also runs again when a program of its package that cargo hands it as
`CARGO_BIN_EXE_<name>` was built again. `cargo ra-jit test -v` says, under each test
executable, why each test it runs every time does, by the fewest calls from the test to it:

    Always tests::reads_a_file: it reaches `statx`, which reads or writes files, by tsel::tests::reads_a_file -> std::fs::read_to_string::<…> -> std::fs::read_to_string::inner -> <std::fs::File>::metadata -> … -> statx

Under `affected` a test that reaches outside its code is not run every time: the sandbox
records every path it opens, looks at, lists or runs, with the environment it was given (the
variables cargo sets for a test, `PATH`, `HOME`, `USER`, `LOGNAME`, `LANG`, `LANGUAGE`,
`LC_*`, `TZ`, `CARGO`, `CARGO_HOME`, `RUSTUP_HOME`, `RUSTUP_TOOLCHAIN`, `LD_LIBRARY_PATH`,
`NIX_LD`, `NIX_LD_LIBRARY_PATH`, `RUST_BACKTRACE`, `RUST_LIB_BACKTRACE`, `RUST_MIN_STACK`, and
those `RA_JIT_TEST_ENV` names, by commas; under `all` a test gets the whole environment). Once
it passes, what was at each path (a file's contents, a directory's names, nothing) is kept
beside its program, and the test runs again when its code, its environment or one of those
changed; otherwise it is skipped like any other. `-v` says why one runs:

    Sandboxed tests::reads_a_file: it read `/home/me/tsel/data.txt`, which changed

A test whose reads are not all known (a program macOS starts without the interposer), one its
package takes out of the sandbox, and every one under `RA_JIT_SANDBOX=off` runs every time.
The time a test reads, the randomness it draws and the threads it starts are its own still.

`-v`, `-q` and `--color` only change what cargo prints, and are the same build to the daemon.

A test executable cargo built, a command that names tests or passes the harness other flags
than `-q`, `--nocapture`, `--show-output`, `--test-threads` and `--color`, and one that runs
the ignored tests run as asked: every test. Time, randomness (a `HashMap`'s seed too) and
threads are not counted as outside the code, so a test that depends on them by chance is as
flaky as it was; nor is a backtrace, whose capture reads `RUST_BACKTRACE` and whose text the
working directory, for what a failure prints. Unset, or `all`, runs every test, as cargo does;
the daemon then hashes nothing. What is left (a hash that never compiles on a warm store, a
watch mode) is [#227](https://github.com/indexable-inc/ra-jit/issues/227).

While cargo builds, a spare worker already loads the workspace, resolves it and compiles the
functions of each crate cargo is done with on a quarter of the cores, and reads the symbols of
each library as cargo writes it. When cargo is done it reads the symbols of the executable
rustc linked, and the daemon is ready: the first edits are patches spliced into that
executable. The daemon builds its own base in the background once nothing has waited for a
second, and gives it up when that would take more than a quarter of the machine's memory
([architecture.md](architecture.md#the-first-edits-patch-what-rustc-linked)). On Linux
x86-64 (16 vCPUs, 8 cores), from an empty store, in interleaved runs:

| Project | `cargo build` | `cargo ra-jit build` | Ready | First edit |
|---|---|---|---|---|
| just | 7.6 to 7.7 s | 8.0 to 8.3 s | 8.4 to 8.7 s | 0.5 s, a patch |
| ripgrep | 5.0 to 5.4 s | 5.4 to 5.7 s | 5.6 to 5.9 s | 1.4 to 1.5 s, a patch |
| nushell | 34.7 to 37.0 s | 37.4 to 38.9 s | 39.0 to 40.5 s | 1.7 to 2.4 s, a patch |

The command takes longer than cargo's because of the spare worker beside it: what it reads
and resolves of the standard library (some 10 core-seconds on every project) and of the
workspace takes cores from cargo, and on this machine every second core is a hyperthread of
another, so a core cargo leaves idle is still not free. With `RA_JIT_SPARE=0` the command is
within a few percent of cargo's, and the daemon is ready later, as it loads the workspace only
then: 11.8 s for just, 8.2 s for ripgrep and 46.7 s for nushell. ripgrep's first edit, in
`main`, moves the lines of a generic function below it, which rustc copied into its callers:
the patch replaces those copies too. On macOS that patch's frames (244 KB) are more than the
unwind table of rustc's link (218 KB) they would take the place of, so the splice moves both
tables into the room the link leaves after its load commands, and the edit is a patch there too.

A spare worker is started for a build: one as the daemon starts, for the build that started
it, and one each time a build of a workspace the daemon has no worker for begins. Once a
workspace is loaded, none waits for the next: a spare with the library loaded holds 1.3 GB.
What that costs is the start of a second project whose program cargo has already built, as
the spare loads the library then rather than before. `RA_JIT_SPARE=ready` keeps one loaded at
all times, as the daemon did until October 2026. On Linux x86-64 (16 cores), a second project
under a running daemon, medians of three interleaved rounds (`harness/spare.py`):

| Project | Daemon, idle | Cold start | Warm start | Daemon after the warm start |
|---|---|---|---|---|
| ripgrep | 1,275 MB (2,635 with `ready`) | 4.86 s (4.74 s) | 2.62 s (1.29 s) | 3,652 MB (4,918 MB) |
| fd | 1,267 MB (2,638) | 24.17 s (24.21 s) | 3.48 s (2.19 s) | 3,354 MB (4,543 MB) |
| nushell | 1,282 MB (2,638) | 30.70 s (30.48 s) | 9.51 s (8.62 s) | 7,219 MB (8,699 MB) |

The cold start is `cargo ra-jit build` from an empty target directory until the daemon is
ready; the warm start is the same once cargo has built it, the daemon stopped and started
again. With `RA_JIT_SPARE=0` the daemon holds the same as without the variable, a cold start
waits for the workspace to load after cargo (7.14 s on ripgrep, 38.43 s on nushell), and a
warm one is quicker (2.14 s, 6.83 s).

## When cargo builds instead

ra-jit hands a build to cargo whenever it cannot be sure its own result would be what cargo's
is. It prints one line with the reason, `ra-jit: bug: <reason>; building with cargo, which
ra-jit is never to do (<step>, and #84)`, and counts it in `cargo ra-jit status`. Each is a bug:
ra-jit is never to build with cargo
([#84](https://github.com/indexable-inc/ra-jit/issues/84)); the line names the step of
[no-rustc.md](no-rustc.md) that removes it, M ([#93](https://github.com/indexable-inc/ra-jit/issues/93))
for an edit's build and K ([#91](https://github.com/indexable-inc/ra-jit/issues/91)) for a first
build; a reason of the test harness names H ([#88](https://github.com/indexable-inc/ra-jit/issues/88)), and a
function ra-jit does not compile whose rustc copy is from before the edit names E
([#85](https://github.com/indexable-inc/ra-jit/issues/85)). The build then behaves as cargo's because it is cargo's.

A whole invocation goes to cargo when any of these hold (`Invocation::only_cargo` in
`cargo/cli.rs`); for `check` only `--build-plan` and `--unit-graph` do:

| Condition | Reason printed |
|---|---|
| `--build-plan` or `--unit-graph` | `--build-plan and --unit-graph are cargo's to answer` |
| `--message-format` | `--message-format` is cargo's to answer |
| a `-Z` flag | unstable cargo flags |
| `--exclude` | `--exclude` is not loaded by the daemon yet |
| more than one `--target` | several targets in one build |
| `RUSTC_WORKSPACE_WRAPPER` set to another wrapper | `RUSTC_WORKSPACE_WRAPPER` is set to another wrapper |
| `ra-jit-rustc` missing | `ra-jit-rustc` is not installed next to `ra-jit` |
| `--target` or `CARGO_BUILD_TARGET` naming a target that is neither ELF nor Mach-O, or one ra-jit does not know | programs for the triple are not linked through `cargo ra-jit` yet |

A single build goes to cargo when:

- a manifest, `Cargo.lock`, a cargo configuration file, or something a
  build script reads has changed since cargo last built (a source a build script makes newer
  before cargo runs rustc on it, as biome's do, has not). Cargo builds, and the daemon loads
  the workspace again afterwards. On a large workspace that reload is long: 36 to 51 s on
  nushell;
- rust-analyzer finds an error in the source. The daemon refuses it, cargo runs, and you get
  rustc's diagnostics and cargo's exit status. This costs 0.1 to 0.5 s over cargo alone.
  Errors rust-analyzer does not find are not caught here; see "Known problems";
- the daemon refused the build or failed.

Doc tests go through ra-jit in rustdoc's place. An example ra-jit cannot build is left to
rustdoc, with a line saying why. Of the test harness's flags, `-q`, `--format pretty`,
`--format terse`, `--color never`, `--exact`, `--skip`, `--test-threads` and test filters
are handled, and the results are shown as libtest shows them; any other flag leaves the
library's doctests to rustdoc. Cargo's run of rustdoc for each library is only written
down; once cargo is done, one run of ra-jit loads the workspace and tests the libraries'
examples in the order cargo asked, each in the directory and with the variables cargo gave
its rustdoc, and says `Doc-tests` and what failed as cargo does. The daemon does not run
doctests yet.

The fallback is per invocation or per program, not per crate. If one program of an
invocation cannot be the daemon's, cargo builds the invocation.

These flags are handled on the daemon's path: `--target` naming the host, a single `-p`,
`--features`, `--bin`, `--example`, `--test`, `--lib`, `--manifest-path`, `--workspace`,
`--no-run`, `--no-fail-fast`, `-q`, test filters and the arguments after `--`.

The daemon's state is kept per command line. `cargo ra-jit build`, `cargo ra-jit build -q`
and `cargo ra-jit build --features x` are three different builds to ra-jit, told apart by a
hash of the subcommand (`run` counts as `build`), the build flags, the working directory
and the environment variables that change what cargo builds. The first use of each is a
cargo build, usually a quick one because cargo's own output is shared between them.

## The daemon

There is one daemon per user. It is started by the first `cargo ra-jit` that needs it, in a
session of its own, and it outlives the command that started it. It exits after 30 minutes
without a request; `RA_JIT_IDLE_SECS` sets another time and is read when the daemon starts
(0 means never).

The daemon holds no compiler state itself. It owns worker processes, each holding one
rust-analyzer database for one repository, toolchain, target, feature set and `cfg(test)`
setting. Git worktrees of one repository share a worker, so what they have in common is
analysed once. Different repositories get different workers.

A new worktree still starts with a cargo build, because it has a target directory of its
own. What it saves is the daemon's side: the worktree joins the worker that is already
loaded instead of loading the workspace again. With a ripgrep checkout loaded, the first
`cargo ra-jit build` in a new worktree of it was cargo's and took 16 s, the worktree showed
as ready under the same worker, and the first edit there was built by the daemon in 0.4 s. A `cargo ra-jit test` can use
two workers, because unit tests are built with `cfg(test)` and integration tests without.

`cargo ra-jit status` shows the daemon, its workers and their log files:

    daemon: pid 10969, version 0.0.0-3da7208-18df73efa25a74db, exits after 1800s idle, workers hold at most 32768 MB
      worker: pid 13169, /dev/shm/ripgrep/, holds 1312 MB, log /home/user/.cache/ra-jit/workers/f30b80a4e02dffde/log
        /dev/shm/ripgrep: ready
    /dev/shm/ripgrep (dev, 1f59ed7b049b7fa0): 3 builds by the daemon, 0 by cargo

A worker for a large workspace holds a lot of memory: 1.3 GB for ripgrep and 9.4 GB for
dioxus-cli at the end of the agent-style edit runs in [results.md](results.md). The workers
together hold at most half of the machine's memory, or what `RA_JIT_MEMORY_MAX` says. On
Linux the machine's memory is the least of what it has and what the control group the daemon
was started in allows (a container's, a CI job's, a systemd unit's). Once a
second, over that, the daemon stops the worker asked longest ago, and the next build of its
workspace is cargo's while a worker loads it again. A worker that is answering or loading is
never stopped, nor is the one asked last, nor any while a program takes live edits.

A client and a daemon built from different sources do not talk to each other. A daemon that
gets a build request from another version stops its workers and exits, and the client
starts its own.

### Files

The daemon's directory is `$RA_JIT_HOME` if set, else `$XDG_RUNTIME_DIR/ra-jit`, else
`~/.cache/ra-jit`. It holds `daemon.sock`, `daemon.sock.lock`, `daemon.log`,
`workers/<id>/` with each worker's socket and log, and `states/`. The directory must belong
to you and be closed to group and others (mode 0700); otherwise the client and the daemon
refuse to use it. The socket is 0600, and a connection from another user is refused before
its request is read.

In a project, ra-jit keeps its records next to cargo's output, so `cargo clean` removes
them too:

    target/<profile>/.ra-jit-units/     what the wrapper recorded of each rustc run
    target/ra-jit/<key>/                state of one command line, and the daemon's objects

On Linux the object with all of a program the daemon compiled, `programs/<unit>/ra-jit.o`, is
kept compressed as `ra-jit.o.zst` between the links that read it, a sixth of its size, and
inflated again for the next: the executable has the debug information, so nothing else reads
it. With `split-debuginfo` set, or on macOS, where a debugger reads the object, it stays as it
is.

Cargo keeps what it builds through a workspace wrapper apart from what it builds without
one. `cargo build` and `cargo ra-jit build` in one checkout therefore each compile the
workspace's crates once, and share the dependencies.

## Environment variables

These are read by `cargo ra-jit` and passed to the worker it starts. They are part of which
build a command line is, so changing one starts from a cargo build again.

| Variable | Meaning | Default |
|---|---|---|
| `RA_JIT_BACKEND` | `cranelift` or `llvm`. `llvm` needs a ra-jit built with `--features llvm` | `cranelift` |
| `RA_JIT_OPT` | LLVM optimisation level, 0 to 3. Functions are still compiled one at a time | 2 |
| `RA_JIT_EMIT` | LLVM instruction selection: `optimized` or `fast` | `optimized`; `fast` at level 0 |
| `RA_JIT_INLINE` | With LLVM at level 2 or 3, a program built whole (`runbench.py`'s builds) has the functions it calls in any crate built into it or shown to LLVM, as a release build does; the standard library's only when they are generic or `#[inline]`, as rustc does across crates. `crate` keeps that to the function's own crate, `0` builds nothing in, `all` does it in other builds with LLVM at level 2 or 3 too (never the daemon's, whose store keeps each function by its own code alone) | across crates in a whole program, the function's own crate otherwise |
| `RA_JIT_SPLICE` | `0` links in full after every edit instead of splicing | splice |
| `RA_JIT_WHOLE_BUILD` | `1` has `build` and `run` from nothing build each program whole with blaze, with no `cargo build` ([above](#what-a-build-does)) | cargo builds first |
| `RA_JIT_OTHER_TOOLCHAIN` | `1` builds with the project's toolchain when it is not the pinned nightly, rather than with the pinned nightly, for a project pinned to a nightly near it; nothing checks that it agrees ([support.md](support.md#toolchain)) | not set |

`RA_JIT_TESTS` is read by each `cargo ra-jit test` alone and is not part of which build it
is: `affected` runs the tests an edit reached
([above](#running-only-the-tests-an-edit-reached)), `all` every test. The default is `all`.
`RA_JIT_SANDBOX=off` runs every test outside the sandbox
([above](#every-test-runs-in-a-sandbox)), and `RA_JIT_TEST_ENV` names more variables a test
is given under `affected`, by commas; both are read the same way.

These are read by the daemon when it starts, from the environment of whichever command
started it. To change one, run `cargo ra-jit stop` first.

| Variable | Meaning | Default |
|---|---|---|
| `RA_JIT_HOME` | the daemon's directory (also read by every client) | see above |
| `RA_JIT_IDLE_SECS` | seconds without a request before the daemon exits; 0 is never | 1800 |
| `RA_JIT_MEMORY_MAX` | the most the workers hold together, a size such as `6G`, or `off` | half the machine's memory (on Linux, or of what its control group allows) |
| `RA_JIT_SPARE` | `ready` keeps a spare worker with the library loaded at all times, for the next project; `0` starts none, and a workspace loads once cargo has built it | a spare for each build of a new workspace, as it begins |
| `RA_JIT_TRACE_STARTUP` | trace function dependencies in Blaze's baseline fragments from the C startup wrapper to names containing this text (for example `::report`). `(inlined)` marks a body incorporated into the preceding fragment by Blaze's inliner | off |
| `RA_JIT_OWN_BASE_MEMORY` | the most a worker may hold while it builds its own base in the background, a size such as `40G`, past which it gives that base up and edits patch rustc's executable; `off` is no bound | a quarter of the machine's memory (on Linux, or of what its control group allows) |
| `RA_JIT_LRU` | how many values of the queries a build in full holds the most of a worker keeps once the build is over, `mir=N,runtime=N,compiled=N,inlinable=N` (lowered MIR, runtime and monomorphized MIR, each instance's code, bodies to inline); 0 keeps every value. One let go is computed again when an edit asks for it ([#261](https://github.com/indexable-inc/blaze/issues/261)) | `mir=2048,runtime=2048,compiled=16384,inlinable=8192` |
| `RA_JIT_CACHE` | directory of the disk store, or `off` for none | `ra-jit/store` in the user's cache directory ([cache.md](cache.md)) |
| `RA_JIT_REMOTE` and the other store variables | see [cache.md](cache.md) | not set |

The disk store is on by default and shared by every daemon and checkout of the user. The
daemon reads `RA_JIT_CACHE` once, when it starts: a daemon started by a shell without the
variable keeps the default store whatever later shells have set, so run `cargo ra-jit stop`
before changing it.

## Known problems

- **The daemon's verdict is the build's, and it is not yet all of rustc's.** An error that
  rust-analyzer or ra-jit's borrow checker finds in the edited files fails the build, written
  as rustc writes it, with cargo's status; no rustc runs to say it again
  ([#84](https://github.com/indexable-inc/ra-jit/issues/84)). What they do not find is
  built: rust-analyzer reports few of rustc's errors for certain, and the borrow checker
  passes part of rustc's tests ([borrowck.md](borrowck.md)). Code rustc builds that ra-jit
  takes for an error fails the build too; that is a ra-jit bug to report. A lint the source
  denies fails it as well, for the lints rust-analyzer finds only where rustc does
  (`non_snake_case`, `non_camel_case_types`, `non_upper_case_globals`, `unused_must_use`),
  unless `--cap-lints` or `--force-warn` is among rustc's flags. `cargo ra-jit check` says
  what such a build would, when the daemon has it loaded, and what it finds in the whole
  workspace otherwise.
- Linux x86-64 and macOS arm64 have been run; on macOS only ripgrep, fd and hyperfine and
  the generated workspace of the harness. The code compiles for Windows, but every build
  there would go to cargo (the link step knows ELF and Mach-O), and the command has not
  been built or run there. The socket's permission and peer checks do nothing on Windows.
- On Linux `cargo ra-jit` was measured with the Cranelift backend only; LLVM has been run
  through the harness gate, not through `harness/cargo_ra_jit.py`. On macOS the harness was
  run under both.
- In ten runs of just's test suite through `harness/cargo_ra_jit.py` there were two
  differences from cargo that are not explained. One was a test failing in the
  daemon-built executable and passing under cargo. The other was between two executables
  cargo built, which points at the tests being sensitive to load, but that was not shown.
- A function rustc emitted no symbol for is assumed to be dead code. One bug came from that
  assumption being wrong (test functions, fixed); no other is known, and it is not proved
  that there is none.
- The check that only your user can reach the daemon is tested for the same user only.
- `cargo ra-jit` is not in the CI workflow. `harness/cargo_ra_jit.py` needs the network and
  about 20 minutes.

## Plain `cargo`, switched on globally

`cargo ra-jit` needs whoever runs the build to type something different. The global switch is
for when they won't: agents, scripts, editors that call `cargo build` themselves.

    ra-jit enable     # plain cargo now goes through ra-jit, for this user
    ra-jit status     # is it on, and what the daemon served or left to rustc
    ra-jit disable    # back to how it was

`ra-jit`, `cargo-ra-jit` and `ra-jit-rustc` have to be in the same directory.

### What `enable` changes

One key in `$CARGO_HOME/config.toml` (usually `~/.cargo/config.toml`):

    [build]
    rustc-workspace-wrapper = "<dir>/ra-jit-rustc"

No shell profile or environment variable is touched. `enable` keeps a copy of the file as it
was in `ra-jit-enabled.json` next to it. `disable` restores that copy byte for byte, or, if
you edited the file in between, removes only its own line.

### Wrappers you already have

- `build.rustc-wrapper` or `RUSTC_WRAPPER` (sccache and the like) is left alone and runs
  around `ra-jit-rustc`.
- An existing `rustc-workspace-wrapper` is chained: `ra-jit-rustc` runs rustc through it, and
  `disable` puts your line back.
- `enable` and `status` print both, so you can see what will run.

A `RUSTC_WORKSPACE_WRAPPER` in the environment, or a project `.cargo/config.toml` that sets
the key, takes precedence over the user config. ra-jit is not used in that workspace, and
`ra-jit status` run there says so.

### What you get, and what you don't

The daemon, its loaded workspaces, the sharing between worktrees and the store are the same
ones `cargo ra-jit` uses. The difference is where ra-jit sits. Cargo still plans the build and
calls the wrapper once per crate; the daemon answers for programs and test executables, and
workspace library crates are still compiled by rustc. A build with nothing to do costs what
cargo's own no-op costs. The daemon loads a build that rustc did once cargo exits: after
`cargo test` has run the tests, after `cargo run` has run the program.

When the daemon cannot serve a unit, rustc builds it and one line on stderr says why:

    ra-jit: the daemon had not loaded the build yet; rustc links `app`

`ra-jit status` counts these by reason. A build never fails because ra_jit is enabled: a
daemon that is missing, dies during the build, or is another version (it gets replaced) all
end in rustc doing the work. An error ra-jit cannot diagnose itself, such as a use after
move, fails with rustc's message and status 101, as under cargo.

The store is on by default in this mode and in `cargo ra-jit`: `$XDG_CACHE_HOME/ra-jit/store`,
else `~/.cache/ra-jit/store`, limited to a tenth of its disk (at most 32 GiB). `RA_JIT_CACHE=<dir>` moves it, `RA_JIT_CACHE=off`
disables it, `RA_JIT_CACHE_MAX` sets the limit. The socket and logs are in
`$XDG_RUNTIME_DIR/ra-jit`, else `~/.cache/ra-jit`.

### What has been checked

On Linux x86-64, under both backends, with a generated workspace, each of these gave the
same output, exit status and test results as cargo alone on the first build, a warm build
and a build after an edit: `cargo test`, `test --doc`, `test --no-fail-fast` with a failing
test, `run --example`, `run --release`, `run --target <host>`, `run --bin`,
`run --features -p`, `check`, `check --all-targets`, `build --workspace --all-targets`,
`cargo install --path`, and a `cargo check --message-format=json` running beside a build.
Also: 12 builds at once over 3 worktrees on one daemon, the daemon killed during a build,
both kinds of user wrapper, and the `enable`/`disable` round trip. ripgrep, fd and hyperfine
match cargo on every step. `harness/cargo_ra_jit.py` runs all of this.

Not checked: a proc macro defined in the workspace, `--target` for another triple,
`cargo install` from the registry, a real rust-analyzer session next to it, real sccache (a
stand-in wrapper was used). On Windows every build is cargo's in this mode and nothing was
run. macOS arm64 is below.

### Known problems

- Edits are slower than with `cargo ra-jit`, and on four of nine projects slower than cargo
  alone (one run each, Cranelift: ripgrep 1.32 s against 0.78 s, eza 2.02 against 1.32,
  tokei 1.23 against 0.70, axum 0.68 against 0.41). The cause has not been measured yet.
- The store grows fast: one harness pass over starship wrote 7.6 GiB.
- A socket path over 108 bytes cannot be bound; the build goes to cargo and says so. The
  default locations are well under that.


## macOS arm64

`cargo ra-jit` and plain cargo after `ra-jit enable` serve dev and test builds for
`aarch64-apple-darwin`. x86-64 macOS is not a target ra-jit knows, so builds for it are
cargo's.

Two things differ from Linux. Apple's linker has no way to let the first of two
definitions win, so the daemon links rustc's line with copies of the workspace's objects in
which every definition is weak, and its own object in front; this is the link the daemon
already did on macOS, and the line also gets `-Wl,-headerpad` (1 MiB to start with) so that
the executable can be spliced later. A program whose unwind table does not fit is linked
once more with the room it needs, which the links after keep (see
[platforms.md](platforms.md)). And there is no `/proc`: in the global mode, the cargo above the
wrapper is read through `libproc` and `sysctl`.

What was run, on an 8-CPU Apple M4 virtual machine with macOS 26.5 and Rust 1.99.0:
`harness/cargo_ra_jit.py --small`, under Cranelift and under LLVM (`RA_JIT_OPT=2`,
`RA_JIT_EMIT=fast`). Under each it passed: ripgrep, fd and hyperfine gave the same output,
exit status and test results as cargo alone on every step of both modes (134 comparisons),
and the 101 checks of the flags, the daemon and plain cargo held. Those include the
`enable`/`disable` round trip of `config.toml`, byte for byte, over five configurations, and
the second user at the socket, for which the harness uses `nobody` on macOS.

Seconds for the build after an edit, ra-jit / cargo alone, one run each:

| Project | `cargo ra-jit`, Cranelift | `cargo ra-jit`, LLVM | plain cargo, Cranelift | plain cargo, LLVM |
|---|---:|---:|---:|---:|
| ripgrep 15.1.0 | 2.08 / 1.43 | 0.40 / 0.67 | 1.35 / 0.97 | 1.07 / 0.69 |
| fd 10.3.0 | 0.24 / 0.42 | 0.25 / 0.43 | 0.26 / 0.42 | 0.29 / 0.42 |
| hyperfine 1.19.0 | 0.34 / 0.35 | 0.21 / 0.33 | 0.24 / 0.34 | 0.23 / 0.34 |

The ripgrep numbers are not steady. Two earlier Cranelift runs of the same step on the same
machine gave 0.52 / 0.73 and 1.08 / 1.30 for `cargo ra-jit`, and 1.86 / 0.78 and 1.30 / 1.30
for plain cargo; in every run the daemon did that build, and under plain cargo it was not
faster than cargo. Why ripgrep's edit takes one to two seconds in some runs was not looked
into. A build with no change took 0.03 to 0.06 s through `cargo ra-jit` against 0.31 to
0.41 s, and 0.16 to 0.19 s through plain cargo.

Builds that went to cargo in those runs, each with its reason printed: the first build of
every project, the build of source that does not compile, ripgrep's tests after an edit
under `cargo ra-jit` (a library the unit tests are linked with as rustc built it), and the
tests of ripgrep and hyperfine after an edit under plain cargo ("the build is not one the
daemon has loaded").

Not run on macOS: the projects of the harness other than those three, `cargo ra-jit` with
debug info looked at in a debugger, and anything on x86-64.
