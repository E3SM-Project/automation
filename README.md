# automation

Automation for the E3SM-Project GitHub organization. This is NOT for the E3SM
code itself, but for the bot-like tooling that supports the organization and its
projects.

The repo holds three kinds of things:

- **Scheduled workflows** that run on their own and report to Slack (and, in the
  future, any other bot-like task such as issue/PR housekeeping or reminders).
- **Reusable actions and workflows** that other repos in the organization can
  call, instead of each copying the same steps.
- **Supporting scripts and their tests** used by the above, in `.github/scripts/`.

## Layout

```
<name>/                 # one directory per reusable composite action: action.yml,
                        #   README.md, and (optionally) tests/ with its test fixtures
.github/workflows/      # scheduled/bot workflows (and reusable workflows)
.github/scripts/        # scripts called by the workflows
.github/scripts/tests/  # unit tests for those scripts
```

Action directories live at the repo root, so they can be referenced as
`E3SM-Project/automation/<name>@<ref>`. An action directory can't start with a
dot (`.github` is taken).

## Current workflows

| Workflow | Schedule | What it does |
| --- | --- | --- |
| `cdash-eamxx-report.yaml` | weekdays, 18:00 UTC | Summarizes the EAMxx CDash results and posts them to Slack |
| `gh-ai-usage-report.yaml` | daily, 08:00 UTC | Reports the organization's GitHub AI credit usage to Slack |

Both can also be run manually from the Actions tab (`workflow_dispatch`), with a
`dry_run` option that only writes the job summary and doesn't post to Slack. If a
scheduled run fails, both post a failure alert to Slack. The AI-usage workflow also
runs as a dry run on PRs that touch it or its script.

## Current actions

| Action | What it does |
| --- | --- |
| `show-trigger` | Prints the event, actor, ref/SHA, and event-specific details (PR, push, inputs, ...) that triggered the run, also as a job summary |
| `gpu-arch` | Detects the runner's GPU (Nvidia or AMD) and outputs the Kokkos arch and CMake configs |

## Using reusable actions from other repos

Reference an action by owner, repo, directory name, and a ref (tag, branch, or
SHA). Pin a tag or SHA, not a branch:

```yaml
steps:
  - uses: E3SM-Project/automation/<name>@v1
```

Reusable workflows live in `.github/workflows/` and are called with:

```yaml
jobs:
  call:
    uses: E3SM-Project/automation/.github/workflows/<file>.yaml@v1
```

If this repo is private, enable access for other repos under
Settings → Actions → General → Access.

## Adding something new

- New bot/scheduled task: add a workflow under `.github/workflows/`, with any
  logic that is more than a few lines in `.github/scripts/` (and a test in
  `.github/scripts/tests/`).
- New shared step: add `<name>/action.yml` (+ `README.md`), add its directory to
  `.github/dependabot.yml`, and add a job for it in
  `.github/workflows/test-actions.yaml` (copy the `show-trigger` one).
- Keep secrets in repository/organization secrets, never in the repo.

## CI

- `lint.yaml`: `actionlint` and schema validation for workflows and `action.yml`
  files, `ruff` and the unit tests for `.github/scripts/`. Runs on every PR and on `main`.
- Reusable actions also get a smoke test in `test-actions.yaml` (added together
  with the first action).
