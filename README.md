# automation

Automation for the E3SM-Project GitHub organization. This is NOT for the E3SM
code itself, but for the bot-like tooling that supports the organization and its
projects.

The repo holds three kinds of things:

- **Scheduled workflows** that run on their own and report to Slack (and, in the
  future, any other bot-like task such as issue/PR housekeeping or reminders).
- **Reusable actions and workflows** that other repos in the organization can
  call, instead of each copying the same steps.
- **Supporting scripts and tests** used by the above.

## Layout

```
.github/workflows/   # scheduled/bot workflows (and reusable workflows)
actions/<name>/      # reusable composite actions, each with an action.yml
scripts/             # scripts called by the workflows
tests/               # tests for the scripts
```

## Current workflows

| Workflow | Schedule | What it does |
| --- | --- | --- |
| `cdash-eamxx-report.yaml` | weekdays, 18:00 UTC | Summarizes the EAMxx CDash results and posts them to Slack |
| `gh-ai-usage-report.yaml` | daily, 08:00 UTC | Reports the organization's GitHub AI credit usage to Slack |

Both can also be run manually from the Actions tab (`workflow_dispatch`).

## Using reusable actions from other repos

Reference an action by its full path, including the owner, the `actions/`
directory, and a ref (tag, branch, or SHA):

```yaml
steps:
  - uses: E3SM-Project/automation/actions/<name>@v1
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
  logic that is more than a few lines in `scripts/` (and a test in `tests/`).
- New shared step: add `actions/<name>/action.yml`.
- Keep secrets in repository/organization secrets, never in the repo.
