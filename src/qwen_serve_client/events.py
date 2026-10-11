"""SSE event envelope parsing for the qwen serve daemon stream.

Every SSE `data:` line carries one JSON envelope:
    {"id": 7, "v": 1, "type": "session_update", "data": {...}}

Synthetic frames (session_snapshot, replay_complete, stream_error,
client_evicted, state_resync_required, slow_client_warning) have no `id`.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Optional

# After one of these frames the daemon closes the SSE connection.
TERMINAL_STREAM_EVENTS = frozenset(
    {"session_died", "session_closed", "client_evicted", "stream_error"}
)


@dataclass
class PermissionRequest:
    """A pending `permission_request` event, awaiting a vote."""

    request_id: str
    session_id: Optional[str]
    tool_call: dict[str, Any]
    options: list[dict[str, Any]]

    @property
    def tool_name(self) -> Optional[str]:
        title = self.tool_call.get("title")
        return title if isinstance(title, str) else None

    def option_ids(self) -> list[str]:
        return [
            o["optionId"]
            for o in self.options
            if isinstance(o, dict) and isinstance(o.get("optionId"), str)
        ]


@dataclass
class Event:
    """One parsed daemon event."""

    type: str
    data: dict[str, Any]
    id: Optional[int] = None

    @property
    def is_terminal(self) -> bool:
        return self.type in TERMINAL_STREAM_EVENTS

    def _update_payload(self) -> dict[str, Any]:
        """The ACP session-update payload, nested or flattened."""
        update = self.data.get("update")
        return update if isinstance(update, dict) else self.data

    def text_chunk(self) -> Optional[str]:
        """Incremental assistant text, if this is an agent_message_chunk."""
        if self.type != "session_update":
            return None
        update = self._update_payload()
        if update.get("sessionUpdate") != "agent_message_chunk":
            return None
        content = update.get("content")
        if isinstance(content, dict) and content.get("type") == "text":
            text = content.get("text")
            return text if isinstance(text, str) else None
        return None

    def usage(self) -> Optional[dict[str, int]]:
        """Context occupancy {used, size} tokens, if this is a usage_update."""
        if self.type != "session_update":
            return None
        update = self._update_payload()
        if update.get("sessionUpdate") != "usage_update":
            return None
        used, size = update.get("used"), update.get("size")
        if isinstance(used, int) and isinstance(size, int):
            return {"used": used, "size": size}
        return None

    def permission_request(self) -> Optional[PermissionRequest]:
        """Structured view of a permission_request event, else None."""
        if self.type != "permission_request":
            return None
        request_id = self.data.get("requestId")
        if not isinstance(request_id, str):
            return None
        tool_call = self.data.get("toolCall")
        options = self.data.get("options")
        return PermissionRequest(
            request_id=request_id,
            session_id=self.data.get("sessionId"),
            tool_call=tool_call if isinstance(tool_call, dict) else {},
            options=[o for o in (options or []) if isinstance(o, dict)],
        )


@dataclass
class TurnResult:
    """Outcome of one completed prompt turn."""

    text: str
    stop_reason: str
    prompt_id: Optional[str] = None
    # Last usage_update seen during the turn: {"used": int, "size": int}
    # (context occupancy in tokens). None if the model emitted none.
    usage: Optional[dict[str, int]] = None


def parse_envelope(raw: str, *, sse_id: Optional[str] = None) -> Optional[Event]:
    """Parse one SSE `data:` payload. Returns None for undecodable frames."""
    try:
        payload = json.loads(raw)
    except (ValueError, TypeError):
        return None
    if not isinstance(payload, dict):
        return None
    event_type = payload.get("type")
    if not isinstance(event_type, str):
        return None
    data = payload.get("data")
    event_id: Optional[int] = None
    raw_id = payload.get("id", sse_id)
    if raw_id is not None:
        try:
            event_id = int(raw_id)
        except (ValueError, TypeError):
            event_id = None
    return Event(
        type=event_type,
        data=data if isinstance(data, dict) else {},
        id=event_id,
    )
