import importlib.util
import pathlib
import unittest
from collections import Counter
from unittest.mock import patch


SCRIPT = pathlib.Path(__file__).parents[1] / "cdash_eamxx_report.py"
SPEC = importlib.util.spec_from_file_location("cdash_eamxx_report", SCRIPT)
REPORT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(REPORT)


class CollectTests(unittest.TestCase):
    def test_collect_excludes_only_old_matching_suites(self):
        now = 1_000_000
        cutoff = now - REPORT.MAX_SUITE_AGE_SECONDS
        builds = [
            {"id": "old", "site": "site", "buildname": "old-eamxx", "builddatefull": cutoff - 1},
            {"id": "old-other", "site": "site", "buildname": "old-other", "builddatefull": cutoff - 1},
            {"id": "boundary", "site": "site", "buildname": "boundary", "builddatefull": cutoff},
            {"id": "unknown", "site": "site", "buildname": "unknown"},
        ]
        tests = {
            "old": [{"id": "1", "name": "eamxx-old", "status": "FAILED", "details": ""}],
            "old-other": [{"id": "2", "name": "other", "status": "FAILED", "details": ""}],
            "boundary": [{"id": "3", "name": "eamxx-boundary", "status": "PASSED", "details": ""}],
            "unknown": [{"id": "4", "name": "eamxx-unknown", "status": "PASSED", "details": ""}],
        }
        triaged = []

        def paged(query, build_id, field, variables=None):
            self.assertIs(query, REPORT.TESTS_Q)
            return tests[build_id]

        index = {"date": "2026-11-04", "buildgroups": [{"name": "filtered", "builds": builds}]}
        with patch.object(REPORT, "GROUPS", {"filtered": "eamxx"}), \
             patch.object(REPORT, "http_json", return_value=index), \
             patch.object(REPORT, "paged", side_effect=paged), \
             patch.object(REPORT, "triage_build_tests", side_effect=lambda build_id, _: triaged.append(build_id)):
            result = REPORT.collect("2026-11-04", now=now)

        group = result["groups"][0]
        self.assertEqual(group["excluded_old"], 1)
        self.assertEqual([b["name"] for b in group["builds"]], ["boundary", "unknown"])
        self.assertEqual(group["builds"][0]["started_at"], cutoff)
        self.assertEqual(triaged, ["boundary", "unknown"])


class SectionTests(unittest.TestCase):
    def test_standalone_suite_classification(self):
        self.assertTrue(REPORT.is_standalone_suite("scream_unit_tests_full_debug"))
        self.assertTrue(REPORT.is_standalone_suite("scripts_tests"))
        self.assertFalse(REPORT.is_standalone_suite("e3sm_eamxx_v1_master_gnugpu"))

    def test_report_separates_standalone_tests_and_coverage(self):
        rep = {"date": "2026-11-04", "coverage": [{
            "buildid": "coverage", "site": "site", "buildname": "unit coverage",
            "percentage": 80.0, "loctested": 8, "locuntested": 2,
            "emoji": ":large_green_circle:",
        }], "groups": [{
            "name": "group", "mode": "all", "missing": False, "excluded_old": 0,
            "excluded_old_cime": 0, "excluded_old_standalone": 0,
            "builds": [
                {"id": "cime", "site": "site", "name": "e3sm_master", "tests": [],
                 "counts": Counter({"PASS": 3}), "orphan_build_errors": 0, "commit": None},
                {"id": "unit", "site": "site", "name": "scream_unit_tests_release", "tests": [],
                 "counts": Counter({"PASS": 6000}), "orphan_build_errors": 0, "commit": None},
                {"id": "scripts", "site": "site", "name": "scripts_tests", "tests": [],
                 "counts": Counter({"PASS": 2}), "orphan_build_errors": 0, "commit": None},
            ],
        }]}

        report = REPORT.format_report(rep)
        markdown = REPORT.format_markdown_report(rep)
        summary = REPORT.format_slack_summary(rep, None)
        thread = REPORT.format_thread(rep, with_file=False)

        self.assertLess(report.index("CIME-based tests"), report.index("Standalone tests"))
        self.assertIn("CIME-based tests* · 1 suites · 3 tests", report)
        self.assertIn("Standalone tests* · 2 suites · 6,002 tests", report)
        self.assertIn("Coverage", report)
        self.assertIn("### CIME-based tests", markdown)
        self.assertIn("### Standalone tests", markdown)
        self.assertIn("| group | 1 | 3 | 0 | 0 | 0 | 0 |", markdown)
        self.assertIn("| group | 2 | 6,002 | 0 | 0 | 0 | 0 |", markdown)
        self.assertEqual(summary.count("Coverage"), 1)
        self.assertIn("CIME-based tests", thread)
        self.assertIn("Standalone tests", thread)


class FormattingTests(unittest.TestCase):
    def test_compact_output_omits_passing_suite_rows_but_keeps_failures(self):
        rep = {"date": "2026-11-04", "coverage": [], "groups": [{
            "name": "group", "mode": "all", "missing": False, "excluded_old": 2,
            "builds": [
                {"id": "1", "site": "site", "name": "passing-suite", "tests": [],
                 "counts": Counter({"PASS": 1}), "orphan_build_errors": 0, "commit": None},
                {"id": "2", "site": "site", "name": "failing-suite", "tests": [{
                    "id": "test-1", "name": "failure-name", "bucket": "FAIL", "phase": None,
                    "likely_cause": "the likely cause",
                }], "counts": Counter({"FAIL": 1}), "orphan_build_errors": 0, "commit": None,
                 "started_at": 0},
            ],
        }]}

        report = REPORT.format_report(rep)
        markdown = REPORT.format_markdown_report(rep)
        summary = REPORT.format_slack_summary(rep, None)
        thread = REPORT.format_thread(rep, with_file=False)

        self.assertIn("Suites excluded as older than 18 hours:* 2", report)
        self.assertNotIn("passing-suite", report)
        self.assertIn("failure-name", report)
        self.assertIn("the likely cause", report)
        self.assertIn("| Group | Suites | PASS | FAIL | BUILD FAIL | DIFF | Not run |", markdown)
        self.assertIn("| group | 2 | 1 | 1 | 0 | 0 | 0 |", markdown)
        self.assertIn("- Started: 1970-01-01 00:00 UTC", markdown)
        self.assertIn("Suites excluded as older than 18 hours:* 2", summary)
        self.assertNotIn("passing-suite", thread)
        self.assertIn("failure-name", thread)
        self.assertNotIn("Started:", thread)

    def test_shared_diff_diagnostics_are_summarized_for_suite(self):
        diagnostics = [
            "field lists differ (otherwise bit-for-bit)",
            "baseline directory is missing",
        ]
        diffs = [
            {"id": "test-1", "name": "diff-one", "bucket": "DIFF", "bless": None,
             "diff_diagnostics": diagnostics},
            {"id": "test-2", "name": "diff-two", "bucket": "DIFF", "bless": None,
             "diff_diagnostics": diagnostics},
        ]
        rep = {"date": "2026-11-04", "coverage": [], "groups": [{
            "name": "group", "mode": "all", "missing": False, "excluded_old": 0,
            "builds": [{"id": "1", "site": "site", "name": "suite", "tests": diffs,
                        "counts": Counter({"DIFF": 2}), "orphan_build_errors": 0,
                        "commit": None}],
        }]}

        report = REPORT.format_report(rep)
        self.assertIn("All 2: _bless info not found in log_ · field lists differ "
                      "(otherwise bit-for-bit) · baseline directory is missing", report)
        self.assertNotIn("diff-one", report)
        self.assertNotIn("diff-two", report)
        self.assertNotIn("/some/private/path", report)

    def test_diff_tallies_group_matching_baselines_and_diagnostics(self):
        diffs = (
            [{"bless": ("5b1427fe20eb1234", "2026-09-23_12:34:51"),
              "diff_diagnostics": ["field lists differ (otherwise bit-for-bit)"]}] * 6
            + [{"bless": None, "diff_diagnostics": ["baseline directory is missing"]}] * 16
            + [{"bless": ("ca0b1c1b1da1234", "2026-10-15_09:32:39"), "diff_diagnostics": []}] * 12
        )

        tallies = "\n".join(REPORT.format_diff_tallies(diffs))
        self.assertIn("6: sha `5b1427fe20eb` · blessed 2026-09-23_12:34:51 · field lists differ "
                      "(otherwise bit-for-bit)", tallies)
        self.assertIn("16: _bless info not found in log_ · baseline directory is missing", tallies)
        self.assertIn("12: sha `ca0b1c1b1da1` · blessed 2026-10-15_09:32:39", tallies)


if __name__ == "__main__":
    unittest.main()
