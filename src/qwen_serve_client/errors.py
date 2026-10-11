"""Exceptions raised by qwen_serve_client."""

from __future__ import annotations

from typing import Any, Optional

# Statuses worth retrying: the request may never have reached the daemon, or
# the daemon declared itself temporarily unable to serve it.
TRANSIENT_STATUSES = frozenset({408, 429, 500, 502, 503, 504})

# Structured codes the daemon documents as temporary conditions.
TRANSIENT_CODES = frozenset(
    {
        "daemon_draining",
        "session_limit_exceeded",
        "workspace_runtime_unavailable",
        "attachment_upload_store_busy",
    }
)


class QwenServeError(Exception):
    """Base class for all client errors.

    `transient` marks failures that are safe to retry as-is (connection
    errors, daemon overload). Permanent failures (bad request, auth,
    not found) have it False.
    """

    def __init__(self, *args: object, transient: bool = False) -> None:
        super().__init__(*args)
        self.transient = transient


class DaemonHttpError(QwenServeError):
    """The daemon answered with a non-2xx HTTP status.

    `code` carries the daemon's structured error code (e.g.
    "session_not_found", "session_limit_exceeded") when present.
    `retry_after` is the daemon's Retry-After hint in seconds, if sent.
    """

    def __init__(
        self,
        status_code: int,
        message: str,
        *,
        code: Optional[Any] = None,
        payload: Optional[dict] = None,
        retry_after: Optional[float] = None,
    ) -> None:
        super().__init__(f"HTTP {status_code}: {message}")
        self.status_code = status_code
        self.code = code
        self.payload = payload or {}
        self.retry_after = retry_after

    @property
    def is_transient(self) -> bool:
        return (
            self.status_code in TRANSIENT_STATUSES
            or self.code in TRANSIENT_CODES
        )


class TurnError(QwenServeError):
    """The daemon reported a `turn_error` event for the prompt."""

    def __init__(
        self,
        message: str,
        *,
        code: Optional[str] = None,
        prompt_id: Optional[str] = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.prompt_id = prompt_id


class TurnTimeoutError(QwenServeError):
    """run_turn exceeded its turn_timeout; the prompt was cancelled."""

    def __init__(
        self,
        timeout: float,
        *,
        prompt_id: Optional[str] = None,
    ) -> None:
        super().__init__(f"turn did not finish within {timeout}s; cancelled")
        self.timeout = timeout
        self.prompt_id = prompt_id


class JsonOutputError(QwenServeError):
    """ask_json exhausted its attempts without getting parseable JSON."""

    def __init__(self, attempts: int, last_output: str, last_error: str) -> None:
        super().__init__(
            f"model did not produce valid JSON after {attempts} attempts: "
            f"{last_error}"
        )
        self.attempts = attempts
        self.last_output = last_output
        self.last_error = last_error


class SessionTerminatedError(QwenServeError):
    """The event stream ended because the session died or the client was evicted."""

    def __init__(self, event_type: str, data: dict) -> None:
        reason = data.get("reason") or data.get("error") or ""
        super().__init__(f"event stream terminated by {event_type!r}: {reason}")
        self.event_type = event_type
        self.data = data
