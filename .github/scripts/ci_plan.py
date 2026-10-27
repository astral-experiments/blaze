"""Resolve Luna's selection or Blaze's event defaults before dispatching CI jobs."""

import html
import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "harness"))
from ci import focused


FAST = ["fmt", "linux-x64", "linux-arm64", "macos", "fork"]
SUITES = FAST + ["bench", "nix", "install"]
LINUX_CHECKS = ["borrowck", "pinned-ui", "bun-e2e"]
PLATFORMS = {"linux-x64": ("linux", "x86_64"), "linux-arm64": ("linux", "aarch64"),
             "macos": ("macos", "aarch64")}


def changed_paths(base, head, event):
    if not base or not head:
        return None
    endpoint = f"repos/{os.environ['GITHUB_REPOSITORY']}/compare/{base}...{head}?per_page=1"
    # The compare API lists at most 300 files. Both names of a rename matter.
    query = '[.status, (.files | if length > 0 and length < 300 then map(.filename, .previous_filename // empty) else null end)]'
    result = subprocess.run(["gh", "api", endpoint, "--jq", query], capture_output=True, text=True)
    if result.returncode:
        return None
    status, paths = json.loads(result.stdout)
    return paths if event == "pull_request" or status == "ahead" else None


def compiler_change(path):
    return (path.endswith((".rs", "/Cargo.toml")) or path in {"Cargo.toml", "Cargo.lock", "harness/toolchain"}
            or path.startswith(("crates/", "vendor/")) and not path.endswith(".md"))


def defaults(paths):
    suites = ["fmt"]
    if any(compiler_change(p) or p.startswith(("harness/", ".github/workflows/ra-jit", ".github/actions/"))
           or p == ".github/scripts/ci_plan.py" for p in paths):
        suites.append("linux-x64")
    if any(p.startswith("vendor/rust-analyzer/") and not p.endswith(".md") for p in paths):
        suites.append("fork")
    if any(p.startswith(("harness/bench", ".github/workflows/bench.yaml")) for p in paths):
        suites.append("bench")
    if any(p in {"Cargo.lock", "Cargo.toml", "flake.nix", "flake.lock", ".github/workflows/nix-live.yaml"}
           or p.startswith("nix/") or p.endswith("/Cargo.toml") for p in paths):
        suites.append("nix")
    if any(p in {"install.sh", ".github/workflows/install.yaml"} for p in paths):
        suites.append("install")
    return suites


def valid_plan(plan):
    if not isinstance(plan, dict) or not isinstance(plan.get("rationale"), str):
        return False
    if not isinstance(plan.get("suites"), list) or not isinstance(plan.get("tests"), list):
        return False
    for suite in plan["suites"]:
        if (not isinstance(suite, dict) or suite.get("id") not in SUITES + LINUX_CHECKS + ["full"]
                or not isinstance(suite.get("reason"), str)):
            return False
    for test in plan["tests"]:
        if (not isinstance(test, dict) or not isinstance(test.get("reason"), str)
                or test.get("platform") not in list(PLATFORMS) or test.get("backend") not in ("cranelift", "llvm")
                or not focused(test.get("name"), *PLATFORMS[test["platform"]], test["backend"])):
            return False
    return True


def resolve(event, tier, paths, plan, reason):
    if event in {"push", "pull_request"} and valid_plan(plan):
        requested = [item["id"] for item in plan["suites"]]
        if paths is None or any(map(compiler_change, paths)):
            requested.extend(["fmt", "linux-x64"])
        source = "luna"
    else:
        source = "fallback" if event in {"push", "pull_request"} else "event"
        plan = {"rationale": "Luna returned an invalid plan" if plan is not None else reason,
                "suites": [], "tests": []}
        if event in {"schedule", "workflow_dispatch"}:
            requested = FAST.copy()
        elif paths is None:
            requested = SUITES.copy()
            tier = "fast"
        else:
            requested = defaults(paths)
            if "linux-x64" in requested:
                plan["tests"].append({"platform": "linux-x64", "backend": "cranelift", "name": "difftest",
                                      "reason": "Check compiler behavior against rustc"})

    matrices = {platform: [] for platform in PLATFORMS}
    for test in plan["tests"]:
        platform = test["platform"]
        requested.append(platform)
        job = {"part": "test:" + test["name"], "backend": test["backend"]}
        if job not in matrices[platform]:
            matrices[platform].append(job)
    checks = [check for check in LINUX_CHECKS if check in requested]
    if checks:
        requested.append("linux-x64")
    if "full" in requested:
        tier = "full"
        requested.extend(FAST)
    suites = [suite for suite in SUITES if suite in requested]
    return dict(plan, source=source, tier=tier, run_suites=suites, linux_checks=checks, focused_matrix=matrices)


def main():
    env = os.environ
    event = env["GITHUB_EVENT_NAME"]
    paths = (changed_paths(env["BASE_SHA"], env["HEAD_SHA"], event)
             if event in {"push", "pull_request", "merge_group"} else None)
    reason = env.get("LUNA_REASON") or "Using the default suites for this event"
    try:
        choice = json.loads(env.get("LUNA_PLAN") or "null")
    except ValueError:
        choice, reason = None, "Luna returned an invalid plan"
    plan = resolve(event, env["TIER"], paths, choice, reason)
    plan.update(schema_version=2, model="gpt-6-luna" if plan["source"] == "luna" else None,
                event=event, base_sha=env["BASE_SHA"] or None, head_sha=env["HEAD_SHA"],
                run_id=env["GITHUB_RUN_ID"], run_attempt=env["GITHUB_RUN_ATTEMPT"])
    Path("luna-ci-plan.json").write_text(json.dumps(plan, indent=2) + "\n")
    with open(env["GITHUB_OUTPUT"], "a") as outputs:
        print(f"suites={json.dumps(plan['run_suites'])}", file=outputs)
        print(f"tier={plan['tier']}", file=outputs)
        print(f"tests={json.dumps(plan['focused_matrix'])}", file=outputs)
        print(f"checks={json.dumps(plan['linux_checks'])}", file=outputs)
    with open(env["GITHUB_STEP_SUMMARY"], "a") as summary:
        print(f"### CI plan ({plan['source']})\n\n<p>{html.escape(plan['rationale'])}</p>", file=summary)
        for item in plan["suites"]:
            print(f"- <code>{item['id']}</code> — <code>{html.escape(item['reason'])}</code>", file=summary)
        for t in plan["tests"]:
            print(f"- <code>{t['platform']} / {t['backend']} / {t['name']}</code> — {html.escape(t['reason'])}", file=summary)
        print(f"\nTier: {plan['tier']}. Suites to run: " + (", ".join(plan['run_suites']) or "none"), file=summary)


if __name__ == "__main__":
    main()
