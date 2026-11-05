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

Both can also be run manually from the Actions tab (`workflow_dispatch`).

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
  `.github/dependabot.yml`, add a job for it in
  `.github/workflows/test-actions.yaml` (copy the `show-trigger` one), and a row
  in the "Current actions" table above. CI (`check_actions.py`) fails if any of
  these is missing.
- Keep secrets in repository/organization secrets, never in the repo.

## Releasing

Consumers pin a tag, so a change only reaches other repos when a tag moves.
Merging to `main` on its own affects nobody. All actions in this repo share the
same tags.

- Tags are `vMAJOR.MINOR.PATCH` (e.g. `v1.2.0`, never moved) plus a floating
  `vMAJOR` (e.g. `v1`) that always points at the latest release of that major.
- **Non-breaking changes** (fixes, new outputs, new actions): tag a new
  `v1.x.y` and move `v1` to it.
- **Breaking changes** (renamed or removed inputs/outputs, changed behavior that
  consumers rely on): start a new major (`v2`) and leave `v1` where it is, so
  existing users keep working until they choose to move.
- Release from `main` once CI is green:

  ```bash
  git tag -a v1.2.0 -m "<what changed>"
  git tag -fa v1 -m "v1 -> v1.2.0"
  git push origin v1.2.0
  git push --force origin v1
  ```

- Only maintainers should be able to create or move `v*` tags (a tag ruleset on
  the repo). It must allow updating tags, since moving `vMAJOR` is an update.
- Consumers who want the strongest guarantee can pin `vX.Y.Z` or a commit SHA
  instead of `vMAJOR`.

## CI

- `lint.yaml`: `actionlint` and schema validation for workflows and `action.yml`
  files, `ruff`, the unit tests and `check_actions.py` (every action is in
  `dependabot.yml`, `test-actions.yaml` and the README table) for
  `.github/scripts/`. Runs on every PR and on `main`.
- `test-actions.yaml`: runs each reusable action for real on a runner (smoke
  tests), on PRs that touch an action.
- `dependabot.yml`: weekly grouped updates of the actions used by the workflows
  and by our own actions.
