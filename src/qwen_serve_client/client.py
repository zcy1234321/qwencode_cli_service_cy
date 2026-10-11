"""Synchronous client for the qwen serve HTTP daemon.

Wire contract (qwen-code repo):
- docs/developers/qwen-serve-protocol.md
- docs/developers/daemon-rest-api-reference.md
"""

from __future__ import annotations

import atexit
import json
import mimetypes
import os
import threading
import time
import weakref
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterator, Optional, Union

import httpx
from httpx_sse import connect_sse

from .errors import (
    DaemonHttpError,
    JsonOutputError,
    QwenServeError,
    SessionTerminatedError,
    TurnError,
    TurnTimeoutError,
)
from .events import Event, PermissionRequest, TurnResult, parse_envelope

DEFAULT_BASE_URL = "http://127.0.0.1:4170"
APPROVAL_MODES = ("plan", "default", "auto-edit", "auto", "yolo")

# The daemon rejects attachments above 8 MiB (raw upload endpoint).
MAX_ATTACHMENT_BYTES = 8 * 1024 * 1024

JSON_INSTRUCTION = (
    "\n\n【输出要求】只输出一个合法的 JSON 值（对象或数组），"
    "不要输出任何其他文字、解释或 markdown 代码围栏。"
)

Attachment = Union[str, Path, dict[str, Any]]


def _extract_json(text: str) -> Any:
    """Best-effort JSON extraction from model output.

    Accepts a bare JSON value, a markdown-fenced block, or prose with an
    embedded JSON value. Raises ValueError when nothing parses.
    """
    s = text.strip()
    if s.startswith("```"):
        lines = s.splitlines()
        lines = lines[1:]  # drop the ```lang fence
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        s = "\n".join(lines).strip()
    try:
        return json.loads(s)
    except ValueError:
        pass
    decoder = json.JSONDecoder()
    for i, ch in enumerate(s):
        if ch in "{[":
            try:
                value, _ = decoder.raw_decode(s[i:])
                return value
            except ValueError:
                continue
    raise ValueError("no parseable JSON value in output")


@dataclass
class SessionInfo:
    session_id: str
    workspace_cwd: Optional[str]
    attached: bool
    raw: dict[str, Any]


@dataclass
class PromptAck:
    prompt_id: Optional[str]
    last_event_id: Optional[int]
    event_epoch: Optional[str]
    raw: dict[str, Any]


class QwenServeClient:
    """Synchronous client for one qwen serve daemon.

    token defaults to the QWEN_SERVER_TOKEN environment variable. On a plain
    loopback binding without --require-auth the daemon accepts requests with
    no token at all.

    Safe requests (health/capabilities/session lifecycle) are retried on
    transient daemon errors (max_retries). Sessions created by this client
    are best-effort closed at process exit as a safety net.
    """

    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        token: Optional[str] = None,
        *,
        client_id: Optional[str] = None,
        timeout: float = 30.0,
        sse_read_timeout: float = 90.0,
        max_retries: int = 2,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        if token is None:
            token = os.environ.get("QWEN_SERVER_TOKEN")
        headers: dict[str, str] = {}
        if token:
            headers["Authorization"] = f"Bearer {token.strip()}"
        if client_id:
            headers["X-Qwen-Client-Id"] = client_id
        self._http = httpx.Client(
            base_url=self.base_url, headers=headers, timeout=timeout
        )
        self._sse_read_timeout = sse_read_timeout
        self._max_retries = max_retries
        self._capabilities: Optional[dict[str, Any]] = None
        self._open_sessions: set[str] = set()
        self._register_atexit_cleanup()

    def _register_atexit_cleanup(self) -> None:
        ref = weakref.ref(self)

        def _cleanup() -> None:
            client = ref()
            if client is not None:
                client._close_sessions_at_exit()

        atexit.register(_cleanup)

    def __enter__(self) -> "QwenServeClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        self._http.close()

    def _close_sessions_at_exit(self) -> None:
        for sid in list(self._open_sessions):
            try:
                self._http.request("DELETE", f"/session/{sid}", timeout=3)
            except Exception:
                pass

    # ------------------------------------------------------------------
    # HTTP plumbing
    # ------------------------------------------------------------------

    def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        try:
            resp = self._http.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            raise QwenServeError(
                f"cannot reach qwen serve at {self.base_url}: {exc}",
                transient=True,
            ) from exc
        if resp.status_code >= 400:
            payload: Optional[dict[str, Any]] = None
            try:
                body = resp.json()
                if isinstance(body, dict):
                    payload = body
            except ValueError:
                pass
            message = resp.text
            if payload and isinstance(payload.get("error"), str):
                message = payload["error"]
            retry_after: Optional[float] = None
            raw_retry = resp.headers.get("retry-after")
            if raw_retry:
                try:
                    retry_after = float(raw_retry)
                except ValueError:
                    pass
            raise DaemonHttpError(
                resp.status_code,
                message,
                code=payload.get("code") if payload else None,
                payload=payload,
                retry_after=retry_after,
            )
        if resp.status_code == 204 or not resp.content:
            return {}
        body = resp.json()
        return body if isinstance(body, dict) else {}

    def _request_with_retry(
        self,
        method: str,
        path: str,
        *,
        retries: Optional[int] = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """_request with retries on transient failures (connection errors,
        429/5xx, daemon-documented temporary codes), honoring Retry-After."""
        attempts = 1 + (self._max_retries if retries is None else retries)
        for attempt in range(attempts):
            try:
                return self._request(method, path, **kwargs)
            except QwenServeError as exc:
                transient = exc.transient or (
                    isinstance(exc, DaemonHttpError) and exc.is_transient
                )
                if not transient or attempt == attempts - 1:
                    raise
                delay = 0.0
                if isinstance(exc, DaemonHttpError) and exc.retry_after:
                    delay = exc.retry_after
                else:
                    delay = min(2.0**attempt, 8.0)
                time.sleep(delay)
        raise AssertionError("unreachable")

    # ------------------------------------------------------------------
    # Discovery
    # ------------------------------------------------------------------

    def health(self, *, deep: bool = False) -> dict[str, Any]:
        params = {"deep": "1"} if deep else None
        return self._request_with_retry("GET", "/health", params=params)

    def capabilities(self, *, refresh: bool = False) -> dict[str, Any]:
        if self._capabilities is None or refresh:
            self._capabilities = self._request_with_retry("GET", "/capabilities")
        return self._capabilities

    def has_feature(self, name: str) -> bool:
        features = self.capabilities().get("features") or []
        return name in features

    # ------------------------------------------------------------------
    # Session lifecycle
    # ------------------------------------------------------------------

    def create_session(
        self,
        *,
        cwd: Optional[str] = None,
        model: Optional[str] = None,
        approval_mode: Optional[str] = None,
        reasoning_effort: Optional[str] = None,
        scope: str = "thread",
        session_id: Optional[str] = None,
    ) -> SessionInfo:
        """Create a session.

        scope="thread" (default) always creates an independent session;
        "single" attaches to the workspace's existing session if there is one.
        model maps to startupConfig.modelServiceId (see GET /capabilities
        modelServices for valid ids).
        """
        body: dict[str, Any] = {"sessionScope": scope}
        if cwd:
            body["cwd"] = cwd
        if approval_mode:
            if approval_mode not in APPROVAL_MODES:
                raise ValueError(
                    f"approval_mode must be one of {APPROVAL_MODES}"
                )
            body["approvalMode"] = approval_mode
        if session_id:
            body["sessionId"] = session_id
        startup: dict[str, Any] = {}
        if model:
            startup["modelServiceId"] = model
        if reasoning_effort:
            startup["reasoningEffort"] = reasoning_effort
        if startup:
            body["startupConfig"] = startup
        resp = self._request_with_retry("POST", "/session", json=body)
        info = SessionInfo(
            session_id=resp["sessionId"],
            workspace_cwd=resp.get("workspaceCwd"),
            attached=bool(resp.get("attached")),
            raw=resp,
        )
        if not info.attached:
            self._open_sessions.add(info.session_id)
        return info

    def resume_session(
        self,
        session_id: str,
        *,
        cwd: Optional[str] = None,
        approval_mode: Optional[str] = None,
    ) -> dict[str, Any]:
        """Re-attach the daemon to a persisted session after a restart."""
        body: dict[str, Any] = {}
        if cwd:
            body["cwd"] = cwd
        if approval_mode:
            body["approvalMode"] = approval_mode
        return self._request_with_retry(
            "POST", f"/session/{session_id}/resume", json=body
        )

    def close_session(self, session_id: str) -> None:
        """Close a session. Idempotent: unknown/closing sessions are ignored."""
        self._open_sessions.discard(session_id)
        try:
            self._request_with_retry("DELETE", f"/session/{session_id}")
        except DaemonHttpError as exc:
            if exc.status_code == 404 or exc.code in (
                "session_not_found",
                "session_closing",
            ):
                return
            raise

    def session_pool(self, *, size: int = 4, **session_kwargs: Any) -> "SessionPool":
        """Create a pool of reusable sessions for concurrent embedded tasks."""
        return SessionPool(self, size=size, **session_kwargs)

    def set_approval_mode(
        self, session_id: str, mode: str, *, persist: bool = False
    ) -> dict[str, Any]:
        if mode not in APPROVAL_MODES:
            raise ValueError(f"mode must be one of {APPROVAL_MODES}")
        return self._request(
            "POST",
            f"/session/{session_id}/approval-mode",
            json={"mode": mode, "persist": persist},
        )

    def respond_permission(
        self, session_id: str, request_id: str, option_id: str
    ) -> dict[str, Any]:
        """Answer a permission_request event by selecting an offered option."""
        return self._request(
            "POST",
            f"/session/{session_id}/permission/{request_id}",
            json={"outcome": {"outcome": "selected", "optionId": option_id}},
        )

    def cancel_permission(self, session_id: str, request_id: str) -> dict[str, Any]:
        """Drop a pending permission_request (agent sees outcome cancelled)."""
        return self._request(
            "POST",
            f"/session/{session_id}/permission/{request_id}",
            json={"outcome": {"outcome": "cancelled"}},
        )

    # ------------------------------------------------------------------
    # Prompting
    # ------------------------------------------------------------------

    def upload_attachment(
        self,
        session_id: str,
        file: Union[str, Path, bytes],
        *,
        name: Optional[str] = None,
        mime_type: Optional[str] = None,
    ) -> dict[str, Any]:
        """Upload a file to a session; returns the attachment reference dict
        {type, attachmentId, mimeType, size} usable as a prompt content block.

        file may be a path (mime guessed from the suffix) or raw bytes
        (name required). Hard daemon limit: 8 MiB per attachment.
        Requires daemon feature `session_attachments` (absent in 0.21.x);
        preflight with has_feature() if you target old daemons.
        """
        if isinstance(file, (str, Path)):
            path = Path(file)
            data = path.read_bytes()
            name = name or path.name
        else:
            data = bytes(file)
            if not name:
                raise ValueError("name is required when file is bytes")
        mime = (
            mime_type
            or mimetypes.guess_type(name)[0]
            or "application/octet-stream"
        )
        if len(data) > MAX_ATTACHMENT_BYTES:
            raise ValueError(
                f"attachment exceeds the 8 MiB daemon limit: {len(data)} bytes"
            )
        return self._request(
            "POST",
            f"/session/{session_id}/attachments",
            params={"name": name},
            content=data,
            headers={"Content-Type": mime},
        )

    def prompt(
        self,
        session_id: str,
        text: str = "",
        *,
        attachments: Optional[list[Attachment]] = None,
        event_detail_mode: Optional[str] = None,
    ) -> PromptAck:
        """Enqueue a prompt. Returns the 202 ack; results arrive over SSE.

        attachments items may be local paths (uploaded automatically),
        attachment reference dicts from upload_attachment(), or raw daemon
        content blocks.
        """
        blocks: list[dict[str, Any]] = []
        if text:
            blocks.append({"type": "text", "text": text})
        for item in attachments or []:
            if isinstance(item, dict):
                blocks.append(item)
            else:
                blocks.append(self.upload_attachment(session_id, item))
        if not blocks:
            raise ValueError("prompt requires text and/or attachments")
        body: dict[str, Any] = {"prompt": blocks}
        if event_detail_mode:
            body["eventDetailMode"] = event_detail_mode
        resp = self._request("POST", f"/session/{session_id}/prompt", json=body)
        last_event_id = resp.get("lastEventId")
        return PromptAck(
            prompt_id=resp.get("promptId"),
            last_event_id=int(last_event_id) if last_event_id is not None else None,
            event_epoch=resp.get("eventEpoch"),
            raw=resp,
        )

    def cancel(self, session_id: str) -> None:
        """Cancel the session's currently active prompt (204)."""
        self._request("POST", f"/session/{session_id}/cancel", json={})

    # ------------------------------------------------------------------
    # SSE event stream
    # ------------------------------------------------------------------

    def iter_events(
        self,
        session_id: str,
        *,
        last_event_id: Optional[int] = None,
        epoch: Optional[str] = None,
        max_reconnects: int = 3,
        reconnect_delay: float = 1.0,
        deadline: Optional[float] = None,
        max_total_reconnects: Optional[int] = 30,
    ) -> Iterator[Event]:
        """Yield events from GET /session/:id/events.

        Reconnects with Last-Event-ID after dropped connections. Stops after a
        terminal frame (session_died / session_closed / client_evicted /
        stream_error), a clean server-side close, or when deadline
        (time.monotonic()-based) is exceeded. The SSE socket has a read
        timeout (sse_read_timeout; the daemon heartbeats every 15s), so a
        dead connection triggers a reconnect instead of hanging forever.

        max_reconnects caps consecutive failures within one outage (reset on
        each successful connect); max_total_reconnects caps cumulative
        reconnects for the whole call so a flapping connection cannot loop
        forever (None/0 = unlimited).
        """
        next_last = last_event_id
        attempt = 0
        total_reconnects = 0
        while True:
            headers: dict[str, str] = {}
            if next_last is not None:
                headers["Last-Event-ID"] = str(next_last)
            if epoch:
                headers["X-Qwen-Event-Epoch"] = epoch
            timeout: float = self._sse_read_timeout
            if deadline is not None:
                timeout = max(
                    1.0, min(timeout, deadline - time.monotonic())
                )
            try:
                with connect_sse(
                    self._http,
                    "GET",
                    f"/session/{session_id}/events",
                    headers=headers,
                    timeout=timeout,
                ) as sse:
                    attempt = 0
                    for frame in sse.iter_sse():
                        if deadline is not None and time.monotonic() > deadline:
                            return
                        event = parse_envelope(frame.data, sse_id=frame.id)
                        if event is None:
                            continue
                        if event.id is not None:
                            next_last = event.id
                        yield event
                        if event.is_terminal:
                            return
                    return
            except httpx.TimeoutException:
                if deadline is not None and time.monotonic() >= deadline:
                    return
                attempt += 1
                total_reconnects += 1
                if attempt > max_reconnects or (
                    max_total_reconnects
                    and total_reconnects > max_total_reconnects
                ):
                    raise
                time.sleep(reconnect_delay * attempt)
            except httpx.HTTPError:
                attempt += 1
                total_reconnects += 1
                if attempt > max_reconnects or (
                    max_total_reconnects
                    and total_reconnects > max_total_reconnects
                ):
                    raise
                time.sleep(reconnect_delay * attempt)

    # ------------------------------------------------------------------
    # High-level helpers
    # ------------------------------------------------------------------

    def run_turn(
        self,
        session_id: str,
        text: str = "",
        *,
        attachments: Optional[list[Attachment]] = None,
        on_text: Optional[Callable[[str], None]] = None,
        on_event: Optional[Callable[[Event], None]] = None,
        on_permission: Optional[Callable[[PermissionRequest], Optional[str]]] = None,
        turn_timeout: Optional[float] = None,
        max_reconnects: int = 3,
    ) -> TurnResult:
        """Send one prompt and consume events until the turn finishes.

        The prompt ack carries lastEventId — the newest event id at enqueue
        time — so subscribing with Last-Event-ID=ack.lastEventId replays
        exactly this turn's events, with no ordering race against the SSE
        connection.

        on_permission: called for each permission_request event; return an
        optionId to vote with, or None to leave the request for another
        voter (e.g. a human on the Web UI). If the callback raises, the
        request is cancelled and the exception propagates.

        turn_timeout: wall-clock seconds for the whole turn. On expiry the
        active prompt is cancelled and TurnTimeoutError is raised.
        """
        ack = self.prompt(session_id, text, attachments=attachments)
        resume_from = ack.last_event_id if ack.last_event_id is not None else 0
        deadline = (
            time.monotonic() + turn_timeout if turn_timeout is not None else None
        )
        parts: list[str] = []
        usage: Optional[dict[str, int]] = None
        try:
            for event in self.iter_events(
                session_id,
                last_event_id=resume_from,
                epoch=ack.event_epoch,
                max_reconnects=max_reconnects,
                deadline=deadline,
            ):
                if deadline is not None and time.monotonic() > deadline:
                    raise TurnTimeoutError(turn_timeout, prompt_id=ack.prompt_id)
                if on_event is not None:
                    on_event(event)
                if event.type == "session_update":
                    chunk = event.text_chunk()
                    if chunk:
                        parts.append(chunk)
                        if on_text is not None:
                            on_text(chunk)
                    seen = event.usage()
                    if seen is not None:
                        usage = seen
                elif event.type == "permission_request":
                    preq = event.permission_request()
                    if preq is not None and on_permission is not None:
                        self._vote_permission(session_id, preq, on_permission)
                elif event.type == "turn_complete":
                    prompt_id = event.data.get("promptId")
                    if ack.prompt_id and prompt_id and prompt_id != ack.prompt_id:
                        continue
                    return TurnResult(
                        text="".join(parts),
                        stop_reason=str(event.data.get("stopReason", "unknown")),
                        prompt_id=prompt_id or ack.prompt_id,
                        usage=usage,
                    )
                elif event.type == "turn_error":
                    prompt_id = event.data.get("promptId")
                    if ack.prompt_id and prompt_id and prompt_id != ack.prompt_id:
                        continue
                    raise TurnError(
                        str(event.data.get("message", "turn failed")),
                        code=event.data.get("code"),
                        prompt_id=prompt_id or ack.prompt_id,
                    )
                elif event.is_terminal:
                    raise SessionTerminatedError(event.type, event.data)
        except TurnTimeoutError:
            try:
                self.cancel(session_id)
            except QwenServeError:
                pass
            raise
        if deadline is not None and time.monotonic() >= deadline:
            try:
                self.cancel(session_id)
            except QwenServeError:
                pass
            raise TurnTimeoutError(turn_timeout, prompt_id=ack.prompt_id)
        raise QwenServeError(
            "event stream ended without turn_complete/turn_error"
        )

    def _vote_permission(
        self,
        session_id: str,
        preq: PermissionRequest,
        on_permission: Callable[[PermissionRequest], Optional[str]],
    ) -> None:
        try:
            option_id = on_permission(preq)
        except Exception:
            try:
                self.cancel_permission(session_id, preq.request_id)
            except QwenServeError:
                pass
            raise
        if option_id is not None:
            self.respond_permission(session_id, preq.request_id, option_id)

    def ask(
        self,
        text: str,
        *,
        model: Optional[str] = None,
        approval_mode: Optional[str] = "auto",
        cwd: Optional[str] = None,
        reasoning_effort: Optional[str] = None,
        turn_timeout: Optional[float] = None,
        max_retries: int = 1,
        on_text: Optional[Callable[[str], None]] = None,
    ) -> str:
        """One-shot question: create a session, run one turn, close it.

        approval_mode="auto" lets the agent run tools without prompting a
        human; pass None to use the daemon's configured default, or "default"
        to receive permission_request events instead.

        Transient failures (connection errors, 429/5xx, daemon overload)
        are retried up to max_retries times; each attempt uses a fresh
        session, so a retried turn has no leftover context.
        """
        last_exc: Optional[QwenServeError] = None
        for attempt in range(max_retries + 1):
            session = self.create_session(
                cwd=cwd,
                model=model,
                approval_mode=approval_mode,
                reasoning_effort=reasoning_effort,
            )
            try:
                return self.run_turn(
                    session.session_id,
                    text,
                    on_text=on_text,
                    turn_timeout=turn_timeout,
                ).text
            except QwenServeError as exc:
                transient = exc.transient or (
                    isinstance(exc, DaemonHttpError) and exc.is_transient
                )
                if not transient or attempt == max_retries:
                    raise
                last_exc = exc
                delay = (
                    exc.retry_after
                    if isinstance(exc, DaemonHttpError) and exc.retry_after
                    else min(2.0**attempt, 8.0)
                )
                time.sleep(delay)
            finally:
                self.close_session(session.session_id)
        raise last_exc or QwenServeError("ask failed")

    def ask_json(
        self,
        text: str,
        *,
        model: Optional[str] = None,
        approval_mode: Optional[str] = "auto",
        cwd: Optional[str] = None,
        reasoning_effort: Optional[str] = None,
        validator: Optional[Callable[[Any], bool]] = None,
        max_attempts: int = 3,
        turn_timeout: Optional[float] = None,
    ) -> Any:
        """Ask for a structured answer; returns the parsed JSON value.

        The model is instructed to answer with JSON only. Unparseable
        output (or output rejected by validator) triggers a follow-up
        correction prompt in the same session, up to max_attempts rounds;
        then JsonOutputError is raised.
        """
        session = self.create_session(
            cwd=cwd,
            model=model,
            approval_mode=approval_mode,
            reasoning_effort=reasoning_effort,
        )
        try:
            prompt_text = text + JSON_INSTRUCTION
            last_output = ""
            last_error = ""
            for attempt in range(1, max_attempts + 1):
                result = self.run_turn(
                    session.session_id, prompt_text, turn_timeout=turn_timeout
                )
                last_output = result.text
                try:
                    value = _extract_json(result.text)
                except ValueError as exc:
                    last_error = f"JSON 解析失败: {exc}"
                else:
                    if validator is None or validator(value):
                        return value
                    last_error = "JSON 合法但未通过 validator 校验"
                if attempt < max_attempts:
                    prompt_text = (
                        f"你的上一次输出不满足要求（{last_error}）。"
                        "请只输出修正后的纯 JSON，不要任何解释或代码围栏。"
                    )
            raise JsonOutputError(max_attempts, last_output, last_error)
        finally:
            self.close_session(session.session_id)


class SessionPool:
    """A fixed pool of reusable sessions for concurrent embedded tasks.

    Keeps up to `size` sessions alive and hands them out to callers, so
    repeated tasks skip session-creation cost and multi-turn context can be
    reused per worker slot. Thread-safe.

    with client.session_pool(size=4, approval_mode="auto") as pool:
        results = [pool.run(t) for t in tasks]  # or via ThreadPoolExecutor
    """

    def __init__(
        self,
        client: QwenServeClient,
        *,
        size: int = 4,
        **session_kwargs: Any,
    ) -> None:
        if size < 1:
            raise ValueError("size must be >= 1")
        self._client = client
        self._size = size
        self._session_kwargs = session_kwargs
        self._idle: list[str] = []
        self._created = 0
        self._closed = False
        self._cond = threading.Condition()

    def acquire(self) -> str:
        """Take a session id, creating one if below size; blocks when all
        slots are checked out until another caller releases."""
        with self._cond:
            while True:
                if self._closed:
                    raise QwenServeError("session pool is closed")
                if self._idle:
                    return self._idle.pop()
                if self._created < self._size:
                    self._created += 1
                    break
                self._cond.wait()
        try:
            return self._client.create_session(**self._session_kwargs).session_id
        except Exception:
            with self._cond:
                self._created -= 1
                self._cond.notify()
            raise

    def release(self, session_id: str) -> None:
        """Return a session to the pool for reuse."""
        with self._cond:
            if self._closed:
                close_now = True
            else:
                self._idle.append(session_id)
                self._cond.notify()
                close_now = False
        if close_now:
            self._client.close_session(session_id)

    def discard(self, session_id: str) -> None:
        """Drop a broken session (turn died, timeout) instead of reusing it."""
        with self._cond:
            self._created -= 1
            self._cond.notify()
        self._client.close_session(session_id)

    @contextmanager
    def session(self) -> Iterator[str]:
        sid = self.acquire()
        try:
            yield sid
        finally:
            self.release(sid)

    def run(
        self, text: str, *, reuse_on_error: bool = False, **run_turn_kwargs: Any
    ) -> TurnResult:
        """Acquire a session, run one turn, release it. On TurnError/
        TurnTimeoutError/SessionTerminatedError the session is discarded
        (fresh one next time) unless reuse_on_error=True."""
        sid = self.acquire()
        try:
            result = self._client.run_turn(sid, text, **run_turn_kwargs)
        except (TurnError, TurnTimeoutError, SessionTerminatedError):
            if reuse_on_error:
                self.release(sid)
            else:
                self.discard(sid)
            raise
        else:
            self.release(sid)
            return result

    def close(self) -> None:
        with self._cond:
            self._closed = True
            idle, self._idle = self._idle, []
            self._cond.notify_all()
        for sid in idle:
            try:
                self._client.close_session(sid)
            except QwenServeError:
                pass

    def __enter__(self) -> "SessionPool":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def ask(
    text: str,
    *,
    base_url: str = DEFAULT_BASE_URL,
    token: Optional[str] = None,
    **kwargs: Any,
) -> str:
    """One-shot convenience: spin up a client, ask, clean up."""
    with QwenServeClient(base_url, token) as client:
        return client.ask(text, **kwargs)
