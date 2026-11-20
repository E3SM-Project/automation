import importlib.util
import io
import pathlib
import unittest
import urllib.error
from collections import Counter
from unittest.mock import patch


SCRIPT = pathlib.Path(__file__).parents[1] / "cdash_eamxx_report.py"
SPEC = importlib.util.spec_from_file_location("cdash_eamxx_report", SCRIPT)
REPORT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(REPORT)


class CollectTests(unittest.TestCase):
    def test_baseline_merge_pr_is_looked_up_once_across_suites(self):
        sha = "5b1427fe20eb1234"
        builds = [{"id": build_id, "site": "site", "buildname": f"eamxx_{build_id}",
                   "builddatefull": 1_000_000} for build_id in ("one", "two")]
        index = {"date": "2026-11-19", "buildgroups": [{"name": "E3SM_EAMXX", "builds": builds}]}

        def paged(query, build_id, field, variables=None):
            if query is REPORT.TESTS_Q:
                return [{"id": build_id, "name": "eamxx-test", "status": "FAILED",
                         "details": "Completed (DIFF)"}]
            self.assertIs(query, REPORT.FAILED_LOGS_Q)
            return [{"id": build_id, "output": f"Most recent bless: sha:{sha} date:2026-11-18_12:00:00"}]

        with patch.object(REPORT, "GROUPS", {"E3SM_EAMXX": "all"}), \
             patch.object(REPORT, "http_json", return_value=index), \
             patch.object(REPORT, "paged", side_effect=paged), \
             patch.object(REPORT, "lookup_merge_pr", return_value="1234") as lookup, \
             patch.object(REPORT, "lookup_pr_title", return_value="Fix & clarify baselines") as title_lookup, \
             patch.object(REPORT, "triage_build_tests"), \
             patch.object(REPORT, "find_commit", return_value=None):
            result = REPORT.collect("2026-11-19", now=1_000_000, profile=REPORT.PROFILE_CIME)

        lookup.assert_called_once_with(sha)
        title_lookup.assert_called_once_with("1234")
        self.assertEqual([b["tests"][0]["baseline_pr"] for b in result["groups"][0]["builds"]],
                         ["1234", "1234"])
        self.assertIn("Merge PR #1234> · Fix &amp; clarify baselines",
                      REPORT.format_report(result, slack=True))
        self.assertIn("[Merge PR #1234](https://github.com/E3SM-Project/E3SM/pull/1234)"
                      " · Fix & clarify baselines",
                      REPORT.format_markdown_report(result))

    def test_coverage_build_error_without_failed_test_is_reported_separately(self):
        build = {"id": "coverage-build", "site": "ghci-cuda",
                 "buildname": "scream_unit_tests_test_coverage", "builddatefull": 1_000_000,
                 "compilation": {"error": 1}}
        index = {"date": "2026-11-19", "buildgroups": [{"name": "E3SM_EAMXX", "builds": [build]}]}

        def paged(query, build_id, field, variables=None):
            self.assertEqual(build_id, "coverage-build")
            if query is REPORT.TESTS_Q:
                return [{"id": "passing", "name": "passing-test", "status": "PASSED", "details": ""}]
            self.assertIs(query, REPORT.BUILD_ERRORS_Q)
            return [{"stdError": "test_coverage failed to build", "stdOutput": ""}]

        with patch.object(REPORT, "GROUPS", {"E3SM_EAMXX": "all"}), \
             patch.object(REPORT, "http_json", return_value=index), \
             patch.object(REPORT, "paged", side_effect=paged), \
             patch.object(REPORT, "triage_build_tests"), \
             patch.object(REPORT, "find_commit", return_value=None):
            result = REPORT.collect("2026-11-19", now=1_000_000, profile=REPORT.PROFILE_COVERAGE)

        suite = result["groups"][0]["builds"][0]
        self.assertEqual(suite["counts"], Counter({"PASS": 1}))
        self.assertEqual(suite["orphan_build_errors"], 1)
        summary = REPORT.format_slack_summary(result, None)
        table = summary.split("```")[1].strip("\n").splitlines()
        self.assertEqual(table[2].split(), ["E3SM_EAMXX", "1", "0", "0", "0", "1", "0", "1", "1"])
        self.assertEqual(table[3].split(), ["Total", "1", "0", "0", "0", "1", "0", "1", "1"])
        self.assertIn("scream_unit_tests_test_coverage", summary)
        self.assertIn("Build errors are not tied to a test", summary)
        self.assertIn("| E3SM_EAMXX | 1 | 0 | 0 | 0 | 1 | 0 | 1 |",
                      REPORT.format_markdown_report(result))
        self.assertIn("build errors 1", REPORT.format_report(result))

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

    def test_profiles_filter_suites_before_fetching_tests(self):
        builds = [
            {"id": "cime", "site": "site", "buildname": "e3sm_eamxx_master", "builddatefull": 1_000_000},
            {"id": "unit", "site": "site", "buildname": "scream_unit_tests_release", "builddatefull": 1_000_000},
            {"id": "scripts", "site": "site", "buildname": "scripts_tests", "builddatefull": 1_000_000},
            {"id": "coverage", "site": "site", "buildname": "scream_unit_tests_coverage",
             "builddatefull": 1_000_000},
            {"id": "test-coverage", "site": "site", "buildname": "scream_unit_tests_test_coverage",
             "builddatefull": 1_000_000},
        ]
        tests = {build["id"]: [{"id": build["id"], "name": "eamxx", "status": "PASSED", "details": ""}]
                 for build in builds}
        index = {"date": "2026-11-04", "buildgroups": [{"name": "filtered", "builds": builds}],
                 "coverages": [{"buildid": "coverage"}]}
        fetched = []

        def paged(query, build_id, field, variables=None):
            fetched.append(build_id)
            return tests[build_id]

        with patch.object(REPORT, "GROUPS", {"filtered": "all"}), \
             patch.object(REPORT, "http_json", return_value=index), \
             patch.object(REPORT, "paged", side_effect=paged), \
             patch.object(REPORT, "triage_build_tests"):
            cime = REPORT.collect("2026-11-04", now=1_000_000, profile=REPORT.PROFILE_CIME)
            self.assertEqual(fetched, ["cime"])
            self.assertEqual(cime["coverage"], [])
            fetched.clear()
            unit = REPORT.collect("2026-11-04", now=1_000_000, profile=REPORT.PROFILE_UNIT)
            self.assertEqual(fetched, ["unit", "scripts"])
            self.assertEqual(unit["coverage"], [])
            fetched.clear()
            coverage = REPORT.collect("2026-11-04", now=1_000_000, profile=REPORT.PROFILE_COVERAGE)

        self.assertEqual(fetched, ["coverage", "test-coverage"])
        self.assertEqual(unit["profile"], REPORT.PROFILE_UNIT)
        self.assertEqual(coverage["profile"], REPORT.PROFILE_COVERAGE)
        self.assertEqual(coverage["coverage"], index["coverages"])

    def test_stale_coverage_suites_are_excluded_only_from_coverage_report(self):
        cutoff = 1_000_000 - REPORT.MAX_SUITE_AGE_SECONDS
        builds = [{"id": build_id, "site": "site", "buildname": name,
                   "builddatefull": cutoff - 1} for build_id, name in (
                       ("cime", "e3sm_eamxx_master"),
                       ("unit", "scream_unit_tests_release"),
                       ("coverage", "scream_unit_tests_coverage"))]
        index = {"date": "2026-11-04", "buildgroups": [{"name": "group", "builds": builds}]}
        with patch.object(REPORT, "GROUPS", {"group": "all"}), \
             patch.object(REPORT, "http_json", return_value=index), \
             patch.object(REPORT, "paged", return_value=[]):
            report = REPORT.collect("2026-11-04", now=1_000_000)

        group = report["groups"][0]
        self.assertEqual(group["excluded_old"], 3)
        for section in ("cime", "unit", "coverage"):
            self.assertEqual(group[f"excluded_old_{section}"], 1)
            self.assertIn("hours:* 1 (group: 1)", REPORT.format_excluded_old(report, section))


class CliTests(unittest.TestCase):
    def test_profile_defaults_to_all(self):
        args = REPORT.make_parser().parse_args([])
        self.assertEqual(args.profile, REPORT.PROFILE_ALL)
        self.assertEqual(REPORT.make_parser().parse_args(["--profile", "eamxx-coverage"]).profile,
                         REPORT.PROFILE_COVERAGE)


class SectionTests(unittest.TestCase):
    def test_standalone_suite_classification(self):
        self.assertTrue(REPORT.is_standalone_suite("scream_unit_tests_full_debug"))
        self.assertTrue(REPORT.is_standalone_suite("scripts_tests"))
        self.assertFalse(REPORT.is_standalone_suite("e3sm_eamxx_v1_master_gnugpu"))
        self.assertEqual(REPORT.suite_section("scream_unit_tests_coverage"), "coverage")
        self.assertEqual(REPORT.suite_section("scream_unit_tests_test_coverage"), "coverage")

    def test_report_separates_standalone_tests_and_coverage(self):
        rep = {"date": "2026-11-04", "coverage": [{
            "buildid": "coverage", "site": "site", "buildname": "unit coverage",
            "percentage": 80.0, "loctested": 8, "locuntested": 2,
        }], "groups": [{
            "name": "group", "mode": "all", "missing": False, "excluded_old": 0,
            "excluded_old_cime": 0, "excluded_old_unit": 0, "excluded_old_coverage": 0,
            "builds": [
                {"id": "cime", "site": "site", "name": "e3sm_master", "tests": [],
                 "counts": Counter({"PASS": 3}), "orphan_build_errors": 0, "commit": None},
                {"id": "unit", "site": "site", "name": "scream_unit_tests_release", "tests": [],
                 "counts": Counter({"PASS": 6000}), "orphan_build_errors": 0, "commit": None},
                {"id": "scripts", "site": "site", "name": "scripts_tests", "tests": [],
                 "counts": Counter({"PASS": 2}), "orphan_build_errors": 0, "commit": None},
                {"id": "coverage", "site": "site", "name": "scream_unit_tests_coverage", "tests": [],
                 "counts": Counter({"PASS": 5}), "orphan_build_errors": 0, "commit": None},
            ],
        }]}

        report = REPORT.format_report(rep)
        markdown = REPORT.format_markdown_report(rep)
        summary = REPORT.format_slack_summary(rep, None)
        thread = REPORT.format_thread(rep, with_file=False)

        self.assertLess(report.index("CIME-based tests"), report.index("Standalone tests"))
        self.assertIn("CIME-based tests* · 1 suites · 3 tests", report)
        self.assertIn("Standalone tests* · 2 suites · 6,002 tests", report)
        self.assertIn("Coverage tests* · 1 suites · 5 tests", report)
        self.assertIn("### CIME-based tests", markdown)
        self.assertIn("### Standalone tests", markdown)
        self.assertIn("### Coverage tests", markdown)
        self.assertIn("| group | 1 | 0 | 0 | 0 | 3 | 0 | 0 |", markdown)
        self.assertIn("| group | 2 | 0 | 0 | 0 | 6,002 | 0 | 0 |", markdown)
        self.assertIn("| group | 1 | 0 | 0 | 0 | 5 | 0 | 0 |", markdown)
        self.assertIn("80.0% (below target of 90%)", summary)
        self.assertIn("CIME-based tests", thread)
        self.assertIn("Standalone tests", thread)
        self.assertIn("Coverage tests", thread)

        cime_report = {**rep, "profile": REPORT.PROFILE_CIME}
        unit_report = {**rep, "profile": REPORT.PROFILE_UNIT}
        coverage_report = {**rep, "profile": REPORT.PROFILE_COVERAGE}
        cime_text = REPORT.format_report(cime_report)
        unit_text = REPORT.format_report(unit_report)
        coverage_text = REPORT.format_report(coverage_report)
        self.assertIn("CIME-based tests", cime_text)
        self.assertNotIn("Standalone tests", cime_text)
        self.assertNotIn("Coverage", cime_text)
        self.assertIn("Standalone tests", unit_text)
        self.assertNotIn("CIME-based tests", unit_text)
        self.assertNotIn("Coverage tests", unit_text)
        self.assertNotIn("scream_unit_tests_coverage", unit_text)
        self.assertIn("Coverage tests", coverage_text)
        self.assertNotIn("Standalone tests", coverage_text)
        self.assertNotIn("• *Coverage:*", REPORT.format_slack_summary(unit_report, None))
        self.assertIn("• *Coverage:*", REPORT.format_slack_summary(coverage_report, None))
        self.assertNotIn("### Coverage tests", REPORT.format_markdown_report(unit_report))
        self.assertIn("### Coverage tests", REPORT.format_markdown_report(coverage_report))
        empty_unit = {"date": "2026-11-04", "profile": REPORT.PROFILE_UNIT, "coverage": [],
                      "groups": [{"name": "group", "missing": False, "builds": []}]}
        self.assertIn("No suites matched this report profile", REPORT.format_report(empty_unit))
        self.assertNotIn(":white_check_mark:", report + markdown + summary + thread)
        self.assertNotIn("🎉", report + markdown + summary + thread)


class FormattingTests(unittest.TestCase):
    def test_suite_links_show_failed_test_list_only_when_tests_failed(self):
        filters = "%7B%22all%22%3A%5B%7B%22eq%22%3A%7B%22status%22%3A%22FAILED%22%7D%7D%5D%7D"
        builds = []
        for bucket in ("FAIL", "BUILDFAIL", "DIFF"):
            build = {"id": bucket, "site": "site", "name": f"eamxx_{bucket}", "commit": None,
                     "orphan_build_errors": 1 if bucket == "FAIL" else 0,
                     "counts": Counter({bucket: 1}), "tests": [{
                         "id": bucket, "name": f"test-{bucket}", "bucket": bucket,
                         "diff_diagnostics": [], "bless": None,
                     }]}
            builds.append(build)
            self.assertEqual(REPORT.blink(build),
                             f"<https://my.cdash.org/builds/{bucket}/tests?filters={filters}|"
                             f"site / eamxx_{bucket}>")
        orphan = {"id": "orphan", "site": "site", "name": "orphan-build", "commit": None,
                  "orphan_build_errors": 1, "counts": Counter({"PASS": 1}),
                  "tests": [{"bucket": "PASS"}]}
        self.assertEqual(REPORT.blink(orphan), "site / orphan-build")
        self.assertEqual(REPORT.blink({**orphan, "tests": [{"bucket": "NOTRUN"}]}),
                         "site / orphan-build")

        rep = {"date": "2026-11-20", "profile": REPORT.PROFILE_CIME, "coverage": [],
               "groups": [{"name": "group", "missing": False, "builds": [*builds, orphan]}]}
        full = REPORT.format_report(rep)
        thread = REPORT.format_thread(rep, with_file=False)
        markdown = REPORT.format_markdown_report(rep)
        summary = REPORT.format_slack_summary(rep, None)
        for bucket in ("FAIL", "BUILDFAIL", "DIFF"):
            url = f"https://my.cdash.org/builds/{bucket}/tests?filters={filters}"
            self.assertIn(f"<{url}|site / eamxx_{bucket}>", full)
            self.assertIn(f"<{url}|site / eamxx_{bucket}>", thread)
            self.assertIn(f"[site / eamxx_{bucket}]({url})", markdown)
        self.assertIn("*group* · site / orphan-build", full)
        self.assertIn("*group* · site / orphan-build", thread)
        self.assertIn("[build errors](https://my.cdash.org/builds/orphan/build)", markdown)
        self.assertIn("• site / orphan-build: 1 build error(s)", summary)
        self.assertIn(f"<https://my.cdash.org/builds/FAIL/tests?filters={filters}|"
                      "site / eamxx_FAIL>", summary)
        self.assertNotIn("/builds/orphan|", full + thread + summary)

    def test_merge_pr_lookup_parses_only_commit_headline_and_falls_back(self):
        with patch.dict(REPORT.os.environ, {"GITHUB_TOKEN": "test-token"}), \
             patch.object(REPORT.urllib.request, "urlopen") as urlopen:
            urlopen.return_value.__enter__.return_value = io.BytesIO(
                b'{"commit": {"message": "Merge pull request #1234 from branch\\n\\nDetails"}}')
            self.assertEqual(REPORT.lookup_merge_pr("5b1427fe20eb1234"), "1234")
            request = urlopen.call_args.args[0]
            self.assertEqual(request.full_url,
                             "https://api.github.com/repos/E3SM-Project/E3SM/commits/5b1427fe20eb1234")
            self.assertEqual(request.get_header("Authorization"), "Bearer test-token")
            self.assertEqual(urlopen.call_args.kwargs["timeout"], 10)

            urlopen.return_value.__enter__.return_value = io.BytesIO(
                b'{"commit": {"message": "Update model\\n\\nMerge pull request #1234"}}')
            self.assertIsNone(REPORT.lookup_merge_pr("5b1427fe20eb1234"))
            urlopen.side_effect = urllib.error.HTTPError(request.full_url, 403, "rate limited", {}, None)
            self.assertIsNone(REPORT.lookup_merge_pr("5b1427fe20eb1234"))
            urlopen.reset_mock()
            self.assertIsNone(REPORT.lookup_merge_pr("not-a-sha"))
            urlopen.assert_not_called()

    def test_pr_title_lookup_collapses_lines_and_falls_back_on_errors(self):
        with patch.dict(REPORT.os.environ, {"GITHUB_TOKEN": "test-token"}), \
             patch.object(REPORT.urllib.request, "urlopen") as urlopen:
            urlopen.return_value.__enter__.return_value = io.BytesIO(
                b'{"title": "Fix baseline tests  \\n for EAMxx"}')
            self.assertEqual(REPORT.lookup_pr_title("1234"), "Fix baseline tests for EAMxx")
            request = urlopen.call_args.args[0]
            self.assertEqual(request.full_url, "https://api.github.com/repos/E3SM-Project/E3SM/pulls/1234")
            self.assertEqual(request.get_header("Authorization"), "Bearer test-token")
            self.assertEqual(urlopen.call_args.kwargs["timeout"], 10)

            urlopen.side_effect = urllib.error.HTTPError(request.full_url, 404, "not found", {}, None)
            self.assertIsNone(REPORT.lookup_pr_title("1234"))
            urlopen.side_effect = None
            urlopen.return_value.__enter__.return_value = io.BytesIO(b'{"title": "  "}')
            self.assertIsNone(REPORT.lookup_pr_title("1234"))

    def test_slack_summary_aligns_counts_for_groups_and_totals(self):
        def build(name, counts, orphan_build_errors=0):
            return {"id": name, "site": "site", "name": name, "counts": Counter(counts),
                    "orphan_build_errors": orphan_build_errors}

        rep = {"date": "2026-11-04", "profile": REPORT.PROFILE_CIME, "coverage": [],
               "groups": [
                   {"name": "E3SM_EAMXX", "missing": False, "excluded_old": 8,
                    "builds": [build("cime-a", {"FAIL": 1, "PASS": 150, "NOTRUN": 8}),
                               build("cime-b", {})]},
                   {"name": "E3SM_MustPass_wBaselines", "missing": False,
                    "builds": [build("cime-c", {"DIFF": 4, "PASS": 7}, 1)]},
                   {"name": "E3SM_MustPass", "missing": True, "builds": []},
               ]}

        summary = REPORT.format_slack_summary(rep, "_Details in thread_")
        self.assertEqual(summary.count("```"), 2)
        self.assertNotIn(" · FAIL", summary)
        table = summary.split("```")[1].strip("\n").splitlines()
        self.assertEqual(len(table), 6)
        self.assertEqual(table[0].split(),
                         ["Group", "Suites", "FAIL", "BUILD", "FAIL", "DIFF", "PASS",
                          "Not", "run", "Build", "errors", "Attention"])
        self.assertEqual(table[2].split(), ["E3SM_EAMXX", "2", "1", "0", "0", "150", "8", "0", "1"])
        self.assertEqual(table[3].split(),
                         ["E3SM_MustPass_wBaselines", "1", "0", "0", "4", "7", "0", "1", "1"])
        self.assertEqual(table[4].split(), ["E3SM_MustPass", "missing", "—", "—", "—", "—", "—", "—", "—"])
        self.assertEqual(table[5].split(), ["Total", "3", "1", "0", "4", "157", "8", "1", "2"])
        for column in ("Suites", "FAIL", "BUILD FAIL", "DIFF", "PASS", "Not run", "Build errors", "Attention"):
            position = table[0].index(column) + len(column) - 1
            self.assertTrue(all(row[position] != " " for row in table[2:]), column)
        self.assertIn("Suites excluded as older than 18 hours:* 8", summary)
        self.assertTrue(summary.endswith("_Details in thread_"))

    def test_slack_summary_preserves_empty_profile_message(self):
        rep = {"date": "2026-11-04", "profile": REPORT.PROFILE_UNIT, "coverage": [],
               "groups": [{"name": "group", "missing": False, "builds": []}]}
        self.assertIn("No suites matched this report profile", REPORT.format_slack_summary(rep, None))

    def test_coverage_uses_text_for_threshold_status(self):
        coverage = [{"buildid": "1", "site": "site", "buildname": "unit coverage",
                     "percentage": 80.0, "loctested": 8, "locuntested": 2}]
        rendered = "\n".join(REPORT.format_coverage(coverage))
        self.assertIn("80.00%* (below target of 90%)", rendered)
        self.assertNotIn("🟢", rendered)
        self.assertEqual(REPORT.coverage_status({"percentage": 90, "thresholdgreen": 70}),
                         "meets target of 90%")
        self.assertEqual(REPORT.coverage_status({"percentage": 89.9}), "below target of 90%")
        self.assertEqual(REPORT.coverage_status({"percentage": 60}), "below target of 90%")
        self.assertEqual(REPORT.coverage_status({"percentage": 40}), "below minimum of 49% (target 90%)")

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
        self.assertIn("| Group | Suites | FAIL | BUILD FAIL | DIFF | PASS | Not run | Build errors |", markdown)
        self.assertIn("| group | 2 | 1 | 0 | 0 | 1 | 0 | 0 |", markdown)
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
              "diff_diagnostics": ["field lists differ (otherwise bit-for-bit)"]}] * 3
            + [{"bless": ("5b1427fe20eb1234", "2026-09-23_12:35:07"),
                "diff_diagnostics": ["field lists differ (otherwise bit-for-bit)"]}] * 3
            + [{"bless": ("5b1427fe20eb1234", "2026-09-24_00:01:02"),
                "diff_diagnostics": ["field lists differ (otherwise bit-for-bit)"]}]
            + [{"bless": None, "diff_diagnostics": ["baseline directory is missing"]}] * 16
            + [{"bless": ("ca0b1c1b1da1234", "2026-10-15_09:32:39"), "diff_diagnostics": []}] * 12
        )

        tallies = "\n".join(REPORT.format_diff_tallies(diffs))
        self.assertIn("7: sha <https://github.com/E3SM-Project/E3SM/commit/5b1427fe20eb1234|"
                      "`5b1427fe20eb`> · blessed 2026-09-23, 2026-09-24 · field lists differ "
                      "(otherwise bit-for-bit)", tallies)
        self.assertNotIn("12:34:51", tallies)
        self.assertNotIn("12:35:07", tallies)
        self.assertIn("16: _bless info not found in log_ · baseline directory is missing", tallies)
        self.assertIn("12: sha <https://github.com/E3SM-Project/E3SM/commit/ca0b1c1b1da1234|"
                      "`ca0b1c1b1da1`> · blessed 2026-10-15", tallies)
        markdown = REPORT.to_markdown(tallies, title=False)
        self.assertIn("[`5b1427fe20eb`](https://github.com/E3SM-Project/E3SM/commit/"
                      "5b1427fe20eb1234)", markdown)
        diffs[0]["baseline_pr"] = "1234"
        diffs[0]["baseline_pr_title"] = "Fix & clarify <baselines>"
        pr_tallies = "\n".join(REPORT.format_diff_tallies(diffs, slack=True))
        self.assertIn("Merge PR #1234", pr_tallies)
        self.assertIn("<https://github.com/E3SM-Project/E3SM/pull/1234|Merge PR #1234>", pr_tallies)
        self.assertIn("Merge PR #1234> · Fix &amp; clarify &lt;baselines&gt; · blessed", pr_tallies)
        diffs[0]["baseline_pr_title"] = None
        self.assertNotIn("None", "\n".join(REPORT.format_diff_tallies(diffs, slack=True)))
        self.assertIn("sha <https://github.com/E3SM-Project/E3SM/commit/ca0b1c1b1da1234|",
                      pr_tallies)
        self.assertEqual(REPORT.commit_link("unknown"), "`unknown`")


if __name__ == "__main__":
    unittest.main()
