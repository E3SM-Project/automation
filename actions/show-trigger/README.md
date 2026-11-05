# show-trigger

Composite action that prints what triggered the current workflow run: the event
name, the event action (when applicable), and the actor.

```yaml
steps:
  - uses: E3SM-Project/automation/actions/show-trigger@v1
```

No inputs or outputs. It only logs to the job output.
