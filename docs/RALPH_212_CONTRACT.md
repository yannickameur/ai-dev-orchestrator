# Ralph 2.12.0 contract check

Verified locally with `ralph --version` (2.12.0), `ralph run --help`,
`ralph emit --help`, and `ralph hats validate` against both generated Claude
and Ollama configurations. The existing `run -a -q -c ... -H ... -P ...`
arguments are accepted. Generated core YAML and hats YAML validate; terminal
business topics have no hat subscribers by design because AIDO reads their
JSONL events after the run. The offline contract cases are in
`tests/test_ralph_execution_engine.py` and `tests/fixtures/ralph_212_contract.yml`.

Ralph [2.12.0](https://github.com/mikeyobrien/ralph-orchestrator/releases/tag/v2.12.0)
publishes a fix for text backends being stopped when output *quotes*
`Event emitted:`. Ralph [2.11.0](https://github.com/mikeyobrien/ralph-orchestrator/releases/tag/v2.11.0)
publishes a fix for completion on the final allowed iteration. Those release
notes establish the upstream fixes, not that either bug caused an earlier AIDO
run to fail. The earlier BMAX attempt documented in [BMAX_OLLAMA.md](BMAX_OLLAMA.md)
actually emitted `work.completed` while its configured completion topic was
`LOOP_COMPLETE`; that mismatch explains its continued loop. No prior AIDO
failure has been attributed to the quoted-output or final-iteration bugs.

AIDO keeps the proven Ollama Codex arguments. Both cloud and Ollama requests
with one success and one failure topic now use those business topics as
request-scoped completion and cancellation promises. The observed cloud run
`primary-20261122-153357` committed `00100f8` and emitted `work.completed`
on iteration three, but continued through iterations four and five because
its generated completion promise was `LOOP_COMPLETE`. Requests with multiple
terminal topics retain the legacy promise because Ralph accepts only one
completion and one cancellation topic. AIDO's verdict still requires a current
run's JSONL business event: stdout text, Ralph exit status, and a bare
`LOOP_COMPLETE` cannot supply one. Missing or conflicting terminal events
fail closed. A success event on iteration five remains a success even if an
older Ralph returns exit 2 at the iteration boundary; this retains the
2.10.1 rollback behavior. The fixtures are synthetic contract cases: their
`loop.terminate` payloads exercise AIDO's parser, not a claimed exact 2.12.0
trace. They are not evidence of a live provider acceptance run.
