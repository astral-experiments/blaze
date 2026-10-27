"""Time each tool on an empty target directory, then on the same source with one edit."""

import collections
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import time


EDITS = {
    "ruff": (
        "crates/ruff_linter/src/rule_selector.rs",
        '"ALL" => Ok(Self::All),',
        '"ALL" | "ALL-RULES" => Ok(Self::All),',
        2,
        '"ALL-RULES".parse::<RuleSelector>(), Ok(RuleSelector::All)',
    ),
    "ty": (
        "crates/ty_project/src/metadata/python_version.rs",
        "let version = PythonVersion::from_str(s).map_err(SupportedPythonVersionError::Parse)?;",
        'let s = s.strip_prefix("python").unwrap_or(s);\n        let version = PythonVersion::from_str(s).map_err(SupportedPythonVersionError::Parse)?;',
        1,
        '"python3.13".parse::<SupportedPythonVersion>(), Ok(SupportedPythonVersion::Py313)',
    ),
    "uv": (
        "crates/uv-configuration/src/version_bump.rs",
        '"patch" => Ok(Self::Patch),',
        '"patch" | "micro" => Ok(Self::Patch),',
        1,
        '"micro".parse::<VersionBump>(), Ok(VersionBump::Patch)',
    ),
    "uvx": (
        "crates/uv/src/bin/uvx.rs",
        'file_name_str.strip_prefix("uvx")',
        'file_name_str.strip_prefix("uvx").or_else(|| file_name_str.strip_prefix("UVX"))',
        1,
        'get_uvx_suffix(Path::new("UVX@1.2.3")), Some("@1.2.3")',
    ),
}
COW = (
    "cache_clean::clean_all_cloned_file",
    "cache_clean::clean_all_cached_clones",
    "cache_clean::clean_all_compressed_file",
    "pip_install::install_executable_clone",
    "pip_install::install_copy_on_write_fs",
)
WITNESS = "benchmark_edit_new_spelling"
SUMMARY = re.compile(r"test result: (?:ok|FAILED)\. (\d+) passed; (\d+) failed; (\d+) ignored;")
CASE = re.compile(r"^test (.+?) \.\.\. (ok|ignored|FAILED)(?:$|,)")
RUNNING = re.compile(r"^\s*Running .+? \((.+)\)\s*$")


def output(*args, **kwargs):
    return subprocess.check_output(args, text=True, **kwargs).strip()


def selection(suite):
    if suite == "uv":
        return ["--workspace", "--features", "test-python-patch,test-universal,native-auth,secret-service"]
    metadata = json.loads(output("cargo", "metadata", "--locked", "--no-deps", "--format-version", "1"))
    members = set(metadata["workspace_members"])
    names = sorted(p["name"] for p in metadata["packages"] if p["id"] in members)
    chosen = [name for name in names if (name == "ty" or name.startswith("ty_")) == (suite == "ty")]
    assert chosen and len(chosen) != len(names)
    return ["--all-features", *(arg for name in chosen for arg in ("-p", name))]


def change(suite, source):
    _, old, new, count, assertion = EDITS[suite]
    assert source.count(old) == count and WITNESS not in source
    test = f"\n#[cfg(test)]\n#[test]\nfn {WITNESS}() {{\n    assert!(matches!({assertion}));\n}}\n"
    return source.replace(old, new) + test


def executables(log):
    result = {}
    for line in log.splitlines():
        found = RUNNING.match(line)
        if not found:
            continue
        path = Path(found[1]).resolve()
        if path.name.startswith("uv_requirements_txt-") and "unittests" in line:
            result["inline"] = path
        for target in ("build", "pip_install"):
            if f"Running tests/{target}/main.rs" in line:
                result[target] = path
    return result


def timed_run(suite, engine, phase, package_args, env, results):
    python_group = ["--only-group", "test"] if suite == "uv" else ["--only-dev"]
    command = ["uv", "run", "--locked", *python_group, engine, "test", "--locked", "--no-fail-fast", *package_args]
    if env.get("BENCH_SCOPE") == "unit-integration":
        command.append("--tests")
    if suite == "uv":
        command += ["--", "--skip", "test::malformed_option::", *(arg for name in COW for arg in ("--skip", name))]
    stem = f"{engine}-{phase}"
    log = results / f"{stem}.log"
    print(f"START {stem}: {command}", flush=True)
    ready = None
    inline_exe = None
    issues = []
    with log.open("w") as stream:
        start = time.perf_counter()
        with subprocess.Popen(command, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              text=True, errors="replace") as process:
            for line in process.stdout:
                if "Running " in line and (found := RUNNING.match(line)):
                    if ready is None:
                        ready = time.perf_counter() - start
                    if "unittests" in line and Path(found[1]).name.startswith("uv_requirements_txt-"):
                        inline_exe = Path(found[1]).resolve()
                stream.write(line)
            code = process.wait()
        stream.flush()
        if suite == "uv" and inline_exe:
            manifest = Path.cwd() / "crates/uv-requirements-txt"
            run_env = dict(env, CARGO_MANIFEST_DIR=str(manifest))
            listed = output(inline_exe, "--list", "test::malformed_option::", cwd=manifest, env=run_env)
            names = [s.removesuffix(": test") for s in listed.splitlines() if s.endswith(": test")]
            if len(names) != 3:
                issues.append(f"found {len(names)} isolated snapshot cases")
            for case in names:
                result = subprocess.run([inline_exe, "--exact", case], cwd=manifest, env=run_env,
                                        stdout=stream, stderr=subprocess.STDOUT)
                if result.returncode:
                    issues.append(f"{case} exited {result.returncode}")
        elif suite == "uv":
            issues.append("missing requirements-txt test executable")
        elapsed = time.perf_counter() - start
    raw = log.read_text()
    totals = [sum(map(int, values)) for values in zip(*SUMMARY.findall(raw))]
    if not totals:
        totals = [0, 0, 0]
    cases = sorted((m[1], m[2]) for line in raw.splitlines() if (m := CASE.match(line)))
    (results / f"{stem}.cases").write_text("\n".join(f"{status}\t{name}" for name, status in cases) + "\n")
    witness = sum(WITNESS in name and status == "ok" for name, status in cases)
    if witness != int(phase == "edit"):
        issues.append(f"expected {int(phase == 'edit')} new test, saw {witness}")
    if code or totals[1] or not totals[0] or ready is None:
        issues.append(f"exit={code}, totals={totals}, first test={ready}")
    if engine == "blaze":
        if "profile [ra-jit] target(s)" not in raw:
            issues.append("missing Blaze compilation marker")
        if "this build is cargo's" in raw or "; building with cargo" in raw:
            issues.append("Blaze handed the workspace to Cargo")
    record = dict(engine=engine, phase=phase, seconds=elapsed, first_test_seconds=ready, exit=code,
                  passed=totals[0], failed=totals[1], ignored=totals[2], summaries=len(SUMMARY.findall(raw)),
                  witness=witness, listed=len(cases), command=command, issues=issues)
    print("MEASUREMENT " + json.dumps(record), flush=True)
    if issues:
        print("\n".join(raw.splitlines()[-18:]), flush=True)
    return record


def save_cow(log, dest):
    bins = executables(log.read_text())
    with tempfile.TemporaryDirectory(prefix="cowbins-") as scratch:
        for name, path in (("build", bins["build"]), ("pip_install", bins["pip_install"]), ("uv", Path("target/debug/uv"))):
            subprocess.run(["strip", "--strip-debug", "-o", str(Path(scratch) / name), path], check=True)
        dest.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["tar", "-czf", dest, "-C", scratch, "."], check=True)


def main():
    suite, trial, directory = sys.argv[1:]
    trial = int(trial)
    results = Path(directory).resolve()
    results.mkdir(parents=True, exist_ok=True)
    package_args = selection(suite)
    edit_kind = "uvx" if os.environ.get("BENCH_SCOPE") == "uvx-edit" else suite
    assert edit_kind != "uvx" or suite == "uv"
    path = Path(EDITS[edit_kind][0])
    original = path.read_text()
    edit = change(edit_kind, original)
    target = Path("target").resolve()
    assert not target.exists()
    order = ("cargo", "blaze") if trial % 2 else ("blaze", "cargo")
    details = dict(suite=suite, scope=os.environ.get("BENCH_SCOPE", "default"), trial=trial, order=order, downstream=output("git", "rev-parse", "HEAD"),
                   workflow=os.environ["GITHUB_SHA"], run=os.environ["GITHUB_RUN_ID"],
                   blaze=output("git", "-C", "../blaze", "rev-parse", "HEAD"),
                   rustc=output("rustc", "-vV"), cargo=output("cargo", "-V"), kernel=platform.release(),
                   cpu=output("lscpu"), meminfo=Path("/proc/meminfo").read_text().splitlines()[0],
                   jobs=os.environ["CARGO_BUILD_JOBS"], packages=package_args,
                   lock=hashlib.sha256(Path("Cargo.lock").read_bytes()).hexdigest(),
                   errors=[], measurements=[])
    def save():
        (results / "measurements.json").write_text(json.dumps(details, indent=2) + "\n")
    save()
    path.write_text(edit)
    (results / "edit.patch").write_text(output("git", "diff", "--", str(path)) + "\n")
    path.write_text(original)
    for engine in order:
        assert not target.exists() and path.read_text() == original
        with tempfile.TemporaryDirectory(prefix="bb-") as scratch:
            daemon = str(Path(scratch) / "home")
            store = str(Path(scratch) / "store")
            env = dict(os.environ, CARGO_TARGET_DIR=str(target), BLAZE_HOME=daemon, RA_JIT_HOME=daemon,
                       BLAZE_CACHE=store, RA_JIT_CACHE=store, BLAZE_TESTS="all", RA_JIT_TESTS="all")
            assert not Path(daemon).exists() and not Path(store).exists()
            try:
                for phase in ("clean", "edit"):
                    if phase == "edit":
                        path.write_text(edit)
                    row = timed_run(suite, engine, phase, package_args, env, results)
                    details["measurements"].append(row)
                    save()
                    if suite == "uv" and trial in (1, 7) and row["exit"] == 0 and row["passed"]:
                        archive = Path(os.environ["RUNNER_TEMP"]) / "bench-cow" / f"{engine}-{phase}.tar.gz"
                        save_cow(results / f"{engine}-{phase}.log", archive)
            finally:
                if engine == "blaze":
                    subprocess.run(["blaze", "stop"], env=env, timeout=90)
                path.write_text(original)
                if target.exists():
                    shutil.rmtree(target)
    for phase in ("clean", "edit"):
        left = (results / f"cargo-{phase}.cases").read_text().splitlines()
        right = (results / f"blaze-{phase}.cases").read_text().splitlines()
        if left != right:
            cargo, blaze = collections.Counter(left), collections.Counter(right)
            details["errors"].append(f"{phase} case lists differ: {list((cargo-blaze).items())[:12]} vs {list((blaze-cargo).items())[:12]}")
    rows = details["measurements"]
    for engine in order:
        cold, after = [r for r in rows if r["engine"] == engine]
        if (after["passed"], after["ignored"]) != (cold["passed"] + 1, cold["ignored"]):
            details["errors"].append(f"{engine}: an edited suite should add one passing test")
    save()
    if details["errors"] or any(r["issues"] for r in rows):
        raise SystemExit(f"Benchmark validation failed: {details['errors']}")


if __name__ == "__main__":
    main()
