"""Safe observable-output boundary for Claude ``stream-json`` (P21.1).

``ralph run -q`` copies every raw Claude NDJSON line to stdout, including
private reasoning and tool blocks. This module is the one place that
turns that raw stream into display-safe text: NDJSON records are rebuilt
across chunks (bounded), then only explicitly allowlisted fields are
published. Everything else — private/reasoning/tool blocks, unknown
record types, malformed, incomplete or oversized records, and stderr —
is suppressed. No free-text parsing or inference. Raw captures stay
internal to the execution result.
"""

from __future__ import annotations

import json
from typing import Callable

#: Upper bound of one buffered NDJSON record; a longer record is dropped
#: through its terminating newline so memory stays bounded.
MAX_RECORD_CHARS = 262_144

_RATE_LIMIT_STATUSES = frozenset({"allowed", "allowed_warning", "rejected"})


def _public_lines(record: object) -> list[str]:
    if not isinstance(record, dict):
        return []
    kind = record.get("type")
    if kind == "assistant":
        message = record.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, list):
            return []
        # Block-level allowlist: only ``text`` blocks.
        return [
            block["text"] for block in content
            if isinstance(block, dict) and block.get("type") == "text"
            and isinstance(block.get("text"), str) and block["text"].strip()
        ]
    if kind == "result":
        subtype = record.get("subtype")
        if isinstance(subtype, str) and len(subtype) <= 64 and subtype.replace("_", "").isalnum():
            return [f"result: {subtype}"]
    elif kind == "rate_limit_event":
        info = record.get("rate_limit_info")
        status = info.get("status") if isinstance(info, dict) else None
        if status in _RATE_LIMIT_STATUSES:
            return [f"rate_limit: {status}"]
    return []


def _render(line: str) -> str:
    try:
        record = json.loads(line)
    except ValueError:
        return ""
    return "".join(text.rstrip("\n") + "\n" for text in _public_lines(record))


class ClaudePublicOutputFilter:
    """Stateful reassembler of the Claude stdout NDJSON stream. Forwards
    only the public projection of complete records to ``emit``; stderr is
    never forwarded."""

    def __init__(self, emit: Callable[[str, str], None]) -> None:
        self._emit = emit
        self._pending = ""
        self._skipping = False

    def feed(self, stream: str, text: str) -> None:
        if stream != "stdout":
            return
        *lines, self._pending = (self._pending + text).split("\n")
        for line in lines:
            if self._skipping:
                self._skipping = False  # the oversized record ends here
                continue
            self._publish(line)
        if self._skipping or len(self._pending) > MAX_RECORD_CHARS:
            self._pending, self._skipping = "", True

    def finish(self, *, flush: bool = True) -> None:
        """Flush a final unterminated record if it is complete JSON; an
        incomplete one is dropped. ``flush=False`` (timeout/cancellation)
        drops the pending data unconditionally."""
        pending, self._pending = self._pending, ""
        if flush and pending and not self._skipping:
            self._publish(pending)
        self._skipping = False

    def _publish(self, line: str) -> None:
        public = _render(line) if line.strip() else ""
        if public:
            self._emit("stdout", public)


def public_text_from_capture(stdout: bytes) -> str:
    """Public projection of a complete raw Claude stdout capture."""
    parts: list[str] = []
    stream_filter = ClaudePublicOutputFilter(lambda _stream, text: parts.append(text))
    stream_filter.feed("stdout", stdout.decode("utf-8", errors="replace"))
    stream_filter.finish()
    return "".join(parts)
