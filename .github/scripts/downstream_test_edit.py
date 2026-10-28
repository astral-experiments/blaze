"""Check that Blaze applies a production edit and its test in the same downstream workspace."""

import os
from pathlib import Path
import subprocess
import sys


def replace_once(text, before, after):
    if text.count(before) != 1:
        raise ValueError(f"Expected one occurrence of {before!r} in the pinned checkout")
    return text.replace(before, after)


def run_test(command, witness, passed, message):
    run = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    print(run.stdout, flush=True)
    summary = "ok. 1 passed; 0 failed" if passed else "FAILED. 0 passed; 1 failed"
    handoffs = ("building with cargo", "this build is cargo's", "is cargo's to build", "rustc links")
    if any(line.startswith("ra-jit:") and any(text in line for text in handoffs)
           for line in run.stdout.splitlines()):
        raise SystemExit("Blaze handed off the selected build")
    if not (
        (run.returncode == 0) == passed
        and "profile [ra-jit] target(s)" in run.stdout
        and f"test {witness} ..." in run.stdout
        and f"test result: {summary}; 0 ignored" in run.stdout
        and (passed or message in run.stdout)
    ):
        raise SystemExit(f"Unexpected result for {witness}")


def main(suite):
    if suite == "uv-lock-edit":
        package, target = "uv", "lock"
        group = ["--only-group", "test"]
        features = ["--features", "test-python-patch,test-universal,native-auth"]
        witness = "lock::lock_validation_warning_chain"
        source = Path("crates/uv-lock-operations/src/lock.rs")
        test = Path("crates/uv/tests/lock/lock.rs")
        before, after = "Failed to validate existing lockfile", "Could not validate the existing uv.lock"
        old_assertion, new_assertion = before, after
    elif suite == "ty-semantic-edit":
        package, target = "ty_python_semantic", "mdtest"
        group = ["--only-dev"]
        features = ["--all-features"]
        witness = "mdtest::subscript/stepsize_zero.md"
        source = Path("crates/ty_python_semantic/src/types/diagnostic.rs")
        test = Path("crates/ty_python_semantic/resources/mdtest/subscript/stepsize_zero.md")
        before, after = "Slice step size cannot be zero", "A slice step must be nonzero"
        old_assertion = "values[1:10:0]  # error: [zero-stepsize-in-slice]"
        new_assertion = f'{old_assertion} "{after}"'
    else:
        raise ValueError(suite)

    original_source, original_test = source.read_text(), test.read_text()
    edited_source = replace_once(original_source, before, after)
    edited_test = replace_once(original_test, old_assertion, new_assertion)
    command = ["uv", "run", "--locked", *group, "blaze", "test", "--locked", "-p", package,
               "--test", target, *features, "--", "--exact", witness]
    os.environ["RA_JIT_TESTS"] = "all"
    run_test(command, witness, True, after)
    try:
        source.write_text(edited_source)
        if suite == "uv-lock-edit":
            # The old snapshot must see the new diagnostic before we update it.
            run_test(command, witness, False, after)
        test.write_text(edited_test)
        run_test(command, witness, True, after)
        # Keep the new assertion. A stale executable that ignores both edits would pass.
        source.write_text(original_source)
        run_test(command, witness, False, after)
    finally:
        source.write_text(original_source)
        test.write_text(original_test)


if __name__ == "__main__":
    main(sys.argv[1])
