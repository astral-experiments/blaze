"""Timed clean, failing, and repaired downstream test runs with isolated build outputs."""
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
import threading
import time
from bench import CONFIG, output, run


def watch_worker(home, stop, destination):
    start = time.monotonic()
    offsets = {}
    with destination.open("w", buffering=1) as stream:
        while not stop.is_set():
            for log in home.rglob("log"):
                try:
                    with log.open(errors="replace") as source:
                        key = os.fstat(source.fileno()).st_ino
                        source.seek(offsets.get(key, 0))
                        for line in source:
                            stream.write(f"{time.monotonic()-start:.3f}\t{log.relative_to(home)}\t{line}")
                        offsets[key] = source.tell()
                except FileNotFoundError:
                    pass
            stop.wait(0.15)


def main():
    suite, trial, dest = sys.argv[1:]
    trial = int(trial)
    out = Path(dest).resolve()
    out.mkdir(parents=True, exist_ok=True)
    here = Path(__file__).resolve().parent
    mode = os.environ["COLD_MODE"]
    trace = mode == "profile"
    full = mode == "file"
    package, target_name, selector, witness, target_src, features = CONFIG[suite]
    expected = Counter("ok\t" + name for name in (here / "expected" / f"{suite}.txt").read_text().splitlines())
    patch = here / "edits" / f"{suite}.patch"
    changed = [Path(p) for p in re.findall(r"^--- a/(.+)$", patch.read_text(), re.M)]
    assert len(changed) == 2
    original = [p.read_bytes() for p in changed]
    subprocess.run(["git", "apply", "--check", patch], check=True)
    shutil.copy(patch, out / "edit.patch")
    repo = Path("../blaze").resolve()
    paths = {"blaze": repo}
    other = Path("../reference").resolve()
    if other.exists():
        paths["reference"] = other
    if trace:
        order = ["blaze"]
    elif "reference" in paths:
        order = {1:["cargo","reference","blaze"],2:["blaze","reference","cargo"],
                 3:["cargo","blaze","reference"],4:["reference","blaze","cargo"]}[trial]
    else:
        order = ["cargo","blaze"] if trial % 2 else ["blaze","cargo"]
    settings = {k:os.environ[k] for k in ("CARGO_BUILD_JOBS", "RUST_TEST_THREADS","RUST_MIN_STACK","CARGO_PROFILE_DEV_DEBUG")}
    group = ["--only-group","test"] if suite == "uv" else ["--only-dev"]
    job = dict(suite=suite, trial=trial, mode=mode, order=order, settings=settings, run=os.environ["GITHUB_RUN_ID"],
               workflow=os.environ["GITHUB_SHA"], downstream=output("git","rev-parse","HEAD"),
               revisions={k:output("git","-C",str(p),"rev-parse","HEAD") for k,p in paths.items()},
               lock=sha256(Path("Cargo.lock").read_bytes()).hexdigest(), patch=sha256(patch.read_bytes()).hexdigest(),
               rustc=output("rustc","-vV"), cargo=output("cargo","-V"), cpu=output("lscpu"),
               kernel=platform.release(), meminfo=Path("/proc/meminfo").read_text().splitlines()[0], rows=[])
    def save():
        (out/"measurements.json").write_text(json.dumps(job,indent=2)+"\n")
    save()
    target = Path("target").resolve()
    for which in order:
        engine = "cargo" if which == "cargo" else "blaze"
        assert not target.exists() and [p.read_bytes() for p in changed] == original
        with tempfile.TemporaryDirectory(prefix="uc-") as scratch:
            home, store = (Path(scratch)/s for s in ("home","store"))
            env = dict(os.environ, CARGO_TARGET_DIR=str(target), BLAZE_HOME=str(home), RA_JIT_HOME=str(home),
                       BLAZE_CACHE=str(store), RA_JIT_CACHE=str(store), BLAZE_TESTS="all", RA_JIT_TESTS="all")
            if which in paths:
                env["PATH"] = str(paths[which]/"target/release")+":"+env["PATH"]
            flags = ([selector] if suite != "ty" else ["--exact",selector]) if full else ["--exact",witness]
            cmd = ["uv","run","--locked",*group,engine,"test","--locked","-p",package,"--test",target_name,*features,"--",*flags]
            stop = threading.Event()
            if trace:
                sampler = threading.Thread(target=watch_worker,args=(home,stop,out/"worker-timeline.tsv"),daemon=True)
                sampler.start()
            try:
                for phase in (("clean","edit") if full else ("clean","fail","fix")):
                    if phase == "edit":
                        subprocess.run(["git","apply",patch],check=True)
                    elif phase in ("fail","fix"):
                        path = changed[phase=="fix"]
                        subprocess.run(["git","apply","--include="+str(path),patch],check=True)
                    if trace:
                        env["RA_JIT_STATS"]="1"
                        env["RA_JIT_TRACE"]=str(out/f"{which}-{phase}-trace.json")
                    log=out/f"{which}-{phase}.log"
                    row, actual=run(cmd,env,log)
                    if trace:
                        plans = []
                        for state in (home/"states").glob("*.json"):
                            if state.name.endswith(".kept.json"): continue
                            view=json.loads(state.read_text())
                            for checkout in view["checkouts"]:
                                for plan in checkout["programs"]:
                                    plans.append(dict(root=plan["crates"][0], crates=len(plan["crates"]),
                                                      rustc_binary=plan.get("rustc_binary"), whole=plan["whole"]))
                        (out/f"{which}-{phase}-plans.json").write_text(json.dumps(plans,indent=2))
                    success = phase != "fail"
                    want = expected if full else Counter({("ok" if success else "FAILED")+"\t"+witness:1})
                    backend = which=="cargo" or (row["ra_jit_marker"] and not row["fallback_notes"])
                    mismatch = True
                    if not success:
                        txt = log.read_text()
                        mismatch = "Failed to validate existing lockfile" in txt and "Could not validate the existing uv.lock" in txt
                    row.update(tool=which,phase=phase,cases_match=actual==want,n=actual.total(),
                               valid=(row["exit"]==(0 if success else 101) and row["targets"]==[target_src]
                               and row["summaries"]==[(want.total() if success else 0,0 if success else 1,0)]
                               and actual==want and backend and mismatch))
                    job["rows"].append(row); save()
                    print(which,phase,round(row["wall_s"],2),round(row["first_test_s"] or 0,2),row["valid"],flush=True)
            finally:
                if trace:
                    stop.set();sampler.join(4)
                    for log in [*home.rglob("log"),*home.glob("daemon.log")]:
                        if log.is_file():
                            shutil.copyfile(log,out/("worker-"+"-".join(log.relative_to(home).parts)))
                if engine=="blaze":
                    subprocess.run(["blaze","stop"],env=env,timeout=90)
                for path,content in zip(changed,original):
                    path.write_bytes(content)
                if target.exists():
                    shutil.rmtree(target)
    if not all(r["valid"] for r in job["rows"]):
        raise SystemExit("Incorrect test verdict, selection or backend")


if __name__ == "__main__":
    main()
