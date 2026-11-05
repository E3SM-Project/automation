#!/usr/bin/env python3
"""Check that every reusable action at the repo root is registered where it must be.

An action is a top-level directory (not starting with a dot) with an action.yml.
For each one we require:
  - .github/dependabot.yml: a "/<name>" entry under `directories`
  - .github/workflows/test-actions.yaml: a job that uses `./<name>`
  - README.md: a row for it in the "Current actions" table
and, the other way around, that every "/<name>" entry in dependabot.yml is an action.

Standard library only (no PyYAML), so it runs anywhere. Exits 1 if anything is missing.
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def find_actions(root):
    """Names of the top-level directories that hold an action.yml/yaml."""
    return sorted(
        d.name for d in root.iterdir()
        if d.is_dir() and not d.name.startswith(".") and any((d / f).is_file() for f in ("action.yml", "action.yaml"))
    )


def read(path):
    return path.read_text() if path.is_file() else ""


def check(root):
    """Return a list of human-readable problems (empty if all is registered)."""
    problems = []
    names = find_actions(root)
    dependabot = read(root / ".github" / "dependabot.yml")
    tests = read(root / ".github" / "workflows" / "test-actions.yaml")
    readme = read(root / "README.md")

    for name in names:
        n = re.escape(name)
        if not re.search(rf"""^\s*-\s*["']?/{n}["']?\s*$""", dependabot, re.M):
            problems.append(f"{name}: add '- \"/{name}\"' to the directories in .github/dependabot.yml")
        if not re.search(rf"^\s*(?:-\s*)?uses:\s*\./{n}\s*$", tests, re.M):
            problems.append(f"{name}: add a job that runs 'uses: ./{name}' to .github/workflows/test-actions.yaml")
        if not re.search(rf"^\|\s*`{n}`\s*\|", readme, re.M):
            problems.append(f"{name}: add a row for it to the 'Current actions' table in README.md")

    # Stale dependabot entries ("/" is the workflows directory, not an action)
    for entry in re.findall(r"""^\s*-\s*["']?/([^"'\s/]+)["']?\s*$""", dependabot, re.M):
        if entry not in names:
            problems.append(f"{entry}: listed in .github/dependabot.yml but there is no {entry}/action.yml")
    return problems


def main():
    names = find_actions(ROOT)
    problems = check(ROOT)
    if problems:
        print("Problems with the registered actions:", file=sys.stderr)
        for p in problems:
            print(f"  - {p}", file=sys.stderr)
        return 1
    print(f"OK: {len(names)} action(s) registered everywhere: {', '.join(names) or '(none)'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
