import contextlib
import importlib.util
import io
import os
import pathlib
import tempfile
import unittest
import urllib.error
from datetime import datetime, timezone
from unittest.mock import patch

SCRIPT = pathlib.Path(__file__).parents[1] / "gh_ai_usage_report.py"
SPEC = importlib.util.spec_from_file_location("gh_ai_usage_report", SCRIPT)
REPORT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(REPORT)

NOW = datetime(2026, 11, 10, 8, 0, tzinfo=timezone.utc)  # day 10 of 30
DATA = {
    "included_credits_used": 1000.0,
    "included_credits_cap": 3800.0,
    "used_additional_dollars": 10.0,
    "additional_budget_cap": 100.0,
}


def item(sku="copilot_ai_credit", unit="AiCredits", net=0.0, discount=0.0, product="copilot", date="2026-11-03"):
    return {"product": product, "sku": sku, "unitType": unit, "netAmount": net,
            "discountAmount": discount, "grossAmount": None, "date": date}


class LinkTests(unittest.TestCase):
    def test_next_link(self):
        h = '<https://api.github.com/x?page=2>; rel="next", <https://api.github.com/x?page=5>; rel="last"'
        self.assertEqual(REPORT.parse_next_link(h), "https://api.github.com/x?page=2")

    def test_no_next_link(self):
        self.assertIsNone(REPORT.parse_next_link('<https://api.github.com/x?page=1>; rel="prev"'))
        self.assertIsNone(REPORT.parse_next_link(None))


class PaginationTests(unittest.TestCase):
    def test_follows_pages(self):
        pages = {
            "u1": ({"items": [1, 2]}, '<u2>; rel="next"'),
            "u2": ({"items": [3]}, None),
        }
        with patch.object(REPORT, "fetch_json", side_effect=lambda url, headers: pages[url]):
            self.assertEqual(REPORT.get_paginated("u1", "items", {}), [1, 2, 3])

    def test_missing_key_is_empty(self):
        with patch.object(REPORT, "fetch_json", return_value=({}, None)):
            self.assertEqual(REPORT.get_paginated("u1", "items", {}), [])

    def test_http_error_becomes_report_error(self):
        err = urllib.error.HTTPError("u1", 403, "Forbidden", {}, None)
        with patch.object(REPORT, "fetch_json", side_effect=err):
            with self.assertRaisesRegex(REPORT.ReportError, "GET u1 failed"):
                REPORT.get_paginated("u1", "items", {})


class UsageTests(unittest.TestCase):
    def sum(self, items):
        return REPORT.sum_usage(items, "2026-11", log=lambda *_: None)

    def test_sums_discount_and_net(self):
        inc, add = self.sum([item(net=1.5, discount=2.0), item(net=0.5, discount=3.0)])
        self.assertEqual((inc, add), (5.0, 2.0))

    def test_ignores_other_products_and_months(self):
        inc, add = self.sum([item(product="actions", net=9, discount=9),
                             item(date="2026-10-31", net=9, discount=9),
                             item(net=1, discount=1)])
        self.assertEqual((inc, add), (1.0, 1.0))

    def test_ignores_seat_fees(self):
        # "UserMonths" are flat per-seat fees, not AI credit consumption
        inc, add = self.sum([item(unit="UserMonths", net=51.48, discount=0), item(net=2, discount=1)])
        self.assertEqual((inc, add), (1.0, 2.0))

    def test_negative_item_inside_positive_total_is_fine(self):
        inc, add = self.sum([item(net=5, discount=5), item(net=-1, discount=-1)])
        self.assertEqual((inc, add), (4.0, 4.0))

    def test_non_numeric_amount_is_an_error(self):
        with self.assertRaisesRegex(REPORT.ReportError, "netAmount is not a valid number"):
            self.sum([item(net="lots")])


class CapTests(unittest.TestCase):
    def test_included_cap_per_plan(self):
        self.assertEqual(REPORT.included_credits_cap("o", {"plan_type": "business", "seat_breakdown": {"total": 10}}), 19000)
        self.assertEqual(REPORT.included_credits_cap("o", {"plan_type": "enterprise", "seat_breakdown": {"total": 2}}), 7800)

    def test_unknown_plan_or_missing_seats(self):
        with self.assertRaisesRegex(REPORT.ReportError, "Unrecognized Copilot plan_type"):
            REPORT.included_credits_cap("o", {"plan_type": "free", "seat_breakdown": {"total": 1}})
        with self.assertRaisesRegex(REPORT.ReportError, "missing seat count"):
            REPORT.included_credits_cap("o", {"plan_type": "business"})

    def test_budget_cap(self):
        budgets = [
            {"budget_scope": "repository", "budget_product_sku": "copilot", "budget_amount": 1},
            {"budget_scope": "organization", "budget_product_sku": "actions", "budget_amount": 2},
            {"budget_scope": "organization", "budget_product_sku": "ai_credits", "budget_amount": 300},
        ]
        self.assertEqual(REPORT.find_budget_cap(budgets), 300)
        self.assertIsNone(REPORT.find_budget_cap(budgets[:2]))


class ValidateTests(unittest.TestCase):
    def test_ok(self):
        self.assertEqual(REPORT.validate({"included_credits_cap": "5", "additional_budget_cap": 2,
                                          "included_credits_used": 0, "used_additional_dollars": 0})["included_credits_cap"], 5.0)

    def test_rejects_bad_numbers(self):
        base = dict(DATA)
        for bad, msg in (("abc", "not a valid number"), (float("nan"), "not a finite"),
                         (float("inf"), "not a finite"), (-1, "negative")):
            with self.subTest(bad=bad):
                with self.assertRaisesRegex(REPORT.ReportError, msg):
                    REPORT.validate({**base, "used_additional_dollars": bad})

    def test_rejects_zero_caps(self):
        for key in ("included_credits_cap", "additional_budget_cap"):
            with self.subTest(key=key):
                with self.assertRaisesRegex(REPORT.ReportError, "cap came back as 0"):
                    REPORT.validate({**DATA, key: 0})


class MessageTests(unittest.TestCase):
    def build(self, **kw):
        return REPORT.build_report("E3SM-Project", {**DATA, **kw}, NOW)

    def test_normal(self):
        r = self.build()
        self.assertEqual(r["status_label"], "Normal")
        self.assertEqual(r["lines"][0], "Included Credits: 1,000 / 3,800 credits (26.3%)")
        self.assertEqual(r["lines"][1], "Additional Budget: $10.00 / $100.00 (10.0%)")
        self.assertEqual(r["title"], "E3SM-Project GitHub AI usage report")

    def test_included_credits_capped_at_pool_size(self):
        self.assertTrue(self.build(included_credits_used=99999.0)["lines"][0].startswith("Included Credits: 3,800 / 3,800"))

    def test_warning_at_85_percent(self):
        self.assertEqual(self.build(used_additional_dollars=85.0)["status_emoji"], ":warning:")
        self.assertEqual(self.build(used_additional_dollars=84.9)["status_emoji"], ":large_green_circle:")

    def test_budget_exceeded(self):
        r = self.build(used_additional_dollars=112.5)
        self.assertEqual(r["status_emoji"], ":red_circle:")
        self.assertIn("Over by $12.50", r["status_label"])

    def test_pace_verdicts(self):
        # total allotment = 3800 + 100*100 = 13800 credits; day 10/30 -> 33.3% elapsed
        on = self.build(included_credits_used=3800.0, used_additional_dollars=7.0)       # 4500/13800 = 32.6%
        ahead = self.build(included_credits_used=3800.0, used_additional_dollars=50.0)   # 8800/13800 = 63.8%
        behind = self.build(included_credits_used=0.0, used_additional_dollars=0.0)      # 0%
        self.assertIn("on pace", on["lines"][2])
        self.assertIn("pts AHEAD of pace", ahead["lines"][2])
        self.assertIn("pts behind pace", behind["lines"][2])
        self.assertIn("(day 10/30)", on["lines"][2])

    def test_payload_shape(self):
        blocks = self.build()["payload"]["blocks"]
        self.assertEqual([b["type"] for b in blocks], ["header", "section", "context"])
        self.assertIn("Status:", blocks[1]["text"]["text"])


class MainTests(unittest.TestCase):
    """The whole flow, with the HTTP layer replaced by canned API responses."""

    ENV = {"ORG_NAME": "O", "GH_TOKEN": "t", "SLACK_WEBHOOK_URL": "https://hooks.example/x"}

    @staticmethod
    def fake_fetch(url, headers, timeout=15):
        month = datetime.now(timezone.utc).strftime("%Y-%m")
        if url.endswith("/settings/billing/usage"):
            return {"usageItems": [item(net=2, discount=10, date=month + "-01")]}, None
        if url.endswith("/copilot/billing"):
            return {"plan_type": "business", "seat_breakdown": {"total": 2}}, None
        if url.endswith("/settings/billing/budgets"):
            return {"budgets": [{"budget_scope": "organization", "budget_product_sku": "ai_credits",
                                 "budget_amount": 50}]}, None
        raise AssertionError(url)

    def run_main(self, argv=(), env=None, fetch=None):
        out = io.StringIO()
        with patch.dict(os.environ, {**self.ENV, **(env or {})}, clear=True), \
                patch.object(REPORT, "fetch_json", side_effect=fetch or self.fake_fetch), \
                patch.object(REPORT, "post_slack") as post, \
                contextlib.redirect_stdout(out):
            rc = REPORT.main(list(argv))
        return rc, out.getvalue(), post

    def test_posts_to_slack(self):
        rc, out, post = self.run_main()
        self.assertEqual(rc, 0)
        post.assert_called_once()
        webhook, payload = post.call_args.args
        self.assertEqual(webhook, "https://hooks.example/x")
        self.assertEqual(payload["blocks"][0]["text"]["text"], "O GitHub AI usage report")
        self.assertIn("Included Credits: 1,000 / 3,800", out)

    def test_dry_run_flag_does_not_post(self):
        with tempfile.NamedTemporaryFile("r", suffix=".md") as summary:
            rc, out, post = self.run_main(["--dry-run"], env={"GITHUB_STEP_SUMMARY": summary.name})
            written = summary.read()
        self.assertEqual(rc, 0)
        post.assert_not_called()
        self.assertIn("not posting to Slack", out)
        self.assertIn("dry run", written)
        self.assertIn("Included Credits", written)

    def test_pull_request_is_a_dry_run(self):
        rc, _, post = self.run_main(env={"GITHUB_EVENT_NAME": "pull_request"})
        self.assertEqual(rc, 0)
        post.assert_not_called()

    def test_dry_run_needs_no_webhook(self):
        rc, _, post = self.run_main(["--dry-run"], env={"SLACK_WEBHOOK_URL": ""})
        self.assertEqual(rc, 0)

    def test_posting_needs_a_webhook(self):
        rc, out, post = self.run_main(env={"SLACK_WEBHOOK_URL": ""})
        self.assertEqual(rc, 1)
        self.assertIn("::error::environment variable SLACK_WEBHOOK_URL is not set", out)

    def test_api_failure_fails_with_error_annotation(self):
        def boom(url, headers, timeout=15):
            raise urllib.error.HTTPError(url, 500, "Server Error", {}, None)
        rc, out, post = self.run_main(fetch=boom)
        self.assertEqual(rc, 1)
        self.assertIn("::error::GET ", out)
        post.assert_not_called()

    def test_empty_usage_is_an_error(self):
        def empty(url, headers, timeout=15):
            return {"usageItems": []}, None
        rc, out, _ = self.run_main(fetch=empty)
        self.assertEqual(rc, 1)
        self.assertIn("returned no usageItems", out)


if __name__ == "__main__":
    unittest.main()
