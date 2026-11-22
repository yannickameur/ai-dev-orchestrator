# Engine activity and progress events

`OrchestratorEngine.run(on_event=...)` delivers live `EngineEvent` values. The
historical import is `orchestrator.engine.EngineEvent`. Each event includes
`kind`, ISO `timestamp`, `project_id`, `mvp_id`, `work_item_id`, and `payload`.
Optional fields are `execution_id`, `phase`, `status`, `worker_id`,
`worker_display_name`, `provider`, `backend`, `profile_id`, `model`,
`quality_tier`, `reasoning_effort`, and `commit_sha`. Unknown values are
`None`.

| Kind | Payload | Meaning |
| --- | --- | --- |
| `execution.output` | `stream` (`stdout` or `stderr`), `text` | Bounded public subprocess output. Activity only. |
| `execution.output_truncated` | `reason`, `delivered_events`, `delivered_chars` | Live output display limit reached; subprocess draining continues. |
| `execution.heartbeat` | `elapsed_seconds` | Subprocess is still running during output silence. Activity only. |
| `execution.inactivity` | `reason: "no_subprocess_output"` | Ralph produced no stdout/stderr bytes for 360 seconds and its process session was terminated. |

`execution.inactivity` is followed by the phase's `*.interrupted` event.
The run's `FailureDiagnostic.interruption_reason` is `"inactivity"` or
`"timeout"` when observed, and `None` for older or non-interrupted results.
Its `last_output` is a bounded public excerpt; it is evidence of activity,
not proof of completed work. `RunResult.events` retains its historical
coarse work item events and does not accumulate live activity events.

Progress and delivery are separate. Attributed business events determine
execution success; audited Git, distinct Developer and Reviewer phases, and
deterministic QA on the audited SHA govern delivery. Output, heartbeat, and
exit code do not establish those milestones. The 360 second inactivity
limit leaves 83.1 seconds beyond the observed 276.9 second quiet BMAX
iteration and applies to Ralph executions with an absolute deadline above 360
seconds; shorter executions keep their absolute deadline. Heartbeats do
not reset inactivity, and output does not extend the absolute deadline.
