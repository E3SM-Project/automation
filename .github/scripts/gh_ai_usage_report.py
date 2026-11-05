#!/usr/bin/env python3
"""GitHub AI-credit usage report for an organization, posted to Slack.

Reads the org's AI-credit usage, seat count and budget from the GitHub REST API,
checks that the numbers make sense, and posts a short Slack Block Kit message.

Environment variables:
  GH_TOKEN            token with read access to the org's billing (a GitHub App token)
  ORG_NAME            the organization
  SLACK_WEBHOOK_URL   Slack incoming webhook (only needed when actually posting)
  GITHUB_EVENT_NAME   when it is "pull_request" this is a dry run (see --dry-run)
  GITHUB_STEP_SUMMARY set by GitHub Actions; the dry-run preview is appended to it

--dry-run (also implied by running on a pull_request) does everything except post
to Slack: it prints the message and writes it to the job summary instead.

Standard library only, so the workflow has nothing to install.
"""
import argparse
import calendar
import json
import math
import os
import re
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone

API = "https://api.github.com"

# Included-credit pool = seat count * per-seat monthly credit allotment. GitHub
# doesn't expose the pool total, so it's derived here; update this if GitHub
# changes a plan's allotment or E3SM changes plans.
PER_SEAT_CREDITS = {
    "business": 1900,
    "enterprise": 3900,
}

AI_BUDGET_SKUS = ("ai_credits", "copilot")


class ReportError(Exception):
    """Something went wrong that should fail the run with a clear message."""


# ----------------------------------------------------------------------------
# HTTP
# ----------------------------------------------------------------------------

def parse_next_link(link_header):
    """URL of rel="next" in an HTTP Link header, or None."""
    m = re.search(r'<([^>]+)>\s*;\s*rel="next"', link_header or "")
    return m.group(1) if m else None


def fetch_json(url, headers, timeout=15):
    """GET a URL. Returns (parsed JSON, Link header). Raises on HTTP/network errors."""
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8")), resp.headers.get("Link")


def get_paginated(url, list_key, headers):
    """All the items under `list_key`, following Link: rel="next" pages."""
    items = []
    while url:
        try:
            body, link = fetch_json(url, headers)
        except (OSError, ValueError) as exc:  # URLError/HTTPError/timeouts are OSErrors
            raise ReportError(f"GET {url} failed: {exc}")
        items.extend(body.get(list_key, []))
        url = parse_next_link(link)
    return items


def post_slack(webhook_url, payload):
    req = urllib.request.Request(
        webhook_url, data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10):
            pass
    except OSError as exc:
        raise ReportError(f"posting to Slack failed: {exc}")


# ----------------------------------------------------------------------------
# Numbers
# ----------------------------------------------------------------------------

def as_number(name, value):
    """A finite float, or ReportError. Guards against the API changing a field's
    name or type, which would otherwise only show up as a garbled Slack message."""
    try:
        x = float(value)
    except (TypeError, ValueError):
        raise ReportError(f"{name} is not a valid number: {value!r}")
    if math.isnan(x) or math.isinf(x):
        raise ReportError(f"{name} is not a finite number: {value!r}")
    return x


def as_float(name, value):
    """Like as_number, and also not negative (for the totals)."""
    x = as_number(name, value)
    if x < 0:
        raise ReportError(f"{name} is negative, which should not happen: {x}")
    return x


def sum_usage(items, month_prefix, log=print):
    """(included dollars used, additional dollars used) for Copilot in this month.

    discountAmount is the $ covered by the included AI-credit pool, netAmount is
    what was actually billed as overage. "UserMonths" items are excluded from both:
    they are flat per-seat subscription fees, not AI-credit consumption, and the
    org's budget only tracks AI-credit spend, so seat fees must not count against
    it (including them once reported $51.48 spent while the budget page said $0).
    """
    included = additional = 0.0
    for item in items:
        if item.get("product") != "copilot":
            continue
        if not str(item.get("date", "")).startswith(month_prefix):
            continue
        log(f"item: sku={item.get('sku')!r} unitType={item.get('unitType')!r} "
            f"gross={item.get('grossAmount')} discount={item.get('discountAmount')} "
            f"net={item.get('netAmount')}")
        if item.get("unitType") == "UserMonths":
            continue
        additional += as_number("netAmount", item.get("netAmount", 0.0))
        included += as_number("discountAmount", item.get("discountAmount", 0.0))
    return included, additional


def included_credits_cap(org, seat_data):
    plan_type = seat_data.get("plan_type")
    seat_count = seat_data.get("seat_breakdown", {}).get("total")
    if plan_type not in PER_SEAT_CREDITS or seat_count is None:
        raise ReportError(f"Unrecognized Copilot plan_type={plan_type!r} or missing seat count "
                          f"for `{org}` - add the plan to PER_SEAT_CREDITS.")
    return seat_count * PER_SEAT_CREDITS[plan_type]


def find_budget_cap(budgets):
    """The org-level AI budget cap set in the GitHub UI, or None if there is none."""
    return next((b.get("budget_amount") for b in budgets
                 if b.get("budget_scope") == "organization"
                 and b.get("budget_product_sku") in AI_BUDGET_SKUS), None)


def fetch_usage(org, token, now, log=print):
    """Query the GitHub REST API. Returns a dict of the four validated numbers."""
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    usage_items = get_paginated(f"{API}/orgs/{org}/settings/billing/usage", "usageItems", headers)
    if not usage_items:
        raise ReportError(f"Billing usage API returned no usageItems for `{org}`. "
                          "Check the GitHub App's Administration (read) permission and installation scope.")
    included_dollars, additional_dollars = sum_usage(usage_items, now.strftime("%Y-%m"), log)

    try:
        seat_data, _ = fetch_json(f"{API}/orgs/{org}/copilot/billing", headers)
    except (OSError, ValueError) as exc:
        raise ReportError(f"Failed to fetch Copilot seat info for `{org}`: {exc}")

    budgets = get_paginated(f"{API}/orgs/{org}/settings/billing/budgets", "budgets", headers)
    budget_cap = find_budget_cap(budgets)
    if budget_cap is None:
        raise ReportError(f"No org-level AI budget found for `{org}` - was it renamed or removed in the GitHub UI?")

    data = {
        "included_credits_used": included_dollars * 100,  # 1 AI credit = $0.01
        "included_credits_cap": included_credits_cap(org, seat_data),
        "used_additional_dollars": additional_dollars,
        "additional_budget_cap": budget_cap,
    }
    return validate(data)


def validate(data):
    """Floats, finite, non-negative, and the caps non-zero."""
    values = {k: as_float(k, v) for k, v in data.items()}
    if values["included_credits_cap"] == 0 or values["additional_budget_cap"] == 0:
        raise ReportError(f"A cap came back as 0, which should not happen: {values}")
    return values


# ----------------------------------------------------------------------------
# Message
# ----------------------------------------------------------------------------

def build_report(org, data, now):
    """The message as a dict: status, the three text lines, and the Slack payload."""
    included_cap = data["included_credits_cap"]
    budget_cap = data["additional_budget_cap"]
    used_additional = data["used_additional_dollars"]

    base_used = min(data["included_credits_used"], included_cap)
    base_pct = base_used / included_cap * 100.0 if included_cap > 0 else 0.0
    additional_pct = used_additional / budget_cap * 100.0 if budget_cap > 0 else 0.0

    # Month-to-date pace. License credits burn first and additional credits
    # second, so the two pools are viewed as one combined allotment
    # (1 AI credit = $0.01) compared against the share of the month that has
    # elapsed (linear trajectory).
    days_in_month = calendar.monthrange(now.year, now.month)[1]
    elapsed_pct = now.day / days_in_month * 100.0
    total_allotment = included_cap + budget_cap * 100
    total_used = base_used + used_additional * 100
    total_pct = total_used / total_allotment * 100.0
    pace_delta = total_pct - elapsed_pct
    if abs(pace_delta) < 2.0:
        pace_verdict = "on pace"
    elif pace_delta > 0:
        pace_verdict = f"{pace_delta:.1f} pts AHEAD of pace"
    else:
        pace_verdict = f"{-pace_delta:.1f} pts behind pace"

    if used_additional > budget_cap:
        status_emoji = ":red_circle:"
        status_label = f"BUDGET EXCEEDED (Over by ${used_additional - budget_cap:,.2f})"
    elif additional_pct >= 85.0:
        status_emoji = ":warning:"
        status_label = "Warning - Additional budget over 85% depleted"
    else:
        status_emoji = ":large_green_circle:"
        status_label = "Normal"

    included_line = f"Included Credits: {base_used:,.0f} / {included_cap:,.0f} credits ({base_pct:.1f}%)"
    additional_line = f"Additional Budget: ${used_additional:,.2f} / ${budget_cap:,.2f} ({additional_pct:.1f}%)"
    pace_line = (
        f"Overall: {total_pct:.1f}% used vs {elapsed_pct:.1f}% of month "
        f"(day {now.day}/{days_in_month}) - {pace_verdict} "
        f"(${total_used / 100:,.0f} used vs ${total_allotment * elapsed_pct / 100 / 100:,.0f} expected)"
    )
    title = f"{org} GitHub AI usage report"
    payload = {
        "blocks": [
            {"type": "header", "text": {"type": "plain_text", "text": title}},
            {"type": "section", "text": {"type": "mrkdwn", "text": (
                f"{status_emoji} *Status:* {status_label}\n\n"
                f"* *{included_line}*\n"
                f"* *{additional_line}*\n"
                f"* *{pace_line}*")}},
            {"type": "context", "elements": [
                {"type": "mrkdwn", "text": "Automated daily report generated via GitHub Actions."}]},
        ]
    }
    return {"title": title, "status_emoji": status_emoji, "status_label": status_label,
            "lines": [included_line, additional_line, pace_line], "payload": payload}


def print_preview(report, summary_path):
    """Dry run: print the message and add it to the job summary, post nothing."""
    print(report["title"])
    print(f"{report['status_emoji']} Status: {report['status_label']}\n")
    for line in report["lines"]:
        print(line)
    if summary_path:
        with open(summary_path, "a") as f:
            f.write(f"## {report['title']} (dry run - not posted to Slack)\n\n")
            f.write(f"{report['status_emoji']} **Status:** {report['status_label']}\n\n")
            for line in report["lines"]:
                f.write(f"- **{line}**\n")


# ----------------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------------

def require_env(name):
    value = os.environ.get(name)
    if not value:
        raise ReportError(f"environment variable {name} is not set")
    return value


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--dry-run", action="store_true",
                    help="don't post to Slack; print the message and write it to the job summary")
    args = ap.parse_args(argv)
    dry_run = args.dry_run or os.environ.get("GITHUB_EVENT_NAME") == "pull_request"
    try:
        org = require_env("ORG_NAME")
        now = datetime.now(timezone.utc)
        data = fetch_usage(org, require_env("GH_TOKEN"), now)
        print(f"Raw totals: {data}")
        report = build_report(org, data, now)
        if dry_run:
            print("Dry run - not posting to Slack. Message that would be posted:\n")
            print_preview(report, os.environ.get("GITHUB_STEP_SUMMARY"))
        else:
            post_slack(require_env("SLACK_WEBHOOK_URL"), report["payload"])
            print("Successfully posted report to Slack!")
            for line in report["lines"]:
                print(line)
    except ReportError as exc:
        print(f"::error::{exc}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
