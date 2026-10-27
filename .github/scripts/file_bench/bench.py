"""Run a paired clean build and edited rerun of exactly one upstream test file."""

from collections import Counter
from hashlib import sha256
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


CONFIG = {
    "ruff": ("ruff", "cli", "lint::", "lint::lint_options", "tests/cli/main.rs", ["--all-features"]),
    "uv": ("uv", "lock", "lock::", "lock::lock_validation_warning_chain", "tests/lock/main.rs",
           ["--features", "test-python-patch,test-universal,native-auth"]),
    "ty": ("ty_python_semantic", "mdtest", "mdtest::subscript/stepsize_zero.md",
           "mdtest::subscript/stepsize_zero.md", "tests/mdtest.rs", ["--all-features"]),
}
HEADER = re.compile(r"^\s*Running (.+?) \((.+)\)\s*$")
SUMMARY = re.compile(r"^test result: (?:ok|FAILED)\. (\d+) passed; (\d+) failed; (\d+) ignored;")


def output(*command):
    return subprocess.check_output(command, text=True).strip()


def cases(lines):
    pending = None
    result = Counter()
    for line in lines:
        if found := re.match(r"^test (.+?) \.\.\. (.*)$", line):
            pending = found[1].strip()
            line = found[2]
        if pending is not None and (found := re.fullmatch(r"(ok|ignored|FAILED)(?:, .*)?", line)):
            result[f"{found[1]}\t{pending}"] += 1
            pending = None
    return result


def run(command, env, log):
    headers, first = [], None
    with log.open("w") as stream:
        start = time.perf_counter()
        with subprocess.Popen(command, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              text=True, errors="replace") as child:
            for line in child.stdout:
                if found := HEADER.match(line):
                    if first is None:
                        first = time.perf_counter() - start
                    headers.append(found[1])
                stream.write(line)
            code = child.wait()
        elapsed = time.perf_counter() - start
    lines = log.read_text().splitlines()
    count = cases(lines)
    totals = [tuple(map(int, m.groups())) for line in lines if (m := SUMMARY.match(line))]
    notes = [line for line in lines if line.startswith("ra-jit:") and any(
        phrase in line for phrase in ("building with cargo", "this build is cargo's", "is cargo's to build", "rustc links"))]
    log.with_suffix(".cases").write_text("\n".join(sorted(count.elements())) + "\n")
    return dict(command=command, exit=code, wall_s=elapsed, first_test_s=first, targets=headers,
                summaries=totals, ra_jit_marker=any("profile [ra-jit] target(s)" in line for line in lines),
                fallback_notes=notes), count


def main():
    suite, trial, dest = sys.argv[1:]
    trial = int(trial)
    out = Path(dest).resolve()
    out.mkdir(parents=True, exist_ok=True)
    here = Path(__file__).resolve().parent
    package, target_name, selector, witness, target_src, features = CONFIG[suite]
    expected = Counter("ok\t" + name for name in (here / "expected" / f"{suite}.txt").read_text().splitlines())
    probe = os.environ.get("FILE_BENCH_PROBE") == "1"
    if probe:
        expected = Counter({"ok\t" + witness: 1})
    patch = here / "edits" / f"{suite}.patch"
    changed = re.findall(r"^--- a/(.+)$", patch.read_text(), re.M)
    assert len(changed) == 2
    originals = [Path(path).read_bytes() for path in changed]
    shutil.copyfile(patch, out / "edit.patch")
    target = Path("target").resolve()
    order = ["cargo", "blaze"] if trial % 2 else ["blaze", "cargo"]
    if probe:
        order = ["blaze"]
    job = dict(probe=probe, suite=suite, trial=trial, order=order, run=os.environ["GITHUB_RUN_ID"],
               workflow=os.environ["GITHUB_SHA"],
               downstream=output("git", "rev-parse", "HEAD"), blaze=output("git", "-C", "../blaze", "rev-parse", "HEAD"),
               rustc=output("rustc", "-vV"), cargo=output("cargo", "-V"), kernel=platform.release(),
               cpu=output("lscpu"), meminfo=Path("/proc/meminfo").read_text().splitlines()[0],
               lock=sha256(Path("Cargo.lock").read_bytes()).hexdigest(), edited=changed,
               selected_file=changed[1], selector=selector, expected_count=expected.total(), witness=witness,
               settings={k:os.environ[k] for k in ("CARGO_BUILD_JOBS", "RUST_TEST_THREADS", "RUST_MIN_STACK",
                                                    "CARGO_PROFILE_DEV_DEBUG", "BLAZE_SANDBOX")},
               measurements=[], negative_control=None)
    def save():
        (out / "measurements.json").write_text(json.dumps(job, indent=2) + "\n")
    save()
    group = ["--only-group", "test"] if suite == "uv" else ["--only-dev"]
    for engine in order:
        assert not target.exists() and [Path(p).read_bytes() for p in changed] == originals
        with tempfile.TemporaryDirectory(prefix="bf-") as scratch:
            daemon, store = (str(Path(scratch) / name) for name in ("home", "store"))
            env = dict(os.environ, CARGO_TARGET_DIR=str(target), BLAZE_HOME=daemon, RA_JIT_HOME=daemon,
                       BLAZE_CACHE=store, RA_JIT_CACHE=store, BLAZE_TESTS="all", RA_JIT_TESTS="all")
            cmd = ["uv", "run", "--locked", *group, engine, "test", "--locked", "-p", package,
                   "--test", target_name, *features, "--"]
            filters = ["--exact", selector] if suite == "ty" else [selector]
            if probe:
                filters = ["--exact", witness]
            try:
                for phase in ("clean", "edit"):
                    if phase == "edit":
                        subprocess.run(["git", "apply", patch], check=True)
                    row, actual = run([*cmd, *filters], env, out / f"{engine}-{phase}.log")
                    handoff = any("building with cargo" in line or "this build is cargo's" in line for line in row["fallback_notes"])
                    partial = any(any(f"`{name}`" in line for name in (target_name, package)) and
                                  ("is cargo's to build" in line or "rustc links" in line) for line in row["fallback_notes"])
                    backend = ("Cargo" if engine == "cargo" else "Cargo fallback" if handoff else
                               "mixed with rustc" if partial else "Blaze" if row["ra_jit_marker"] else "unconfirmed")
                    row.update(engine=engine, phase=phase, backend=backend, cases_match=actual == expected,
                               count=actual.total(), witness_ok=actual["ok\t" + witness] == 1,
                               valid=row["exit"] == 0 and row["targets"] == [target_src] and actual == expected
                               and row["summaries"] == [(expected.total(), 0, 0)])
                    job["measurements"].append(row)
                    print("MEASUREMENT", json.dumps(row), flush=True)
                    save()
                if probe and job["measurements"][-1]["backend"] == "Blaze":
                    production = Path(changed[0])
                    edited = production.read_bytes()
                    production.write_bytes(originals[0])
                    row, actual = run([*cmd, "--exact", witness], env, out / "blaze-negative-control.log")
                    row["confirmed"] = (row["exit"] != 0 and row["ra_jit_marker"] and not row["fallback_notes"]
                                        and row["targets"] == [target_src] and actual == Counter({"FAILED\t" + witness: 1})
                                        and row["summaries"] == [(0, 1, 0)])
                    job["blaze_negative_control"] = row
                    save()
                    production.write_bytes(edited)
                    row, actual = run([*cmd, "--exact", witness], env, out / "blaze-restored-control.log")
                    row["confirmed"] = (row["exit"] == 0 and row["ra_jit_marker"] and not row["fallback_notes"]
                                        and row["targets"] == [target_src] and actual == expected
                                        and row["summaries"] == [(expected.total(), 0, 0)])
                    job["blaze_restored_control"] = row
                    save()
                # This check runs after all four timed phases once per suite. Keep the new
                # assertion and restore only production code to verify that stale code fails.
                if not probe and trial == 2 and engine == "cargo":
                    assert order[-1] == engine
                    Path(changed[0]).write_bytes(originals[0])
                    row, actual = run([*cmd, "--exact", witness], env, out / "negative-control.log")
                    row["confirmed"] = (row["exit"] != 0 and row["targets"] == [target_src]
                                        and actual == Counter({"FAILED\t" + witness: 1})
                                        and row["summaries"] == [(0, 1, 0)])
                    job["negative_control"] = row
                    save()
            finally:
                if engine == "blaze":
                    if probe:
                        with (out / "compiler-refusals.txt").open("w") as trace:
                            for log in Path(daemon).rglob("log"):
                                if log.is_file():
                                    for line in log.open(errors="replace"):
                                        if "file-bench-forward:" in line or "file-bench-stale:" in line:
                                            trace.write(f"{log.relative_to(daemon)} {line}")
                    subprocess.run(["blaze", "stop"], env=env, timeout=90)
                for path, original in zip(changed, originals):
                    Path(path).write_bytes(original)
                if target.exists():
                    shutil.rmtree(target)
    if any(not row["valid"] or (row["engine"] == "blaze" and row["backend"] != "Blaze")
           for row in job["measurements"]) or (not probe and trial == 2 and not job["negative_control"]["confirmed"]):
        raise SystemExit("Test file validation failed; see measurements.json and the corresponding logs.")
    if probe and not all(job[key]["confirmed"] for key in ("blaze_negative_control", "blaze_restored_control")):
        raise SystemExit("The Blaze-only negative or restored witness failed validation.")


if __name__ == "__main__":
    main()
