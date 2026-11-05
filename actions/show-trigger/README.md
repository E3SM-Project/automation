# show-trigger

Composite action that prints what triggered the current workflow run, in the
job log and as a table in the job summary.

```yaml
steps:
  - uses: E3SM-Project/automation/actions/show-trigger@v1
```

No inputs or outputs.

## What it prints

For every event: event name and action, actor (plus the triggering actor, if a
re-run was started by someone else), repository, ref, SHA, a link to the run
(with attempt number), and the runner.

Plus event-specific details:

| Event | Extra info |
| --- | --- |
| `pull_request`, `pull_request_review` | PR number/title/URL, `head → base`, head SHA, from-fork flag, draft flag |
| `push` | before → after SHA, number of commits, forced-push flag |
| `schedule` | the cron expression that fired |
| `workflow_dispatch` | the inputs passed |
| `workflow_run` | upstream workflow name, result, and URL |
| `issue_comment`, `pull_request_review_comment` | issue/PR, comment author, clipped comment text |

## Notes

- Purely informational: no API calls, no extra permissions, and it never fails
  the job (problems are reported as a warning).
- Event data is handled in JavaScript only, never interpolated into a shell, so
  attacker-controlled text (PR titles, comments) is safe. Long text is clipped.
