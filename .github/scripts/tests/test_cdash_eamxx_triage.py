"""Tests for the CDash report's building blocks: classifying tests, explaining
failures from logs, and formatting for Slack. The end-to-end flow is covered in
test_cdash_eamxx_report.py."""
import importlib.util
import pathlib
import unittest
from collections import Counter

SCRIPT = pathlib.Path(__file__).parents[1] / "cdash_eamxx_report.py"
SPEC = importlib.util.spec_from_file_location("cdash_eamxx_report", SCRIPT)
REPORT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(REPORT)


def make_test(**kw):
    return {"status": "FAILED", "details": "", "name": "some-test", **kw}


class ClassifyTests(unittest.TestCase):
    def test_passed_and_not_run(self):
        self.assertEqual(REPORT.classify(make_test(status="PASSED"), ""), "PASS")
        self.assertEqual(REPORT.classify(make_test(status="NOT_RUN"), ""), "NOTRUN")

    def test_diff_is_recognized_from_details(self):
        self.assertEqual(REPORT.classify(make_test(details="Baseline comparison (DIFF)"), ""), "DIFF")

    def test_build_failures(self):
        # by the phase it failed in...
        self.assertEqual(REPORT.classify(make_test(phase="MODEL_BUILD"), ""), "BUILDFAIL")
        # ...or because the build output names it
        self.assertEqual(REPORT.classify(make_test(name="ERS.ne4"), "error building ERS.ne4"), "BUILDFAIL")

    def test_anything_else_is_a_run_failure(self):
        self.assertEqual(REPORT.classify(make_test(phase="RUN"), "unrelated"), "FAIL")
        self.assertEqual(REPORT.classify(make_test(), ""), "FAIL")

    def test_diff_diagnostics_are_concise(self):
        log = "\n".join([
            "master: FIELDLIST field lists differ (otherwise bit-for-bit)",
            "master: ERROR BFAIL baseline directory /some/private/path does not exist",
        ])
        self.assertEqual(REPORT.diff_diagnostics(log), [
            "field lists differ (otherwise bit-for-bit)",
            "baseline directory is missing",
        ])
        self.assertNotIn("private", " ".join(REPORT.diff_diagnostics(log)))

    def test_unrelated_output_has_no_diff_diagnostics(self):
        self.assertEqual(REPORT.diff_diagnostics("Completed (DIFF)"), [])


class TextHelperTests(unittest.TestCase):
    def test_short_collapses_whitespace_and_truncates(self):
        self.assertEqual(REPORT.short("a   b\n  c"), "a b c")
        self.assertEqual(REPORT.short("x" * 10, 5), "xxxx…")
        self.assertEqual(len(REPORT.short("x" * 500, 160)), 160)

    def test_slack_escape_leaves_link_syntax_to_the_caller(self):
        self.assertEqual(REPORT.slack_escape("a<b>&c"), "a&lt;b&gt;&amp;c")
        self.assertEqual(REPORT.display("<x>", slack=True), "&lt;x&gt;")
        self.assertEqual(REPORT.display("<x>", slack=False), "<x>")

    def test_chunks_keep_all_the_text_and_respect_the_size(self):
        text = "\n".join(f"line {i:02d} " + "x" * 30 for i in range(40))
        parts = list(REPORT.chunks(text, 200))
        self.assertGreater(len(parts), 1)
        self.assertTrue(all(len(p) <= 200 for p in parts))
        self.assertEqual("".join(parts).split(), text.split())

    def test_find_and_excerpt(self):
        self.assertEqual(REPORT.find("a\nfoo bar\nc", "bar"), (1, "foo bar"))
        self.assertIsNone(REPORT.find("a", "zzz"))
        ex = REPORT.excerpt("f.log", "\n".join(map(str, range(20))), 10, before=2, after=2)
        self.assertEqual(ex, {"file": "f.log", "start": 9, "lines": ["8", "9", "10", "11"]})

    def test_counts_str_always_shows_the_main_buckets(self):
        zero = Counter({k: 0 for k in REPORT.BUCKETS})
        out = REPORT.counts_str(zero)
        for label in ("PASS", "FAIL", "BUILD FAIL", "DIFF"):
            self.assertIn(label, out)
        self.assertNotIn("not run", out)  # hidden while zero
        zero["NOTRUN"] = 2
        self.assertIn("2 not run", REPORT.counts_str(zero))

    def test_posted_note_says_how_much_reached_slack(self):
        self.assertEqual(REPORT.posted_note(1), ". Nothing was posted.")
        self.assertEqual(REPORT.posted_note(2), ". Message 1 was posted.")
        self.assertEqual(REPORT.posted_note(5), ". Messages 1-4 were posted.")


class LogAnalysisTests(unittest.TestCase):
    def test_warning_flood_needs_many_repeats_and_ignores_numbers(self):
        # same warning with different numbers counts as one
        log = "\n".join(f"0: WARNING: value {i * 1.5e-3} out of range" for i in range(120))
        n, example = REPORT.warning_flood(log)
        self.assertEqual(n, 120)
        self.assertTrue(example.startswith("WARNING: value"))
        self.assertIsNone(REPORT.warning_flood("warn a\nwarn b"))

    def test_warning_flood_ignores_known_noise(self):
        self.assertIsNone(REPORT.warning_flood("\n".join(["1: PIO: WARNING: harmless"] * 500)))

    def test_abort_message_comes_from_the_aborting_rank(self):
        log = "\n".join([
            "3: ERROR: Negative water vapor in column 12",
            "0: ERROR: an unrelated message from another rank",
            "application called MPI_Abort(MPI_COMM_WORLD, 1) - process 3",
        ])
        line_no, msg = REPORT.abort_message(log)
        self.assertEqual(line_no, 0)
        self.assertEqual(msg, "ERROR: Negative water vapor in column 12")

    def test_abort_without_a_trustworthy_message(self):
        self.assertIsNone(REPORT.abort_message("application called MPI_Abort(MPI_COMM_WORLD, 1) - process 0"))
        self.assertIsNone(REPORT.abort_message("all fine\nnothing to see"))


class TriageBuildTests(unittest.TestCase):
    def triage(self, text, output=""):
        return REPORT.triage_build({"EXEROOT": [("e3sm.bldlog.250101", text)]}, output)

    def test_disk_full(self):
        cause, evidence = self.triage("compiling\nNo space left on device\n")
        self.assertEqual(cause, "Disk full during build")
        self.assertEqual(evidence[0]["lines"], ["compiling", "No space left on device"])

    def test_compiler_killed_is_not_blamed_on_the_code(self):
        cause, _ = self.triage("Killed signal terminated program cc1plus\n")
        self.assertIn("compiler was killed", cause)
        self.assertIn("not a code error", cause)

    def test_compile_error_names_the_error(self):
        cause, evidence = self.triage("ok\nfoo.cpp:3: error: 'x' was not declared\nmore\n")
        self.assertEqual(cause, "Compile error in the e3sm build: error: 'x' was not declared")
        self.assertEqual(evidence[0]["file"], "e3sm.bldlog.250101")

    def test_cmake_error_is_labelled_as_configuration(self):
        cause, _ = self.triage("CMake Error at CMakeLists.txt:10: missing package\n")
        self.assertTrue(cause.startswith("CMake configuration error"))

    def test_disk_full_wins_over_a_later_compile_error(self):
        cause, _ = self.triage("error: something\nNo space left on device\n")
        self.assertEqual(cause, "Disk full during build")

    def test_no_logs(self):
        self.assertEqual(REPORT.triage_build({}, "cmake config failed"),
                         ("CMake configuration of the model failed (no build log uploaded)", []))
        self.assertEqual(REPORT.triage_build({}, "")[0], "Build failed; no build log was uploaded to CDash")


class TriageRunTests(unittest.TestCase):
    def test_model_abort_reports_the_reason(self):
        log = "3: ERROR: Negative water vapor in column 12\napplication called MPI_Abort(MPI_COMM_WORLD, 1) - process 3"
        cause, evidence = REPORT.triage_run({}, {"e3sm": [("e3sm.log.1", log)]})
        self.assertEqual(cause, "Model aborted: ERROR: Negative water vapor in column 12")
        self.assertEqual(evidence[0]["file"], "e3sm.log.1")

    def test_falls_back_to_cdash_output_when_no_run_logs(self):
        cause, evidence = REPORT.triage_run({"output": "something went wrong"}, {})
        self.assertIn("no run logs were uploaded", cause)
        self.assertEqual(evidence[0]["file"], "CDash test output")

    def test_nothing_to_go_on(self):
        self.assertEqual(REPORT.triage_run({"output": ""}, {}), ("Run failed; no run logs were uploaded to CDash", []))

    def test_unrecognized_log_points_at_the_end_of_the_log(self):
        cause, evidence = REPORT.triage_run({}, {"e3sm": [("e3sm.log.1", "step 1\nstep 2\nstep 3\n")]})
        self.assertIn("no recognized error message", cause)
        self.assertEqual(evidence[0]["lines"][-1], "step 3")


if __name__ == "__main__":
    unittest.main()
