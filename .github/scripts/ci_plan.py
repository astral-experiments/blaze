"""Resolve Luna's selection or Blaze's event defaults before dispatching CI jobs."""

import html
import json
import os
import subprocess
from pathlib import Path


FAST = ["fmt", "linux-x64", "linux-arm64", "macos", "fork"]
SUITES = FAST + ["bench", "nix", "install"]


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


def defaults(event, paths):
    if event in {"schedule", "workflow_dispatch"}:
        return FAST.copy()
    # None means the comparison is incomplete or unavailable. Cover every default in that case.
    def touches(predicate):
        return paths is None or any(predicate(path) for path in paths)

    def code(path):
        return (path.startswith((".github/workflows/ra-jit", ".github/actions/"))
                or path == ".github/scripts/ci_plan.py"
                or not (path.endswith(".md") or path.startswith(("docs/", "site/", "LICENSE", ".github/"))))

    suites = FAST.copy() if event != "pull_request" and touches(code) else ["fmt"]
    if event != "merge_group":
        if touches(lambda p: not p.endswith(".md") and not p.startswith(("site/", "harness/"))):
            suites.append("bench")
        if touches(lambda p: p in {"Cargo.lock", "Cargo.toml", "flake.nix", "flake.lock", ".github/workflows/nix-live.yaml"}
                   or p.startswith("nix/") or p.endswith("/Cargo.toml")):
            suites.append("nix")
        if touches(lambda p: p in {"install.sh", ".github/workflows/install.yaml"}):
            suites.append("install")
    return suites


def valid_plan(plan):
    if not isinstance(plan, dict) or not isinstance(plan.get("rationale"), str):
        return False
    return isinstance(plan.get("suites"), list) and all(
        isinstance(item, dict) and item.get("id") in SUITES and isinstance(item.get("reason"), str)
        for item in plan["suites"]
    )


def main():
    env = os.environ
    event = env["GITHUB_EVENT_NAME"]
    tier = env["TIER"]
    try:
        plan = json.loads(env.get("LUNA_PLAN") or "null")
    except ValueError:
        plan = None
    if valid_plan(plan):
        suites = list(dict.fromkeys(item["id"] for item in plan["suites"]))
        source = "luna"
    else:
        paths = (changed_paths(env["BASE_SHA"], env["HEAD_SHA"], event)
                 if event in {"push", "pull_request", "merge_group"} else None)
        suites = defaults(event, paths)
        source = "fallback" if event in {"push", "pull_request"} else "event"
        reason = env.get("LUNA_REASON") or "Using the default suites for this event"
        if env.get("LUNA_PLAN"):
            reason = "Luna returned an invalid plan"
        plan = {"rationale": reason, "suites": []}
    plan.update(schema_version=1, source=source, model="gpt-6-luna" if source == "luna" else None,
                event=event, base_sha=env["BASE_SHA"] or None, head_sha=env["HEAD_SHA"],
                run_id=env["GITHUB_RUN_ID"], run_attempt=env["GITHUB_RUN_ATTEMPT"],
                tier=tier, run_suites=suites)
    Path("luna-ci-plan.json").write_text(json.dumps(plan, indent=2) + "\n")
    with open(env["GITHUB_OUTPUT"], "a") as outputs:
        print(f"suites={json.dumps(suites)}", file=outputs)
        print(f"tier={tier}", file=outputs)
    with open(env["GITHUB_STEP_SUMMARY"], "a") as summary:
        print(f"### CI plan ({source})\n\n<p>{html.escape(plan['rationale'])}</p>", file=summary)
        for item in plan["suites"]:
            print(f"- <code>{item['id']}</code> — <code>{html.escape(item['reason'])}</code>", file=summary)
        print("\nSuites to run: " + (", ".join(suites) or "none"), file=summary)


if __name__ == "__main__":
    main()
