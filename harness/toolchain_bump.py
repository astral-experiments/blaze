#!/usr/bin/env python3
"""toolchain_bump.py: move ra-jit to a newer Rust nightly. ra-jit supports one toolchain, the
dated nightly in harness/toolchain; everything that mirrors that rustc moves with it.

    toolchain_bump.py                  the newest nightly that has every component blaze needs
    toolchain_bump.py --date 2026-11-02
    toolchain_bump.py --checks         after a bump, on a dev host: build ra-jit and run the
                                       checks that prove ra-jit and the nightly agree

A bump, in order (each step names itself when it fails, and stops there):

 1. picks the nightly: the given date, or the newest of the last two weeks whose manifest has
    rustc, cargo, rust-std, rust-src, llvm-tools, rustfmt, clippy, rust-analyzer and (but on
    Windows) rustc-codegen-cranelift-preview for every host blaze ships for;
 2. writes harness/toolchain and runs toolchain_pins.py --write (pins.txt: the files' hashes,
    the nightly's `rustc -V` and its rust-lang/rust commit);
 3. re-vendors from rust-lang/rust at the new commit: vendor/rustc_abi/src is
    compiler/rustc_abi/src copied as it is, and vendor/proc-macro-srv is upstream's crates at
    the new commit with ra-jit's changes to them at the old commit carried over (a three-way
    apply; a conflict stops the bump and names the files);
 4. moves the pins written down elsewhere: the old toolchain name, rustc version line and
    commit in install.sh and vendor/proc-macro-srv (RUSTC_VERSION_STRING, the manifest's
    header), Cargo.toml's rust-version, and the flake's rust-overlay input (`nix flake update
    rust-overlay`, when nix is on PATH);
 5. installs the nightly with rustup.

What it leaves to you, which it prints at the end: answers.py --write on each of x86-64 Linux,
AArch64 Linux and arm64 macOS (the target specs depend on the host rustc was built for), the
code that mirrors rustc by hand (the Rust ABI in the code generator, vtable.rs, type_id.rs,
symbol names, new intrinsics and lang items: the checks find them), and `--checks`. The rustc
and the daemon match by the whole `rustc -V` line, so a binary built before the bump refuses
the new nightly until it is rebuilt.
"""
import argparse, asyncio, datetime, hashlib, os, re, shutil, sys, tempfile, tomllib, urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
PIN = os.path.join(HERE, "toolchain")
PINS = os.path.join(ROOT, "crates/ra-jit/src/cargo/toolchain/pins.txt")
DIST = "https://static.rust-lang.org/dist"
RUST = "https://github.com/rust-lang/rust"
CACHE = os.path.join(os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache"), "ra-jit-bump")
HOSTS = ["aarch64-apple-darwin", "x86_64-unknown-linux-gnu", "aarch64-unknown-linux-gnu",
         "x86_64-pc-windows-msvc"]
COMPONENTS = ["rustc", "cargo", "rust-std", "rust-src", "llvm-tools-preview", "rustfmt-preview",
              "clippy-preview", "rust-analyzer-preview", "rustc-codegen-cranelift-preview"]
# Where the old pin is written down besides harness/toolchain and pins.txt.
MENTIONS = ["install.sh", "vendor/proc-macro-srv/Cargo.toml",
            "vendor/proc-macro-srv/crates/proc-macro-srv/src/lib.rs"]
# vendor/proc-macro-srv's manifest and lock file are ra-jit's, written from upstream's (their
# header says how) and carried as they are; its crates are upstream's (`server_dirs`).
ABI = ("vendor/rustc_abi/src", "compiler/rustc_abi/src")


class Failed(Exception):
    pass


async def run(step, *argv, cwd=ROOT, bound=600, env=None, stdin=None):
    """Runs a command; a failure names the step, the command, its exit code and its stderr."""
    try:
        async with asyncio.timeout(bound):
            proc = await asyncio.create_subprocess_exec(
                *argv, cwd=cwd, env=env, stdin=asyncio.subprocess.PIPE if stdin else None,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            out, err = await proc.communicate(stdin)
    except TimeoutError:
        raise Failed(f"{step}: `{' '.join(argv)}` took over {bound} s") from None
    except FileNotFoundError:
        raise Failed(f"{step}: `{argv[0]}` is not on PATH") from None
    if proc.returncode != 0:
        tail = "\n".join(err.decode(errors="replace").splitlines()[-25:])
        raise Failed(f"{step}: `{' '.join(argv)}` exited {proc.returncode}:\n{tail}")
    return out.decode(errors="replace")


async def fetch(url, bound=60):
    def get():
        with urllib.request.urlopen(url, timeout=bound) as answer:
            return answer.read()
    async with asyncio.timeout(bound):
        return await asyncio.to_thread(get)


async def manifest(date):
    """The nightly of `date`'s channel manifest, or None when there is none."""
    try:
        data = await fetch(f"{DIST}/{date}/channel-rust-nightly.toml")
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError):
        return None
    return tomllib.loads(data.decode())


def missing(channel):
    """What blaze needs that the nightly of `channel` lacks."""
    lacking = []
    for package in COMPONENTS:
        for host in HOSTS:
            if package == "rustc-codegen-cranelift-preview" and "windows" in host:
                continue
            target = "*" if package == "rust-src" else host
            built = channel["pkg"].get(package, {}).get("target", {}).get(target, {})
            if not built.get("available"):
                lacking.append(f"{package} for {target}")
    return lacking


async def pick(date):
    if date:
        channel = await manifest(date)
        if channel is None:
            raise Failed(f"pick: there is no nightly of {date} at {DIST}/{date}")
        if lacking := missing(channel):
            raise Failed(f"pick: the nightly of {date} lacks {', '.join(lacking)}")
        return date, channel
    today = datetime.date.today()
    dates = [(today - datetime.timedelta(days=back)).isoformat() for back in range(14)]
    async with asyncio.TaskGroup() as group:
        tasks = {date: group.create_task(manifest(date)) for date in dates}
    for date in dates:
        channel = tasks[date].result()
        if channel is not None and not missing(channel):
            return date, channel
        print(f"nightly-{date}: {'none published' if channel is None else 'lacks ' + ', '.join(missing(channel))}")
    raise Failed("pick: no nightly of the last two weeks has every component blaze needs")


def pinned():
    """The pin as pins.txt has it: toolchain name, `rustc -V` after `rustc `, commit."""
    lines = dict(line.split(" ", 1) for line in open(PINS).read().splitlines() if line and not line.startswith("#"))
    return lines["release"].split()[0], lines["rustc"], lines["commit"]


async def checkout(commit, paths):
    """rust-lang/rust's `paths` at `commit`, in a cached sparse clone; returns its directory."""
    repo = os.path.join(CACHE, "rust")
    if not os.path.isdir(os.path.join(repo, ".git")):
        os.makedirs(repo, exist_ok=True)
        await run("vendor: clone", "git", "init", "-q", repo)
        await run("vendor: clone", "git", "-C", repo, "remote", "add", "origin", RUST)
    await run("vendor: sparse", "git", "-C", repo, "sparse-checkout", "set", *paths)
    await run(f"vendor: fetch {commit[:9]}", "git", "-C", repo, "fetch", "-q", "--depth", "1",
              "--filter=blob:none", "origin", commit, bound=1800)
    await run(f"vendor: checkout {commit[:9]}", "git", "-C", repo, "checkout", "-q", "-f", commit)
    return repo


def replace_tree(src, dst):
    shutil.rmtree(dst, ignore_errors=True)
    shutil.copytree(src, dst, symlinks=True)


def server_dirs():
    """vendor/proc-macro-srv's crates, each with where it is in rust-lang/rust."""
    server = os.path.join(ROOT, "vendor/proc-macro-srv")
    dirs = {f"crates/{name}": f"src/tools/rust-analyzer/crates/{name}"
            for name in sorted(os.listdir(os.path.join(server, "crates")))}
    return dirs | {"compiler/rustc_lexer": "compiler/rustc_lexer"}


async def vendor(old, new):
    server = os.path.join(ROOT, "vendor/proc-macro-srv")
    dirs = server_dirs()
    paths = [ABI[1], *dirs.values()]
    with tempfile.TemporaryDirectory(prefix="ra-jit-bump-") as work:
        # ra-jit's changes to the server: upstream at the old commit against vendor/, as a patch.
        repo = await checkout(old, paths)
        for ours, theirs in dirs.items():
            shutil.copytree(os.path.join(repo, theirs), os.path.join(work, "base", ours), symlinks=True)
            shutil.copytree(os.path.join(server, ours), os.path.join(work, "ours", ours), symlinks=True)
        diff = await asyncio.create_subprocess_exec(
            "git", "diff", "--no-ext-diff", "--no-index", "--binary", "--no-color", "--no-renames",
            "base", "ours", cwd=work, env={k: v for k, v in os.environ.items() if k != "GIT_EXTERNAL_DIFF"}, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        patch, err = await diff.communicate()
        if diff.returncode not in (0, 1):
            raise Failed(f"vendor: diffing vendor/proc-macro-srv with upstream at {old[:9]} exited "
                         f"{diff.returncode}: {err.decode()[-2000:]}")
        # Paths are a/base/... and b/ours/... (b/base/... for a file ra-jit deleted).
        patch = re.sub(rb"(?m)(^diff --git | |^--- |^\+\+\+ )([ab])/(?:base|ours)/", rb"\1\2/", patch)
        repo = await checkout(new, paths)
        replace_tree(os.path.join(repo, ABI[1]), os.path.join(ROOT, ABI[0]))
        for ours, theirs in dirs.items():
            replace_tree(os.path.join(repo, theirs), os.path.join(server, ours))
        if not patch.strip():
            return
        patch_file = os.path.join(work, "ra-jit.patch")
        open(patch_file, "wb").write(patch)
        # A three-way apply needs the old blobs in this repository, which upstream's are not:
        # apply with rejects, so what applies does and each hunk that does not is a .rej file.
        proc = await asyncio.create_subprocess_exec(
            "git", "apply", "--reject", "--whitespace=nowarn", "--directory=vendor/proc-macro-srv",
            patch_file, cwd=ROOT, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        _, err = await proc.communicate()
        if proc.returncode != 0:
            os.makedirs(CACHE, exist_ok=True)
            kept = os.path.join(CACHE, f"proc-macro-srv-{old[:9]}-{new[:9]}.patch")
            shutil.copy(patch_file, kept)
            raise Failed("vendor: ra-jit's changes to vendor/proc-macro-srv did not all apply at "
                         f"{new[:9]}; resolve the *.rej files under vendor/proc-macro-srv "
                         f"(the whole patch is {kept}), then finish the bump's later steps "
                         f"(move_mentions, flake, rustup) by hand. git apply said:\n{err.decode()[-3000:]}")


def move_mentions(old, new):
    (old_name, old_rustc, old_commit), (new_name, new_rustc, new_commit) = old, new
    pairs = [(old_name, new_name), (old_rustc, new_rustc), (old_commit, new_commit),
             (old_commit[:9], new_commit[:9])]
    for path in MENTIONS:
        full = os.path.join(ROOT, path)
        text = open(full).read()
        moved = text
        for was, now in pairs:
            moved = moved.replace(was, now)
        if moved == text:
            print(f"note: {path} names none of the old pin ({old_name}, {old_commit[:9]})")
        open(full, "w").write(moved)
    major_minor = ".".join(new_rustc.split(" ")[0].split(".")[:2])
    cargo = os.path.join(ROOT, "Cargo.toml")
    text = open(cargo).read()
    moved, count = re.subn(r'(?m)^rust-version = "[^"]*"', f'rust-version = "{major_minor}"', text, count=1)
    if count != 1:
        raise Failed("mentions: Cargo.toml has no `rust-version = ...` line")
    open(cargo, "w").write(moved)


async def bump(date):
    old = pinned()
    date, channel = await pick(date)
    name = f"nightly-{date}"
    if name == old[0]:
        print(f"ra-jit is already on {name}")
        return
    print(f"moving {old[0]} ({old[1]}) to {name} ({channel['pkg']['rustc']['version']})")
    open(PIN, "w").write(name + "\n")
    await run("pins", sys.executable, os.path.join(HERE, "toolchain_pins.py"), "--write", bound=900)
    new = pinned()
    await vendor(old[2], new[2])
    move_mentions(old, new)
    async with asyncio.TaskGroup() as group:
        if shutil.which("nix"):
            group.create_task(run("flake", "nix", "flake", "update", "rust-overlay", bound=600))
        else:
            print("note: nix is not on PATH: run `nix flake update rust-overlay` where it is")
        if shutil.which("rustup"):
            group.create_task(run("rustup", "rustup", "toolchain", "install", name, "--profile", "minimal",
                                  "-c", "rust-src", "-c", "llvm-tools", "-c", "rustfmt", "-c", "clippy",
                                  bound=1200))
    print(f"""
ra-jit is pinned to {name} ({new[1]}). Left to do:
  - on x86-64 Linux, AArch64 Linux and arm64 macOS each: `python3 harness/answers.py --write`
    (with that host's release build of ra-jit), then commit crates/ra-jit/src/answers/rustc.json;
  - `cargo build -p ra-jit --release` and fix what the new rustc_abi and std ask for;
  - `python3 harness/toolchain_bump.py --checks` on a dev host, under Cranelift and under LLVM
    (`nix develop -c ...` with RA_JIT_BACKEND=llvm RA_JIT_OPT=2 RA_JIT_EMIT=fast);
  - the full tier (uitest.py, libsuite.py, borrowck_ui.py) re-measured at the new commit;
  - `git diff --stat` and one commit for the bump.""")


async def checks():
    """Builds ra-jit and runs what proves it agrees with the pinned nightly, in parallel."""
    name = open(PIN).read().strip()
    env = dict(os.environ, RUSTUP_TOOLCHAIN=name)
    await run("build", "cargo", "build", "-p", "ra-jit", "--release", env=env, bound=3600)
    env.setdefault("RA_JIT_BIN", os.path.join(ROOT, "target/release/ra-jit"))
    harness = [
        ["toolchain_pins.py"],
        ["answers.py"],
        ["macroserver.py"],
        ["difftest.py", "corpus", "--bin", "runner", "--crate", "cases", "--features", "cases/nightly", "--layouts"],
        ["ci.py", "quick-1"], ["ci.py", "quick-2"], ["ci.py", "quick-3"],
    ]
    failed = []

    async def one(argv):
        try:
            await run(f"check {argv[0]}", sys.executable, *argv, cwd=HERE, env=env, bound=7200)
            print(f"ok      {' '.join(argv)}")
        except Failed as error:
            failed.append(str(error))
            print(f"FAILED  {' '.join(argv)}")

    async with asyncio.TaskGroup() as group:
        for argv in harness:
            group.create_task(one(argv))
    if failed:
        raise Failed("checks:\n\n" + "\n\n".join(failed))
    print(f"ra-jit agrees with {name} on every check")


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--date", help="the nightly's date, YYYY-MM-DD (default: the newest usable)")
    parser.add_argument("--checks", action="store_true", help="build and run the agreement checks")
    args = parser.parse_args()
    try:
        asyncio.run(checks() if args.checks else bump(args.date))
    except Failed as error:
        sys.exit(f"error: toolchain_bump: {error}")


if __name__ == "__main__":
    main()
