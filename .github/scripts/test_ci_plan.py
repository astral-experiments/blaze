"""Check the ordinary CI budget boundary and full-matrix escape paths."""
import unittest

from ci_plan import FAST, PLATFORMS, SUITES, resolve


def choice(suites=(), tests=()):
    return {"rationale": "Exercise the change", "suites": [{"id": s, "reason": s} for s in suites],
            "tests": [{"platform": p, "backend": b, "name": n, "reason": n} for p, b, n in tests]}


class Routing(unittest.TestCase):
    def test_compiler_change_runs_only_selected_backends_and_regressions(self):
        paths = ["crates/ra-jit/src/cache.rs"]
        tests = [("linux-x64", b, "cached-type-ids") for b in ("cranelift", "llvm")]
        for event in ("pull_request", "push"):
            plan = resolve(event, "focused", paths, choice(tests=tests + tests[:1]), "")
            self.assertEqual(plan["run_suites"], ["fmt", "linux-x64"])
            self.assertEqual(plan["tier"], "focused")
            self.assertEqual(plan["linux_checks"], [])
            self.assertEqual(plan["focused_matrix"], {
                "linux-x64": [{"part": "test:cached-type-ids", "backend": b} for b in ("cranelift", "llvm")],
                "linux-arm64": [], "macos": [],
            })

    def test_empty_build_only_and_individual_checks(self):
        for paths, suites, expected in [
                (["docs/harness.md"], [], []),
                (None, [], ["fmt", "linux-x64"]),
                (["crates/ra-jit/src/lib.rs"], [], ["fmt", "linux-x64"]),
                (["harness/ui-pinned.txt"], ["pinned-ui"], ["linux-x64"])]:
            plan = resolve("pull_request", "focused", paths, choice(suites), "")
            self.assertEqual(plan["run_suites"], expected)
            self.assertEqual(plan["linux_checks"], suites)
            self.assertFalse(any(plan["focused_matrix"].values()))

    def test_invalid_test_and_unsupported_backend_use_the_fallback(self):
        for test in [("linux-x64", "llvm", "../bad"), ("linux-arm64", "llvm", "headerpad"),
                     ("macos", "llvm", "tiers")]:
            plan = resolve("push", "focused", ["src/lib.rs"], choice(tests=[test]), "")
            self.assertEqual(plan["source"], "fallback")
            self.assertEqual(plan["focused_matrix"]["linux-x64"], [{"part": "test:difftest", "backend": "cranelift"}])

    def test_wide_runs(self):
        for event, tier, expected, suites, paths, model in [
                ("schedule", "full", "full", FAST, None, None),
                ("workflow_dispatch", "full", "full", FAST, None, None),
                ("workflow_dispatch", "fast", "fast", FAST, None, None),
                ("pull_request", "focused", "full", FAST, ["crates/a.rs"], choice(["full"])),
                ("merge_group", "focused", "fast", SUITES, None, None)]:
            with self.subTest(event=event, tier=tier):
                plan = resolve(event, tier, paths, model, "")
                self.assertEqual((plan["tier"], plan["run_suites"]), (expected, suites))
                self.assertEqual(set(plan["focused_matrix"]), set(PLATFORMS))


if __name__ == "__main__":
    unittest.main()
