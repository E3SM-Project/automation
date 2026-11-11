#!/usr/bin/env python3
"""
Daily E3SM CDash report for EAMxx-related tests, posted to Slack.

Data sources (all public, no login needed):
  * https://my.cdash.org/api/v1/index.php?project=E3SM[&date=YYYY-MM-DD]
      -> build groups, builds (test suites), and coverage summaries
  * https://my.cdash.org/graphql
      -> per-test status/details, build errors, and test logs

Classification of each test:
  PASS        status PASSED (CDash also counts NLFAIL as passed)
  DIFF        details == "Completed (DIFF)"   (CDash stores these as FAILED)
  BUILD FAIL  a FAILED test that CDash lists among the build's build errors,
              or whose log shows a pre-run phase failing (CREATE_NEWCASE, XML,
              SETUP, SHAREDLIB_BUILD, MODEL_BUILD)
  FAIL        any other FAILED test (run-time failure)
  NOT RUN     status NOT_RUN (e.g. PEND); counted but not detailed

Likely cause (FAIL and BUILD FAIL only): the suite's uploaded log tarball is
streamed without extraction or execution and matched against known failure
signatures: setup and module errors, full disks, compile errors, killed
compilers, model aborts (using the message printed just before MPI_Abort),
crashes, time limits, memory kills, comparison and memory-leak failures. The
log lines behind each cause go in the saved report and GitHub job summary.

Usage:
  Run .github/workflows/cdash-eamxx-report.yaml on its CIME/unit weekday
  schedules, or use workflow_dispatch to choose a profile and testing date.
  The command-line profile defaults to "all" to preserve the combined report.

Output, in this order, so a Slack problem never loses the report:
  1. cdash_report/cdash_eamxx_report_<date>.md   combined report (profile=all)
     cdash_report/cdash_eamxx_report_<profile>_<date>.md   selected profile
     cdash_report/slack_message*.txt             exact Slack text, for pasting by hand
  2. the GitHub Actions job summary (when run in Actions) and stdout
  3. Slack, if configured. If posting fails (revoked token, bot not in the
     channel, missing permission, network), the error and what it usually means
     are logged, and the script exits with status 2 after everything above is saved.

Slack settings (environment variables):
  SLACK_BOT_TOKEN + SLACK_CHANNEL_ID   preferred. Posts a few-line summary to the
      channel, then every suite and the failure details as replies in its thread,
      plus the full .md report as a file in the thread. The Slack app needs the
      chat:write and files:write scopes and must be invited to the channel.
  SLACK_DETAILS   what goes in the thread: both (default), thread, or file.
  SLACK_WEBHOOK_URL   fallback. Webhooks can't thread or attach files, so this
      posts the summary with a link to the GitHub run (or, run outside GitHub
      Actions, the whole report).
"""

import argparse
from datetime import datetime, timezone
import gzip
import json
import os
import re
import socket
import ssl
import sys
import tarfile
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter, deque

CDASH = "https://my.cdash.org"
PROJECT = "E3SM"
GITHUB_REPO = "https://github.com/E3SM-Project/E3SM"  # for linking commit shas

# ---------------------------------------------------------------------------
# Configuration: which build groups to report and how to filter their tests.
#   "all"   -> every test in every build of the group
#   "eamxx" -> only tests whose name contains "eamxx" (case-insensitive);
#              builds with no matching tests are skipped
# ---------------------------------------------------------------------------
GROUPS = {
    "E3SM_EAMXX": "all",
    "E3SM_MustPass_wBaselines": "eamxx",
    "E3SM_MustPass": "eamxx",
}
NAME_FILTER = "eamxx"
PROFILE_ALL = "all"
PROFILE_CIME = "eamxx-cime"
PROFILE_UNIT = "eamxx-unit"
PROFILES = (PROFILE_ALL, PROFILE_CIME, PROFILE_UNIT)
REPORT_SECTIONS = (("CIME-based tests", False), ("Standalone tests", True))
PROFILE_LABELS = {
    PROFILE_ALL: "combined",
    PROFILE_CIME: "CIME",
    PROFILE_UNIT: "unit",
}
MAX_SUITE_AGE_HOURS = 18
MAX_SUITE_AGE_SECONDS = MAX_SUITE_AGE_HOURS * 60 * 60

BLESS_RE = re.compile(r"Most recent bless:\s*sha:(\S+)\s+date:(\S+)")
FIELDLIST_RE = re.compile(r"master:\s*FIELDLIST field lists differ \(otherwise bit-for-bit\)")
BFAIL_BASELINE_RE = re.compile(r"master:\s*ERROR BFAIL baseline directory [^\r\n]* does not exist")
DIFF_DIAGNOSTICS = {
    "fieldlist": "field lists differ (otherwise bit-for-bit)",
    "missing_baseline": "baseline directory is missing",
}
PHASE_RE = re.compile(r"(\w+) FAILED for test")
NOTES_COMMIT_RE = re.compile(r"^Commit\s+([0-9a-f]{7,40})\b", re.M)  # in the suite's notes.txt
LOG_VERSION_RE = re.compile(r"e3sm model version found:\s*([0-9a-f]{7,40})\b")  # CIME test logs
BUILD_PHASES = {"CREATE_NEWCASE", "XML", "SETUP", "SHAREDLIB_BUILD", "MODEL_BUILD"}
SLACK_CHUNK = 3500  # keep each Slack message comfortably under display limits

BUCKETS = ["PASS", "FAIL", "BUILDFAIL", "DIFF", "NOTRUN"]
LABEL = {"PASS": "PASS", "FAIL": "FAIL", "BUILDFAIL": "BUILD FAIL", "DIFF": "DIFF",
         "NOTRUN": "not run"}
# --------------------------------------------------------------------------- CDash
def http_json(url, payload=None, retries=3):
    data = json.dumps(payload).encode() if payload is not None else None
    headers = {"Content-Type": "application/json", "User-Agent": "e3sm-cdash-eamxx-bot"}
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, data=data, headers=headers)
            with urllib.request.urlopen(req, timeout=180) as r:
                return json.load(r)
        except Exception as e:  # network hiccups are common on long runs
            # Retrying can't fix a bad certificate or a host name that won't resolve.
            permanent = isinstance(getattr(e, "reason", None),
                                   (ssl.SSLCertVerificationError, socket.gaierror))
            if permanent or attempt == retries - 1:
                raise
            print(f"retrying {url}: {e}", file=sys.stderr)
            time.sleep(5 * (attempt + 1))


def gql(query, variables=None):
    d = http_json(f"{CDASH}/graphql", {"query": query, "variables": variables or {}})
    if d.get("errors"):
        raise RuntimeError(f"GraphQL error: {d['errors']}")
    return d["data"]


TESTS_Q = """query($id: ID!, $after: String) {
  build(id: $id) { tests(first: 500, after: $after) {
    pageInfo { hasNextPage endCursor }
    edges { node { id name status details } } } } }"""

# Failed-test output is fetched only for tests included in the report.
FAILED_LOGS_Q = """query($id: ID!, $after: String,
                           $filters: BuildTestsFiltersMultiFilterInput!) {
  build(id: $id) { tests(first: 50, after: $after, filters: $filters) {
    pageInfo { hasNextPage endCursor }
    edges { node { id output } } } } }"""

NOTES_Q = """query($id: ID!) { build(id: $id) { notes { edges { node { name text } } } } }"""

BUILD_ERRORS_Q = """query($id: ID!, $after: String) {
  build(id: $id) { buildErrors(first: 200, after: $after) {
    pageInfo { hasNextPage endCursor }
    edges { node { stdOutput stdError } } } } }"""


def paged(query, build_id, field, variables=None):
    out, after = [], None
    variables = variables or {}
    while True:
        t = gql(query, {"id": str(build_id), "after": after, **variables})["build"][field]
        out += [e["node"] for e in t["edges"]]
        if not t["pageInfo"]["hasNextPage"]:
            return out
        after = t["pageInfo"]["endCursor"]


def classify(t, build_error_text):
    if t["status"] == "PASSED":
        return "PASS"
    if t["status"] == "NOT_RUN":
        return "NOTRUN"
    if "(DIFF)" in (t["details"] or ""):
        return "DIFF"
    if t["name"] in build_error_text or t.get("phase") in BUILD_PHASES:
        return "BUILDFAIL"
    return "FAIL"


# --------------------------------------------------------------------------- collect
def collect(date, now=None, profile=PROFILE_ALL):
    if profile not in PROFILES:
        raise ValueError(f"unknown report profile: {profile}")
    url = f"{CDASH}/api/v1/index.php?project={PROJECT}" + (f"&date={date}" if date else "")
    index = http_json(url)
    report = {"date": index.get("date"), "profile": profile, "groups": [],
              "coverage": collect_coverage(index) if profile != PROFILE_CIME else []}
    cutoff = (time.time() if now is None else now) - MAX_SUITE_AGE_SECONDS
    by_name = {g["name"]: g for g in index.get("buildgroups", [])}

    for gname, mode in GROUPS.items():
        g = by_name.get(gname)
        group = {"name": gname, "mode": mode, "builds": [], "missing": g is None,
                 "excluded_old": 0, "excluded_old_cime": 0, "excluded_old_standalone": 0}
        for b in (g or {}).get("builds", []):
            if not suite_matches_profile(b["buildname"], profile):
                continue
            tests = paged(TESTS_Q, b["id"], "tests")
            if mode == NAME_FILTER:
                tests = [t for t in tests if NAME_FILTER in t["name"].lower()]
                if not tests:
                    continue

            try:
                started_at = float(b.get("builddatefull"))
            except (TypeError, ValueError):
                started_at = None  # keep suites CDash did not timestamp
            if started_at is not None and started_at < cutoff:
                group["excluded_old"] += 1
                key = "excluded_old_standalone" if is_standalone_suite(b["buildname"]) else "excluded_old_cime"
                group[key] += 1
                continue

            # Build errors: CDash puts the failing test's name in stdError/stdOutput.
            build_errors = []
            if (b.get("compilation") or {}).get("error", 0) > 0 or \
               (b.get("configure") or {}).get("error", 0) > 0:
                build_errors = paged(BUILD_ERRORS_Q, b["id"], "buildErrors")
            be_text = "\n".join((e["stdError"] or "") + "\n" + (e["stdOutput"] or "")
                                for e in build_errors)

            if any(t["status"] == "FAILED" for t in tests):
                failed_filters = {"eq": {"status": "FAILED"}}
                if mode == NAME_FILTER:
                    failed_filters = {"all": [
                        failed_filters,
                        {"contains": {"name": NAME_FILTER}},
                    ]}
                logs = {n["id"]: n["output"] or "" for n in paged(
                    FAILED_LOGS_Q, b["id"], "tests", {"filters": failed_filters})}
                for t in tests:
                    if t["status"] != "FAILED":
                        continue
                    log = logs.get(t["id"], "")
                    t["output"] = log
                    m = PHASE_RE.search(log)
                    t["phase"] = m.group(1) if m else None
                    m = BLESS_RE.search(log)
                    t["bless"] = (m.group(1), m.group(2)) if m else None
                    t["diff_diagnostics"] = diff_diagnostics(log)
                    t["baseline_exception"] = "Exception during BASELINE" in log
                    m = LOG_VERSION_RE.search(log)
                    t["model_version"] = m.group(1) if m else None

            for t in tests:
                t["bucket"] = classify(t, be_text)
            triage_build_tests(b["id"], tests)

            # Build errors not tied to any test (e.g. a unit-test build that failed to
            # compile). Only meaningful when reporting the whole build.
            names = {t["name"] for t in tests}
            orphan = 0
            if mode == "all":
                orphan = sum(1 for e in build_errors
                             if not any(n in (e["stdError"] or "") + (e["stdOutput"] or "")
                                        for n in names))

            counts = Counter(t["bucket"] for t in tests)
            has_problems = counts["FAIL"] or counts["BUILDFAIL"] or counts["DIFF"] or orphan
            group["builds"].append({
                "id": b["id"], "site": b["site"], "name": b["buildname"], "tests": tests,
                "counts": counts, "orphan_build_errors": orphan,
                "started_at": started_at,
                # Only suites that appear in the details section need their commit.
                "commit": find_commit(b["id"], tests) if has_problems else None,
            })
            print(f"  {gname} / {b['site']} / {b['buildname']}: {len(tests)} tests", file=sys.stderr)
        report["groups"].append(group)
    return report


def diff_diagnostics(log):
    """Return concise, actionable DIFF details found in CDash test output."""
    diagnostics = []
    if FIELDLIST_RE.search(log):
        diagnostics.append(DIFF_DIAGNOSTICS["fieldlist"])
    if BFAIL_BASELINE_RE.search(log):
        diagnostics.append(DIFF_DIAGNOSTICS["missing_baseline"])
    return diagnostics


def find_commit(build_id, tests):
    """The E3SM commit a suite ran, as (sha, source). Jenkins-run suites record it
    in notes.txt ("Commit <sha>"); otherwise fall back to the short sha CIME prints
    in each test log ("e3sm model version found: <sha>"). Returns None if CDash
    has neither, as for the GitHub-CI unit-test builds."""
    notes = gql(NOTES_Q, {"id": str(build_id)})["build"]["notes"]["edges"]
    for n in notes:
        m = NOTES_COMMIT_RE.search(n["node"]["text"] or "")
        if m:
            return m.group(1), "notes"
    versions = Counter(t["model_version"] for t in tests if t.get("model_version"))
    if versions:
        return versions.most_common(1)[0][0], "test log"
    return None


def collect_coverage(index):
    seen, out = set(), []
    for c in index.get("coverages", []):
        if c["buildid"] in seen:  # the dashboard lists some coverage builds twice
            continue
        seen.add(c["buildid"])
        out.append(c)
    green, yellow = index.get("thresholdgreen", 70), index.get("thresholdyellow", 49)
    for c in out:
        c["thresholdgreen"] = green
        c["thresholdyellow"] = yellow
    return out


# --------------------------------------------------------------------------- triage
# "Likely cause" for FAIL and BUILD FAIL tests. Rule-based: it reads the test's
# CDash output plus the suite's uploaded log tarball (streamed without writing
# to disk or executing it) and reports the first known failure signature,
# with the log lines that support it. It is a starting point for a human, not a
# verdict.

MAX_TARBALL_BYTES = 200 * 1024 * 1024  # skip unusually large uploads
TARBALL_IDLE_TIMEOUT_SECONDS = 15  # one stalled network read
TARBALL_TOTAL_SECONDS = 60  # one tarball, including connection and archive iteration
LOG_HEAD, LOG_TAIL = 4 * 1024 * 1024, 16 * 1024 * 1024  # per-log read caps
EVIDENCE_LINES = 12

RANK_PREFIX_RE = re.compile(r"^\s*\d+:\s*")  # "12: " MPI-rank prefix on log lines
LID_RE = re.compile(r"(\d{6}-\d{6})(?:\.gz)?$")  # CIME log timestamp suffix
TAR_TEST_RE = re.compile(r"/([^/]+?)_(CASEDIR|RUNDIR|EXEROOT)_logs/(.+)$")

# Lines that look alarming but are routine in E3SM logs.
NOISE_RE = re.compile(r"ERROR: (RUN|BUILD) FAIL|MPICH_\w+\s*=|PIO: WARNING|ABORT_ON_ERROR|_ERRORS\s*=|"
                      r"error_?check|errorcode\s*=\s*0|ERROR_LOG|no errors|"
                      r"Warning|WARNING|0 errors", re.I)

COMPILE_ERR_RE = re.compile(
    r"(catastrophic error: .*|error #\d+: .*|\berror: .*|Error: .*|"
    r"undefined reference to .*|fatal error: .*|CMake Error.*)")


def read_capped(fileobj, gz):
    """Read a log, keeping its first LOG_HEAD and last LOG_TAIL bytes."""
    stream = gzip.GzipFile(fileobj=fileobj) if gz else fileobj
    head = stream.read(LOG_HEAD)
    tail, tail_size = deque(), 0
    while True:
        chunk = stream.read(1024 * 1024)
        if not chunk:
            break
        tail.append(chunk)
        tail_size += len(chunk)
        while tail_size > LOG_TAIL:
            excess = tail_size - LOG_TAIL
            first = tail.popleft()
            if len(first) > excess:
                tail.appendleft(first[excess:])
                tail_size -= excess
            else:
                tail_size -= len(first)
    if tail:
        head += b"\n[... middle of log skipped ...]\n" + b"".join(tail)
    return head.decode("utf-8", errors="replace")


class TimedTarballReader:
    """Bound both elapsed time and downloaded bytes while streaming a tarball."""
    def __init__(self, response, deadline):
        self.response = response
        self.deadline = deadline
        self.bytes_read = 0

    def read(self, size):
        if time.monotonic() >= self.deadline:
            raise TimeoutError(f"log tarball exceeded the {TARBALL_TOTAL_SECONDS}-second read limit")
        # read1 returns after one socket receive. A full read(size) can keep
        # waiting indefinitely if a server sends a slow trickle of bytes.
        chunk = self.response.read1(size)
        self.bytes_read += len(chunk)
        if self.bytes_read > MAX_TARBALL_BYTES:
            raise ValueError("log tarball exceeds the download size limit")
        if time.monotonic() >= self.deadline:
            raise TimeoutError(f"log tarball exceeded the {TARBALL_TOTAL_SECONDS}-second read limit")
        return chunk


def build_logs(build_id, wanted_tests):
    """Download a suite's log tarball(s) and return {test: {kind: [(file, text)]}}
    for the given test names. Returns {} if the suite uploaded no logs."""
    files = gql("""query($id: ID!) { build(id: $id) { files { edges { node {
                   id name size } } } } }""", {"id": str(build_id)})["build"]["files"]["edges"]
    out = {}
    for f in (e["node"] for e in files):
        if not f["name"].endswith((".tar.gz", ".tgz")) or int(f["size"] or 0) > MAX_TARBALL_BYTES:
            continue
        req = urllib.request.Request(f"{CDASH}/builds/{build_id}/files/{f['id']}",
                                     headers={"User-Agent": "e3sm-cdash-eamxx-bot"})
        deadline = time.monotonic() + TARBALL_TOTAL_SECONDS
        try:
            with urllib.request.urlopen(req, timeout=TARBALL_IDLE_TIMEOUT_SECONDS) as r:
                with tarfile.open(fileobj=TimedTarballReader(r, deadline), mode="r|gz") as tar:
                    for m in tar:
                        mt = TAR_TEST_RE.search("/" + m.name)
                        if not m.isfile() or not mt or mt.group(1) not in wanted_tests:
                            continue
                        test, kind, rel = mt.groups()
                        base = rel.split("/")[-1]
                        if kind == "RUNDIR" and not re.match(r"(e3sm|atm|cpl)\.log\.", base):
                            continue
                        if kind == "EXEROOT" and ".bldlog." not in base:
                            continue
                        if kind == "CASEDIR" and base != "TestStatus.log":
                            continue
                        text = read_capped(tar.extractfile(m), base.endswith(".gz"))
                        out.setdefault(test, {}).setdefault(kind, []).append((base, text))
        except TimeoutError as e:
            print(f"  skipped slow log tarball for build {build_id}: {e}", file=sys.stderr)
    def newest_first(ft):
        m = LID_RE.search(ft[0])
        return m.group(1) if m else ""
    for kinds in out.values():
        for k in kinds:
            kinds[k].sort(key=newest_first, reverse=True)
    return out


def find(text, pattern, flags=0):
    """First match of pattern in text as (line_no, line), or None."""
    rx = re.compile(pattern, flags)
    for i, line in enumerate(text.splitlines()):
        if rx.search(line):
            return i, line
    return None


def excerpt(fname, text, line_no, before=3, after=EVIDENCE_LINES - 4):
    lines = text.splitlines()
    lo, hi = max(0, line_no - before), min(len(lines), line_no + after)
    return {"file": fname, "start": lo + 1, "lines": [l[:300] for l in lines[lo:hi]]}


def tail_excerpt(fname, text, n=EVIDENCE_LINES):
    lines = [l for l in text.splitlines() if l.strip()]
    return {"file": fname, "start": None, "lines": [l[:300] for l in lines[-n:]]}


def short(s, n=160):
    s = re.sub(r"\s+", " ", s).strip()
    return s if len(s) <= n else s[:n - 1] + "…"


def warning_flood(text, threshold=100):
    """The most-repeated warning in a run log, if it repeats suspiciously often."""
    c, example = Counter(), {}
    for line in text.splitlines():
        if re.search(r"warn", line, re.I) and not re.search(r"PIO: WARNING|MPICH_", line):
            clean = RANK_PREFIX_RE.sub("", line).strip()
            key = re.sub(r"(?<![A-Za-z_])[-+]?\d[\d.eE+-]*", "#", clean)
            c[key] += 1
            example.setdefault(key, clean)
    if c:
        key, n = c.most_common(1)[0]
        if n >= threshold:
            return n, example[key]
    return None


def run_progress(logs):
    """How far the model got, from atm.log (EAMxx) or cpl.log."""
    for fname, text in logs.get("atm", []):
        steps = re.findall(r"Atmosphere step = (\d+)", text)
        stamps = re.findall(r"model end-of-step timestamp: (\S+ \S+)", text)
        if steps:
            return f"reached atmosphere step {steps[-1]}" + (f" (model time {stamps[-1]})" if stamps else "")
    for fname, text in logs.get("cpl", []):
        dates = re.findall(r"model date\s*=\s*(\d+)", text)
        if dates:
            return f"reached model date {dates[-1]}"
    return None


RUN_RULES = [
    # (pattern, cause, category)
    (r"DUE TO TIME LIMIT|TIME LIMIT|walltime .* exceeded|PBS: job killed: walltime",
     "Job hit its wall-clock time limit", "timeout"),
    (r"oom[-_ ]kill|Out Of Memory|OUT_OF_MEMORY|out of memory|cudaErrorMemoryAllocation|"
     r"std::bad_alloc|Kokkos.*(allocation|allocate).*fail", "Ran out of memory", "memory"),
    (r"NODE FAILURE|NODE_FAIL|DUE TO PREEMPTION|node failure",
     "Node failure or preemption on the machine (likely not a code problem)", "system"),
    (r"Floating point exception|SIGFPE|floating (invalid|divide|overflow)",
     "Floating-point exception (FPE)", "crash"),
    (r"Segmentation fault|SIGSEGV|signal 11\b|forrtl: severe \(174\)", "Segmentation fault", "crash"),
    (r"OFI poll failed|MPIDI_OFI_\w+ error|PTLTE_NOT_FOUND|Transport retry count exceeded|"
     r"Fatal error in PMPI|ucx.*error|cxi.*error", "MPI/network communication error (often a machine problem)", "crash"),
    (r"exited on signal 9 \(Killed\)|signal 9\b.*Killed|\bKilled\b$",
     "A process was killed (signal 9), often by the system for using too much memory", "crash"),
    (r"died from signal \d+|exited on signal \d+|Bus error|SIGBUS|SIGABRT|core dumped",
     "A model process crashed", "crash"),
    (r"Job killed due to receiving signal \d+ \(SIGTERM\)|received signal 15|SIGTERM",
     "Job was terminated externally (SIGTERM)", "system"),
    (r"forrtl: severe|Fortran runtime error", "Fortran runtime error", "crash"),
    (r"EKAT_REQUIRE|EKAT_ERROR|Kokkos::abort|Error! |FAIL: |shr_sys_abort|ENDRUN|endrun|"
     r"\bERROR[:!]|\bError: |MPI_Abort|MPI_ABORT", "Model aborted", "abort"),
]


ABORT_SKIP_RE = re.compile(
    r"warn|^Exiting|MPICH|aborting job|Backtrace|^[-=*#\s]+$|Unknown\s+Unknown|"
    r"^\S+\s+[0-9A-Fa-f]{12,}\s|^#\d+\s+0x|^Image\s+PC|^at \S+:\d+$|"
    r"Unknown error submitted to shr_abort|MPI_ABORT was invoked|NOTE: invoking MPI_ABORT", re.I)
ABORT_MSG_RE = re.compile(r"error|abort|fail|\bnan\b|negative|exceed|not found|invalid|"
                          r"out of (range|bounds)|cannot|could not|unable|illegal|"
                          r"inconsistent|mismatch|too (large|small)|= 0\b", re.I)


def abort_message(text):
    """When the model calls MPI_Abort it usually prints why just before. Return
    (line_no, message) for the aborting rank's last error-like line, or None."""
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if not re.search(r"application called MPI_Abort|^\s*\d+:.*MPI_Abort", line):
            continue
        m = re.match(r"^\s*(\d+):", line) or re.search(r"process (\d+)\b", line)
        rank = m.group(1) if m else None
        for j in range(i - 1, max(-1, i - 400), -1):
            prev = lines[j]
            if rank and not re.match(rf"^\s*{rank}:", prev):
                continue
            body = RANK_PREFIX_RE.sub("", prev).strip()
            if len(body) < 8 or ABORT_SKIP_RE.search(body):
                continue
            if ABORT_MSG_RE.search(body):
                return j, body
        return None  # aborted, but without a message we can trust
    return None


def triage_run(test, logs):
    """Explain a RUN failure from e3sm.log / atm.log / cpl.log."""
    sources = [(f, t) for k in ("e3sm", "atm", "cpl") for f, t in logs.get(k, [])[:1]]
    if not sources and test.get("output"):
        sources = [("CDash test output", test["output"])]
    for fname, text in sources[:1]:  # e3sm.log has every rank's output
        hit = abort_message(text)
        if hit:
            cause = "Model aborted: " + short(hit[1], 120).rstrip(".!") 
            flood = warning_flood(text)
            if flood:
                cause += (f". Preceded by {flood[0]:,} repeated warnings like "
                          f"'{short(flood[1], 70)}'")
            return cause, [excerpt(fname, text, hit[0], 4, 6)]
    for pattern, cause, cat in RUN_RULES:
        for fname, text in sources:
            hit = find(text, pattern)
            if not hit:
                continue
            if cat == "abort":  # skip noise lines and look for a real message
                for i, line in enumerate(text.splitlines()):
                    if re.search(pattern, line) and not NOISE_RE.search(line):
                        hit = (i, line)
                        break
                else:
                    continue
            detail = ""
            if cat == "timeout":
                prog = run_progress(logs)
                detail = f"; {prog}" if prog else ""
            elif cat in ("crash", "abort") and not cause.startswith("A process was killed"):
                line = hit[1]
                mm = re.search(pattern, line)
                msg = line[mm.start():] if mm and cat == "crash" else RANK_PREFIX_RE.sub("", line)
                if len(re.sub(r"\W", "", msg)) < 12:  # bare "ENDRUN:" etc.; the text follows
                    nxt = [l for l in text.splitlines()[hit[0] + 1:hit[0] + 4] if l.strip()]
                    msg += " " + RANK_PREFIX_RE.sub("", nxt[0]) if nxt else ""
                detail = ": " + short(msg, 120).rstrip(".")
            if fname == "CDash test output":
                detail += " (no run logs uploaded; from CDash output)"
            flood = warning_flood(sources[0][1])
            if flood and cat in ("timeout", "crash", "abort", "memory"):
                detail += (f". Preceded by {flood[0]:,} repeated warnings like "
                           f"'{short(flood[1], 70)}', which may point to the underlying problem")
            if fname == "CDash test output" and cat == "abort":
                cause = "Test reported an error"
            return cause + detail, [excerpt(fname, text, hit[0])]
    if sources and sources[0][0] == "CDash test output":
        return ("Run failed; no run logs were uploaded to CDash, so the cause couldn't be checked",
                [tail_excerpt(*sources[0])])
    if sources:
        return ("Run failed with no recognized error message; see the end of the run log",
                [tail_excerpt(*sources[0])])
    return "Run failed; no run logs were uploaded to CDash", []


def triage_build(logs, output):
    """Explain a BUILD failure from the bldlogs (e3sm.bldlog, then shared libs)."""
    blds = sorted(logs.get("EXEROOT", []), key=lambda ft: not ft[0].startswith("e3sm."))
    for fname, text in blds:
        hit = find(text, r"Disk is full|No space left on device|Disk quota exceeded")
        if hit:
            return "Disk full during build", [excerpt(fname, text, hit[0])]
    for fname, text in blds:
        hit = find(text, r"terminated by kill signal|Killed signal terminated program|"
                         r"internal compiler error: Killed|virtual memory exhausted|cannot allocate memory")
        if hit:
            return ("The compiler was killed during the build, usually because the build node ran "
                    "out of memory (not a code error)", [excerpt(fname, text, hit[0])])
    for fname, text in blds:
        hit = find(text, COMPILE_ERR_RE.pattern)
        if hit:
            m = COMPILE_ERR_RE.search(hit[1])
            what = "CMake configuration error" if "CMake Error" in hit[1] else "Compile error"
            comp = fname.split(".bldlog")[0]
            return (f"{what} in the {comp} build: {short(m.group(1), 130)}",
                    [excerpt(fname, text, hit[0])])
    for fname, text in blds:
        hit = find(text, r"\*\*\* .*Error \d+|ninja: build stopped")
        if hit:
            return f"Build failed in {fname.split('.bldlog')[0]}; see build log", [excerpt(fname, text, hit[0])]
    if "cmake config" in output:
        return "CMake configuration of the model failed (no build log uploaded)", []
    return "Build failed; no build log was uploaded to CDash", []


COMPARE_KINDS = {"ERS": "Restarted run doesn't match the continuous run",
                 "ERR": "Restarted run doesn't match the continuous run",
                 "ERI": "Branch/hybrid restart doesn't match",
                 "ERP": "Results change with threads/PE layout",
                 "PET": "Results change with OpenMP threading",
                 "PEM": "Results change with MPI task count",
                 "PFS": "Comparison failed",
                 "RCS": "Ensemble members don't reproduce"}


def triage_test(test, logs):
    """Return (likely_cause, evidence) for one FAIL or BUILD FAIL test."""
    out = test.get("output") or ""
    status = logs.get("CASEDIR", [])
    text = status[0][1] if status else out  # TestStatus.log, else the same via CDash
    src = "TestStatus.log" if status else "CDash test output"

    # Which failure ended the test: the last ERROR line or phase exception.
    terminal = None
    for i, line in enumerate(text.splitlines()):
        if re.search(r"ERROR: |failed in phase '\w+' with exception", line):
            terminal = (i, line)

    # 1. A CIME phase raised a Python exception.
    m = re.search(r"failed in phase '(\w+)' with exception '([^'\n]*)'", text)
    if m:
        phase, exc = m.groups()
        hit = find(text, re.escape(m.group(0)[:60]))
        ev = [excerpt(src, text, hit[0], 0)] if hit else []
        if re.search(r"No space left|Disk quota", exc):
            return f"Disk full during {phase}", ev
        if re.search(r"transport endpoint|Errno 108|Stale file handle|Input/output error", exc):
            return f"File-system error during {phase} (storage connection lost)", ev
        return f"{phase} raised an exception: {short(exc, 120)}", ev
    m = re.search(r"^(\w+Error): (.*(?:transport endpoint|Stale file handle|No space left).*)$", text, re.M)
    if m:
        hit = find(text, re.escape(m.group(0)[:60]))
        return (f"File-system error ({m.group(1)}): {short(m.group(2), 100)}",
                [excerpt(src, text, hit[0], 2, 2)] if hit else [])

    # 2. Environment problems during setup.
    m = re.search(r"module command (.*?) failed with message:\s*\n(.*)", text)
    if m:
        hit = find(text, r"module command .* failed with message")
        return (f"Module system error during setup: {short(m.group(2), 110)}",
                [excerpt(src, text, hit[0], 0, 4)] if hit else [])
    missing = r"Missing source for symlink|Required path missing|Could not find all inputdata"
    if terminal and re.search(missing, terminal[1]):
        return (f"Missing input data: {short(terminal[1].split('ERROR: ', 1)[-1], 130)}",
                [excerpt(src, text, terminal[0], 1, 3)])

    # 3. Build and run failures, using the uploaded logs.
    if terminal and re.search(r"BUILD FAIL|SHAREDLIB_BUILD|MODEL_BUILD|FAILED, cat ", terminal[1]):
        return triage_build(logs, terminal[1])
    if test.get("phase") in ("SHAREDLIB_BUILD", "MODEL_BUILD"):
        return triage_build(logs, out)
    if terminal and re.search(r"RUN FAIL|Job killed due to receiving signal", terminal[1]):
        cause, ev = triage_run(test, logs)
        if "receiving signal" in terminal[1] and cause.startswith(("Run failed", "Job was terminated")):
            cpl = logs.get("cpl", [])
            if cpl and all("SUCCESSFUL TERMINATION" in t for _, t in cpl):
                n = len(cpl)
                cause = (f"The model {'runs' if n > 1 else 'run'} finished normally, but the test job was "
                         "then killed (SIGTERM) during CIME's post-run steps, possibly by the batch "
                         "time limit")
                ev = [excerpt(f, t, find(t, "SUCCESSFUL TERMINATION")[0], 0, 1) for f, t in cpl]
            else:
                cause = "The test job was killed (SIGTERM) before finishing; the run logs don't say why"
        nl = find(text, r"^(?:[\d-]+ [\d:]+: )?\s*NLCOMP\s*$|NLCOMP FAIL")
        return cause + ("; namelists also differ from the baseline" if nl else ""), ev

    # 4. Run completed, but a post-run check failed.
    m = re.search(r"memleak detected, memory went from ([\d.]+) to ([\d.]+) in (.*)", text)
    if m:
        hit = find(text, "memleak detected")
        return (f"Memory leak check failed: memory grew from {float(m.group(1)):.0f} to "
                f"{float(m.group(2)):.0f} MB over {m.group(3).strip()}", [excerpt(src, text, hit[0], 0, 1)])
    if "seem to be DIFFERENT" in text:
        kind = COMPARE_KINDS.get(test["name"].split(".")[0].split("_")[0], "Run-to-run comparison failed")
        m = re.search(r"of which\s+(\d+) had non-zero differences", text)
        hit = find(text, r"had non-zero differences|seem to be DIFFERENT")
        return (kind + (f" ({m.group(1)} fields differ)" if m else ""),
                [excerpt(src, text, hit[0], 2, 3)] if hit else [])
    hit = find(text, r"^(?:[\d-]+ [\d:]+: )?\s*NLCOMP\s*$|NLCOMP FAIL")
    if hit:
        return "Namelists differ from the baseline (NLCOMP)", [excerpt(src, text, hit[0], 2, 4)]

    # 5. Anything else that left an ERROR line.
    if terminal:
        return f"{short(terminal[1].split('ERROR: ', 1)[-1], 150)}", [excerpt(src, text, terminal[0], 2, 4)]

    # 6. Non-CIME tests (e.g. EAMxx unit tests run by ctest).
    hit = find(out, r"Assertion|FAILED|\berror\b|Error|Exception|terminate called", re.I)
    if hit and not NOISE_RE.search(hit[1]):
        return f"Test reported: {short(hit[1], 140)}", [excerpt("CDash test output", out, hit[0])]
    if out.strip():
        return "No recognized error message; see the end of the test output", [tail_excerpt("CDash test output", out)]
    return "No test output in CDash to examine", []


def earlier_errors(test, logs):
    """A specific ERROR logged before the one that ended the test, if any. These
    are often the real root cause (e.g. a missing file before a crash)."""
    status = logs.get("CASEDIR", [])
    text = status[0][1] if status else (test.get("output") or "")
    errs = [l.split("ERROR: ", 1)[-1] for l in text.splitlines() if "ERROR: " in l]
    for e in errs[:-1]:  # the last one is what triage already explained
        if not re.match(r"Batched build failed|(RUN|BUILD) FAIL|Job killed due to receiving signal", e):
            return short(e, 110)
    return None


def triage_build_tests(build_id, tests):
    """Add likely_cause/evidence to each FAIL and BUILD FAIL test of one suite.
    Problems here never break the report; they just limit what triage can say."""
    targets = [t for t in tests if t["bucket"] in ("FAIL", "BUILDFAIL")]
    if not targets:
        return
    try:
        logs = build_logs(build_id, {t["name"] for t in targets})
    except Exception as e:
        print(f"  could not read log tarball for build {build_id}: {e}", file=sys.stderr)
        logs = {}
    for t in targets:
        tl = logs.get(t["name"], {})
        # Group run logs by component for the run triage.
        for kind in ("e3sm", "atm", "cpl"):
            tl[kind] = [ft for ft in tl.get("RUNDIR", []) if ft[0].startswith(kind + ".log")]
        try:
            t["likely_cause"], t["evidence"] = triage_test(t, tl)
            first = earlier_errors(t, tl)
            if first and first not in t["likely_cause"]:
                t["likely_cause"] += f" (earlier error: {first})"
        except Exception as e:
            t["likely_cause"], t["evidence"] = f"(triage error: {e})", []


# --------------------------------------------------------------------------- format
def slack_escape(value):
    """Escape text interpolated into Slack mrkdwn (but not Slack link syntax)."""
    return str(value).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def display(value, slack):
    return slack_escape(value) if slack else str(value)


def blink(b, slack=False):
    return (f"<{CDASH}/builds/{b['id']}|{display(b['site'], slack)} / "
            f"{display(b['name'], slack)}>")


def tlink(t, slack=False):
    return f"<{CDASH}/tests/{t['id']}|`{display(t['name'], slack)}`>"


def counts_str(c, show_zero=("PASS", "FAIL", "BUILDFAIL", "DIFF")):
    summary_order = ("FAIL", "BUILDFAIL", "DIFF", "PASS", "NOTRUN")
    parts = [f"{LABEL[k]} {c[k]:,}" for k in summary_order if c[k] or k in show_zero]
    return " · ".join(parts)


def format_failures(ts, slack=False):
    """FAIL/BUILD FAIL tests with their likely cause, stated once when shared."""
    def test_line(t):
        return (f"      • {tlink(t, slack)}" +
                (f" (failed at {display(t['phase'], slack)})" if t.get("phase") else ""))
    causes = {t.get("likely_cause") for t in ts}
    if len(ts) > 1 and len(causes) == 1 and None not in causes:
        return ([f"      _All {len(ts)} have the same likely cause:_ {display(causes.pop(), slack)}"]
                + [test_line(t) for t in ts])
    out = []
    for t in ts:
        out.append(test_line(t))
        if t.get("likely_cause"):
            out.append(f"          ↳ _Likely cause:_ {display(t['likely_cause'], slack)}")
    return out


def diff_signature(test):
    """Group blessed DIFFs by SHA, ignoring sequential bless timestamps."""
    if test.get("bless"):
        baseline = ("bless", test["bless"][0])
    elif test.get("baseline_exception"):
        baseline = ("exception",)
    else:
        baseline = ("unknown",)
    return baseline, tuple(test.get("diff_diagnostics", []))


def format_diff_tallies(diffs, slack=False):
    """Group DIFFs by baseline SHA and diagnostics; display distinct bless days."""
    tallies, first_seen, bless_days = Counter(), {}, {}
    for index, test in enumerate(diffs):
        signature = diff_signature(test)
        tallies[signature] += 1
        first_seen.setdefault(signature, index)
        if test.get("bless"):
            match = re.match(r"\d{4}-\d{2}-\d{2}", str(test["bless"][1]))
            bless_days.setdefault(signature, set()).add(match.group() if match else None)

    lines = []
    for signature, count in sorted(tallies.items(), key=lambda item: (-item[1], first_seen[item[0]])):
        baseline, diagnostics = signature
        if baseline[0] == "bless":
            _, sha = baseline
            info = f"sha `{sha[:12]}`"
            days = bless_days[signature]
            if days - {None}:
                info += f" · blessed {', '.join(sorted(days - {None}))}"
            if None in days:
                info += " · bless date unavailable"
        elif baseline[0] == "exception":
            info = "_baseline comparison raised an exception; no bless info in log_"
        else:
            info = "_bless info not found in log_"
        info += "".join(f" · {display(diagnostic, slack)}" for diagnostic in diagnostics)
        prefix = f"All {count}" if len(tallies) == 1 and count > 1 else str(count)
        lines.append(f"      • {prefix}: {info}")
    return lines


FENCE = "`" * 3


def format_evidence(rep):
    """Markdown section with the log lines behind each likely cause. Goes in the
    saved report and the GitHub job summary, not in Slack."""
    parts, total = [], 0
    for g in rep["groups"]:
        for b in g["builds"]:
            for t in b["tests"]:
                if not t.get("evidence"):
                    continue
                body = []
                for e in t["evidence"]:
                    where = e["file"] + (f", from line {e['start']}" if e.get("start") else ", last lines")
                    lines = "\n".join(e["lines"]).replace(FENCE, "'''")
                    body.append(f"_{where}_\n{FENCE}\n{lines}\n{FENCE}")
                block = (f"<details><summary>{b['site']} / {b['name']}: {t['name']}</summary>\n\n"
                         f"Likely cause: {t['likely_cause']}\n\n" + "\n\n".join(body) + "\n\n</details>\n")
                total += len(block)
                if total > 600_000:  # stay well under GitHub's 1 MB job-summary limit
                    parts.append("_More evidence omitted to keep this report a manageable size._\n")
                    break
                parts.append(block)
    if not parts:
        return ""
    return ("\n## Log evidence for likely causes\n\nLikely causes come from matching known failure "
            "patterns in the logs CDash has. They are a starting point, not a diagnosis.\n\n"
            + "\n".join(parts))


def evidence_link():
    """Where Slack readers can find the log excerpts."""
    url = run_url()
    if url:
        return f"_Log excerpts behind each likely cause: <{url}|this GitHub run>_"
    return "_Log excerpts behind each likely cause are in the saved report._"


def format_coverage(cov, slack=False):
    lines = ["*Coverage* (unit tests)"]
    if not cov:
        return lines + ["    _no coverage results today_"]
    for c in cov:
        change = ""
        if "percentagediff" in c:
            d = c["percentagediff"]
            change = f" ({'+' if d >= 0 else ''}{d:.2f}% vs previous)"
        total = c["loctested"] + c["locuntested"]
        status = coverage_status(c)
        lines.append(f"    • <{CDASH}/builds/{c['buildid']}/coverage|{display(c['site'], slack)} / "
                     f"{display(c['buildname'], slack)}>: "
                     f"*{c['percentage']:.2f}%* ({status}){change} · "
                     f"{c['loctested']:,} of {total:,} lines covered")
    return lines


def coverage_status(coverage):
    """Describe CDash coverage thresholds without relying on color or icons."""
    percentage = coverage["percentage"]
    green = coverage.get("thresholdgreen", 70)
    yellow = coverage.get("thresholdyellow", 49)
    if percentage >= green:
        return f"meets target of {green:g}%"
    if percentage >= yellow:
        return f"warning band above {yellow:g}%"
    return f"below minimum of {yellow:g}%"


def is_standalone_suite(name):
    """Whether a CDash suite contains standalone/unit tests rather than CIME tests."""
    name = name.lower()
    return "unit_tests" in name or "scripts_tests" in name


def suite_matches_profile(name, profile):
    """Whether a build suite belongs in the requested report profile."""
    if profile == PROFILE_ALL:
        return True
    return is_standalone_suite(name) == (profile == PROFILE_UNIT)


def section_groups(rep, standalone):
    """Yield (group, builds) for one report section."""
    for group in rep["groups"]:
        builds = [b for b in group["builds"] if is_standalone_suite(b["name"]) == standalone]
        if builds or (group["missing"] and not standalone):
            yield group, builds


def section_totals(rep, standalone):
    total = Counter()
    nsuites = 0
    for _, builds in section_groups(rep, standalone):
        for build in builds:
            total += build["counts"]
            nsuites += 1
    return total, nsuites


def report_sections(rep):
    """Sections rendered for a combined or profile-specific report."""
    profile = rep.get("profile", PROFILE_ALL)
    if profile == PROFILE_ALL:
        return REPORT_SECTIONS
    if profile == PROFILE_CIME:
        return (REPORT_SECTIONS[0],)
    if profile == PROFILE_UNIT:
        return (REPORT_SECTIONS[1],)
    raise ValueError(f"unknown report profile: {profile}")


def report_totals(rep):
    total = Counter()
    for g in rep["groups"]:
        for b in g["builds"]:
            total += b["counts"]
    return total, sum(len(g["builds"]) for g in rep["groups"])


def has_problems(b):
    c = b["counts"]
    return bool(c["FAIL"] or c["BUILDFAIL"] or c["DIFF"] or b["orphan_build_errors"])


def suite_started_at(b):
    """Format a CDash build-start epoch unambiguously for the Markdown report."""
    if b.get("started_at") is None:
        return None
    return datetime.fromtimestamp(b["started_at"], timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def format_header(rep, slack=False):
    total, nsuites = report_totals(rep)
    profile = rep.get("profile", PROFILE_ALL)
    title = "E3SM CDash EAMxx report" if profile == PROFILE_ALL else (
        f"E3SM CDash EAMxx {PROFILE_LABELS[profile]} report")
    return [f"*{title}: testing day {display(rep['date'], slack)}*",
            f"<{CDASH}/index.php?project={PROJECT}&date={rep['date']}|Dashboard> · "
             f"{nsuites} suites · {sum(total.values()):,} tests · {counts_str(total)}"]


def format_section_header(rep, standalone, slack=False):
    total, nsuites = section_totals(rep, standalone)
    label = "Standalone tests" if standalone else "CIME-based tests"
    return [f"*{label}* · {nsuites} suites · {sum(total.values()):,} tests · "
            f"{counts_str(total)}"]


def format_group_summary(rep, standalone, slack=False):
    """One aggregate line per test group, without listing individual suites."""
    lines = []
    groups = list(section_groups(rep, standalone))
    if not any(builds for _, builds in groups):
        if not any(g["missing"] for g, _ in groups):
            return ["• _No suites matched this report profile._"]
    for g, builds in groups:
        if g["missing"]:
            lines.append(f"• *{display(g['name'], slack)}*: _group not present on the dashboard today_")
            continue
        counts = sum((b["counts"] for b in builds), Counter())
        nbuilds = len(builds)
        problems = sum(has_problems(b) for b in builds)
        line = (f"• *{display(g['name'], slack)}* ({nbuilds} suite{'s' if nbuilds != 1 else ''}): "
                f"{counts_str(counts)}")
        if problems:
            line += f" · _{problems} suite{'s' if problems != 1 else ''} need{'s' if problems == 1 else ''} attention_"
        lines.append(line)
    return lines


def format_excluded_old(rep, standalone=None):
    """Briefly report suites skipped because their CDash build start is stale."""
    if standalone is None:
        key = "excluded_old"
    else:
        key = "excluded_old_standalone" if standalone else "excluded_old_cime"
    def count(group):
        # The fallback keeps formatting fixtures and reports created before the
        # section split compatible with the new renderer.
        return group.get(key, group.get("excluded_old", 0) if standalone is False else 0)

    total = sum(count(g) for g in rep["groups"])
    by_group = " · ".join(f"{g['name']}: {count(g)}" for g in rep["groups"] if count(g))
    detail = by_group or "none"
    return (f"*Suites excluded as older than {MAX_SUITE_AGE_HOURS} hours:* "
            f"{total} ({detail})")


def format_details(rep, evidence_note=None, slack=False, standalone=None):
    """FAILs, BUILD FAILs and DIFFs, suite by suite."""
    lines = []
    if standalone is None:
        groups = [(g, g["builds"]) for g in rep["groups"]]
    else:
        groups = list(section_groups(rep, standalone))
    problems = [(g, b) for g, builds in groups for b in builds if has_problems(b)]
    lines.append("*Details: FAILs, BUILD FAILs and DIFFs*" if problems
                 else "*No FAILs, BUILD FAILs or DIFFs*")
    for g, b in problems:
        lines.append("")
        lines.append(f"*{display(g['name'], slack)}* · {blink(b, slack)}")
        if not slack and (started_at := suite_started_at(b)):
            lines.append(f"  Started: {started_at}")
        if b.get("commit"):
            sha, source = b["commit"]
            lines.append(f"  Commit tested: <{GITHUB_REPO}/commit/{sha}|`{sha[:12]}`>"
                         + (" _(from test logs)_" if source == "test log" else ""))
        else:
            lines.append("  Commit tested: _not recorded in CDash for this suite_")
        for bucket in ("BUILDFAIL", "FAIL"):
            ts = [t for t in b["tests"] if t["bucket"] == bucket]
            if ts:
                lines.append(f"  *{LABEL[bucket]} ({len(ts)})*")
                lines += format_failures(ts, slack)
        if b["orphan_build_errors"]:
            lines.append(f"  *{b['orphan_build_errors']} build errors not tied to a test*: "
                         f"see <{CDASH}/builds/{b['id']}/build|build errors>")
        diffs = [t for t in b["tests"] if t["bucket"] == "DIFF"]
        if diffs:
            lines.append(f"  *DIFF ({len(diffs)})*")
            lines += format_diff_tallies(diffs, slack)
    if any(t.get("likely_cause") for _, builds in groups for b in builds for t in b["tests"]):
        lines += ["", evidence_note or evidence_link()]
    return lines


def format_report(rep, slack=False):
    """Compact report for all tests or one selected profile."""
    lines = format_header(rep, slack) + [""]
    for label, standalone in report_sections(rep):
        lines += format_section_header(rep, standalone, slack)
        lines += format_group_summary(rep, standalone, slack)
        lines += [format_excluded_old(rep, standalone)]
        if standalone:
            lines += format_coverage(rep["coverage"], slack)
        lines += [""] + format_details(rep, slack=slack, standalone=standalone) + [""]
    return "\n".join(lines).rstrip()


def format_markdown_report(rep):
    """Markdown report with tables for all tests or one selected profile."""
    total, nsuites = report_totals(rep)
    lines = [
        f"## {format_header(rep)[0].strip('*')}",
        f"[Dashboard]({CDASH}/index.php?project={PROJECT}&date={rep['date']}) · "
        f"{nsuites} suites · {sum(total.values()):,} tests",
        "",
    ]
    for label, standalone in report_sections(rep):
        total, nsuites = section_totals(rep, standalone)
        lines += [f"### {label}", "", "| Group | Suites | FAIL | BUILD FAIL | DIFF | PASS | Not run |",
                  "|---|---:|---:|---:|---:|---:|---:|"]
        for g, builds in section_groups(rep, standalone):
            if g["missing"]:
                lines.append(f"| {g['name']} | — | — | — | — | — | — |")
                continue
            counts = sum((b["counts"] for b in builds), Counter())
            lines.append(f"| {g['name']} | {len(builds)} | {counts['FAIL']:,} | "
                         f"{counts['BUILDFAIL']:,} | {counts['DIFF']:,} | {counts['PASS']:,} | "
                         f"{counts['NOTRUN']:,} |")
        lines.append(f"| **Total** | **{nsuites}** | **{total['FAIL']:,}** | "
                     f"**{total['BUILDFAIL']:,}** | **{total['DIFF']:,}** | **{total['PASS']:,}** | "
                     f"**{total['NOTRUN']:,}** |")
        lines += ["", format_excluded_old(rep, standalone)]
        if standalone:
            lines += [""] + to_markdown("\n".join(format_coverage(rep["coverage"], slack=False)), title=False).splitlines()
        lines += [""] + to_markdown("\n".join(format_details(rep, standalone=standalone)), title=False).splitlines()
        lines.append("")
    return "\n".join(lines) + "\n"


def format_slack_summary(rep, details_where):
    """A few lines for the channel: totals per category, coverage, and where the
    details are. details_where is a short mrkdwn phrase, or None."""
    lines = format_header(rep, slack=True)
    for label, standalone in report_sections(rep):
        lines += format_section_header(rep, standalone, slack=True)
        lines += format_group_summary(rep, standalone, slack=True)
        lines.append(format_excluded_old(rep, standalone))
        if standalone and rep["coverage"]:
            lines.append("• *Coverage:* " + " · ".join(
                f"{slack_escape(c['site'])} {c['percentage']:.1f}% ({coverage_status(c)})"
                for c in rep["coverage"]))
    if details_where:
        lines.append(details_where)
    return "\n".join(lines)


def format_thread(rep, with_file):
    """What goes in the thread under the summary: only problem details."""
    note = ("_Log excerpts behind each likely cause are in the report file below._" if with_file
            else None)
    lines = []
    for label, standalone in report_sections(rep):
        lines += format_section_header(rep, standalone, slack=True)
        lines += format_details(rep, note, slack=True, standalone=standalone)
        lines.append("")
    return "\n".join(lines).rstrip()



# --------------------------------------------------------------------------- deliver
def chunks(text, size=SLACK_CHUNK):
    buf = ""
    for line in text.split("\n"):
        if len(buf) + len(line) + 1 > size and buf:
            yield buf
            buf = ""
        buf += line + "\n"
    if buf.strip():
        yield buf


# What Slack's webhook error strings usually mean, so the log says what to fix.
SLACK_HINTS = {
    "invalid_token": "the webhook URL is invalid or was revoked; create a new one and update the "
                     "SLACK_WEBHOOK_URL secret",
    "no_service": "the webhook was removed or its Slack app was uninstalled; create a new webhook",
    "no_team": "the Slack workspace for this webhook no longer exists or the app was removed",
    "team_disabled": "the Slack workspace has been disabled",
    "action_prohibited": "a Slack admin has restricted this webhook from posting",
    "posting_to_general_channel_denied": "only admins may post to this channel; pick another channel",
    "channel_not_found": "the channel was deleted or can't be seen. With a bot token, check that "
                         "SLACK_CHANNEL_ID is the channel's ID (like C0123ABCD), not its name, and "
                         "for a private channel that the bot has been invited",
    "channel_is_archived": "the channel is archived; unarchive it or use another channel",
    "is_archived": "the channel is archived; unarchive it or use another channel",
    "invalid_payload": "Slack rejected the message format (please report this as a bot bug)",
    # Web API (bot token) errors
    "not_in_channel": "the bot isn't a member of the channel; in Slack, type /invite @<bot name> "
                      "in that channel",
    "invalid_auth": "the bot token is invalid; copy the Bot User OAuth Token (xoxb-...) into "
                    "the SLACK_BOT_TOKEN secret again",
    "not_authed": "no bot token was sent; check the SLACK_BOT_TOKEN secret",
    "token_revoked": "the bot token was revoked; reinstall the Slack app and update SLACK_BOT_TOKEN",
    "token_expired": "the bot token expired; reinstall the Slack app and update SLACK_BOT_TOKEN",
    "account_inactive": "the Slack app was uninstalled or deactivated; reinstall it",
    "restricted_action": "a workspace setting or admin is blocking the bot from posting here",
    "msg_too_long": "a message was too long for Slack (please report this as a bot bug)",
}


class SlackPostError(Exception):
    pass


def posted_note(i):
    """Say how much of the report reached Slack before message i failed."""
    if i == 1:
        return ". Nothing was posted."
    return ". Message 1 was posted." if i == 2 else f". Messages 1-{i - 1} were posted."


def post_webhook(webhook, text):
    """Post via an incoming webhook, in chunks. Returns the number of messages."""
    parts = list(chunks(text))
    for i, part in enumerate(parts, 1):
        body = json.dumps({"text": part, "unfurl_links": False}).encode()
        for attempt in range(4):
            try:
                req = urllib.request.Request(webhook, body, {"Content-Type": "application/json"})
                urllib.request.urlopen(req, timeout=60).read()
                break
            except urllib.error.HTTPError as e:
                detail = e.read().decode(errors="replace").strip() or e.reason
                if e.code == 429 and attempt < 3:  # rate limited: wait and retry
                    time.sleep(int(e.headers.get("Retry-After", 5)))
                    continue
                hint = SLACK_HINTS.get(detail, "")
                raise SlackPostError(f"Slack returned HTTP {e.code} ({detail}) on message {i} of "
                                     f"{len(parts)}" + (f": {hint}" if hint else "") + posted_note(i))
            except (urllib.error.URLError, TimeoutError, OSError) as e:
                if attempt < 3:
                    time.sleep(5 * (attempt + 1))
                    continue
                raise SlackPostError(f"could not reach Slack on message {i} of {len(parts)}: {e}"
                                     + posted_note(i))
        time.sleep(1)  # Slack webhooks allow ~1 message/second
    return len(parts)


SLACK_API = os.environ.get("SLACK_API_BASE", "https://slack.com/api")


def slack_api(token, method, params, form=False):
    """Call a Slack Web API method with the bot token. Slack reports most errors
    as {"ok": false, "error": ...} with HTTP 200; those become SlackPostError."""
    if form:
        body = urllib.parse.urlencode(params).encode()
        ctype = "application/x-www-form-urlencoded"
    else:
        body = json.dumps(params).encode()
        ctype = "application/json; charset=utf-8"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": ctype}
    for attempt in range(4):
        try:
            req = urllib.request.Request(f"{SLACK_API}/{method}", body, headers)
            with urllib.request.urlopen(req, timeout=60) as r:
                resp = json.load(r)
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < 3:
                time.sleep(int(e.headers.get("Retry-After", 5)))
                continue
            raise SlackPostError(f"Slack {method} returned HTTP {e.code} ({e.reason})")
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            if attempt < 3:
                time.sleep(5 * (attempt + 1))
                continue
            raise SlackPostError(f"could not reach Slack ({method}): {e}")
        if resp.get("ok"):
            return resp
        err = resp.get("error", "unknown_error")
        if err == "ratelimited" and attempt < 3:
            time.sleep(5)
            continue
        hint = SLACK_HINTS.get(err, "")
        if err == "missing_scope":
            hint = (f"the Slack app needs the '{resp.get('needed', '?')}' scope; add it under "
                    "OAuth & Permissions, reinstall the app, and update SLACK_BOT_TOKEN")
        raise SlackPostError(f"Slack {method} failed ({err})" + (f": {hint}" if hint else ""))


def slack_upload(token, channel, thread_ts, path, title, comment):
    """Share a file in a thread: reserve an upload URL, send the bytes, complete."""
    data = open(path, "rb").read()
    name = os.path.basename(path)
    r = slack_api(token, "files.getUploadURLExternal", {"filename": name, "length": len(data)}, form=True)
    req = urllib.request.Request(r["upload_url"], data, {"Content-Type": "application/octet-stream"})
    try:
        urllib.request.urlopen(req, timeout=120).read()
    except Exception as e:
        raise SlackPostError(f"uploading {name} to Slack failed: {e}")
    slack_api(token, "files.completeUploadExternal",
              {"files": json.dumps([{"id": r["file_id"], "title": title}]), "channel_id": channel,
               "thread_ts": thread_ts, "initial_comment": comment}, form=True)


def deliver_slack(rep, full_text, md_path):
    """Post to Slack. With a bot token: a short summary in the channel, then every
    suite and the details as thread replies, plus the report file in the thread
    (SLACK_DETAILS = both | thread | file). With only a webhook, which can't thread
    or attach: the summary plus a link to the GitHub run, or the full report when
    there's no run to link to. Returns a description of what was posted."""
    token = os.environ.get("SLACK_BOT_TOKEN", "").strip()
    channel = os.environ.get("SLACK_CHANNEL_ID", "").strip()
    webhook = os.environ.get("SLACK_WEBHOOK_URL", "").strip()
    details = os.environ.get("SLACK_DETAILS", "both").strip().lower() or "both"

    if token and channel:
        where = {"file": "_Full report attached in the thread_ :thread:"}.get(
            details, "_Every suite, the failure details and the full report are in the thread_ :thread:")
        ts = slack_api(token, "chat.postMessage",
                       {"channel": channel, "text": format_slack_summary(rep, where),
                        "unfurl_links": False, "unfurl_media": False})["ts"]
        done = ["summary"]
        try:
            if details in ("both", "thread"):
                parts = list(chunks(format_thread(rep, details == "both")))
                for i, part in enumerate(parts, 1):
                    slack_api(token, "chat.postMessage",
                              {"channel": channel, "thread_ts": ts, "text": part,
                               "unfurl_links": False, "unfurl_media": False})
                    time.sleep(1)
                done.append(f"{len(parts)} thread repl{'ies' if len(parts) > 1 else 'y'}")
            if details in ("both", "file"):
                profile = rep.get("profile", PROFILE_ALL)
                slack_upload(token, channel, ts, md_path,
                             f"EAMxx {PROFILE_LABELS[profile]} report {rep['date']}",
                             "Full report, including the log lines behind each likely cause.")
                done.append("report file")
        except SlackPostError as e:
            raise SlackPostError(f"{e}. Already posted: {', '.join(done)}.")
        return ", ".join(done)

    if webhook:
        link = run_url()
        if link:
            n = post_webhook(webhook, format_slack_summary(
                rep, f"_Every suite, failure details and log evidence: <{link}|full report on GitHub>_"))
        else:  # nowhere to link to, so send everything
            n = post_webhook(webhook, full_text)
        return f"{n} webhook message{'s' if n > 1 else ''}"
    return None


def run_url():
    if os.environ.get("GITHUB_RUN_ID"):
        return (f"{os.environ.get('GITHUB_SERVER_URL', 'https://github.com')}/"
                f"{os.environ.get('GITHUB_REPOSITORY')}/actions/runs/{os.environ['GITHUB_RUN_ID']}")
    return None


# --------------------------------------------------------------------------- readable copy
def to_markdown(text, title=True):
    """Convert the Slack-formatted report to standard Markdown, readable as plain
    text too, and rendered nicely in the GitHub job summary."""
    out = []
    for line in text.split("\n"):
        line = re.sub(r"<([^|>]+)\|([^>]+)>", r"[\2](\1)", line)
        line = re.sub(r"(?<![\w*])\*([^*\n]+)\*(?![\w*])", r"**\1**", line)
        # Slack indents with spaces; in Markdown 4+ leading spaces would make a code
        # block, so turn indentation into list nesting instead.
        indent = len(line) - len(line.lstrip(" "))
        body = line.lstrip(" ")
        if body.startswith("• "):
            body = body[2:]
        if indent:
            line = ("    - " if indent >= 10 else "  - " if indent > 4 else "- ") + body
        out.append(line)
    if title:
        out[0] = "## " + out[0].strip("*")  # report title
    return "\n".join(out) + "\n"



def make_parser():
    ap = argparse.ArgumentParser(
        description="Report EAMxx test results from the E3SM CDash dashboard. "
                    "Posts to Slack with SLACK_BOT_TOKEN plus SLACK_CHANNEL_ID, "
                    "or falls back to SLACK_WEBHOOK_URL.")
    ap.add_argument("--date", help="CDash testing day YYYY-MM-DD (default: current)")
    ap.add_argument("--profile", choices=PROFILES, default=PROFILE_ALL,
                    help="which tests to report (default: all EAMxx tests)")
    ap.add_argument("--dry-run", action="store_true", help="don't post to Slack")
    ap.add_argument("--output-dir", default="cdash_report",
                    help="where to save the readable report (default: ./cdash_report)")
    return ap


def main(argv=None):
    args = make_parser().parse_args(argv)

    print("Fetching results from my.cdash.org (this takes a minute or two)...", file=sys.stderr)
    try:
        rep = collect(args.date, profile=args.profile)
    except (urllib.error.URLError, ssl.SSLError, TimeoutError, OSError) as e:
        print(f"\nError fetching CDash data: {e}", file=sys.stderr)
        return 1
    slack_text = format_report(rep, slack=True)
    md = format_markdown_report(rep) + format_evidence(rep)

    # 1. Always save a readable copy first, so a Slack problem can't lose the report.
    os.makedirs(args.output_dir, exist_ok=True)
    if args.profile == PROFILE_ALL:
        report_filename = f"cdash_eamxx_report_{rep['date']}.md"
        slack_filename = "slack_message.txt"
    else:
        report_filename = f"cdash_eamxx_report_{args.profile}_{rep['date']}.md"
        slack_filename = f"slack_message_{args.profile}_{rep['date']}.txt"
    md_path = os.path.join(args.output_dir, report_filename)
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(md)
    with open(os.path.join(args.output_dir, slack_filename), "w", encoding="utf-8") as f:
        f.write(slack_text + "\n")  # exact Slack text, handy for pasting in by hand

    # 2. Readable copy in the GitHub Actions job summary and the log.
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as f:
            f.write(md)
    print(md)
    print(f"Report saved to {os.path.abspath(md_path)}", file=sys.stderr)

    # 3. Slack, best effort.
    if args.dry_run:
        print("Dry run: not posting to Slack.", file=sys.stderr)
        return 0
    try:
        posted = deliver_slack(rep, slack_text, md_path)
    except SlackPostError as e:
        msg = f"Slack post failed: {e}"
        print(f"::error title=Slack post failed::{e}" if os.environ.get("GITHUB_ACTIONS") else msg,
              file=sys.stderr)
        if os.environ.get("GITHUB_STEP_SUMMARY"):
            with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as f:
                f.write(f"\n> **{msg}**\n> The full report is above and in the "
                        f"`cdash-eamxx-report-{args.profile}` artifact.\n")
        return 2  # nonzero so the workflow run shows red and someone notices
    if posted:
        print(f"Posted to Slack: {posted}.", file=sys.stderr)
    else:
        print("No Slack settings (SLACK_BOT_TOKEN + SLACK_CHANNEL_ID, or SLACK_WEBHOOK_URL); "
              "report was saved but not posted.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nCancelled.", file=sys.stderr)
        sys.exit(130)
