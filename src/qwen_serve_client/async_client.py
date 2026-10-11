"""Async client for the qwen serve HTTP daemon. Mirrors QwenServeClient.

Unlike the sync client there is no atexit cleanup hook (atexit cannot
await) — embedders must close sessions explicitly or use aclose()/pools.
"""

from __future__ import annotations

import asyncio
import mimetypes
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator, Awaitable, Callable, Optional, Union

import httpx
from httpx_sse import aconnect_sse

from .client import (
    APPROVAL_MODES,
    DEFAULT_BASE_URL,
    JSON_INSTRUCTION,
    MAX_ATTACHMENT_BYTES,
    Attachment,
    PromptAck,
    SessionInfo,
    _extract_json,
)
from .errors import (
    DaemonHttpError,
    JsonOutputError,
    QwenServeError,
    SessionTerminatedError,
    TurnError,
    TurnTimeoutError,
)
from .events import Event, PermissionRequest, TurnResult, parse_envelope


class AsyncQwenServeClient:
    """Asynchronous client for one qwen serve daemon.

    token defaults to the QWEN_SERVER_TOKEN environment variable. Safe
    requests (health/capabilities/session lifecycle) are retried on
    transient daemon errors (max_retries).
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
        self._http = httpx.AsyncClient(
            base_url=self.base_url, headers=headers, timeout=timeout
        )
        self._sse_read_timeout = sse_read_timeout
        self._max_retries = max_retries
        self._capabilities: Optional[dict[str, Any]] = None

    async def __aenter__(self) -> "AsyncQwenServeClient":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _request(
        self, method: str, path: str, **kwargs: Any
    ) -> dict[str, Any]:
        try:
            resp = await self._http.request(method, path, **kwargs)
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

    async def _request_with_retry(
        self,
        method: str,
        path: str,
        *,
        retries: Optional[int] = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        attempts = 1 + (self._max_retries if retries is None else retries)
        for attempt in range(attempts):
            try:
                return await self._request(method, path, **kwargs)
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
                await asyncio.sleep(delay)
        raise AssertionError("unreachable")

    async def health(self, *, deep: bool = False) -> dict[str, Any]:
        params = {"deep": "1"} if deep else None
        return await self._request_with_retry("GET", "/health", params=params)

    async def capabilities(self, *, refresh: bool = False) -> dict[str, Any]:
        if self._capabilities is None or refresh:
            self._capabilities = await self._request_with_retry(
                "GET", "/capabilities"
            )
        return self._capabilities

    async def has_feature(self, name: str) -> bool:
        features = (await self.capabilities()).get("features") or []
        return name in features

    async def create_session(
        self,
        *,
        cwd: Optional[str] = None,
        model: Optional[str] = None,
        approval_mode: Optional[str] = None,
        reasoning_effort: Optional[str] = None,
        scope: str = "thread",
        session_id: Optional[str] = None,
    ) -> SessionInfo:
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
        resp = await self._request_with_retry("POST", "/session", json=body)
        return SessionInfo(
            session_id=resp["sessionId"],
            workspace_cwd=resp.get("workspaceCwd"),
            attached=bool(resp.get("attached")),
            raw=resp,
        )

    async def resume_session(
        self,
        session_id: str,
        *,
        cwd: Optional[str] = None,
        approval_mode: Optional[str] = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {}
        if cwd:
            body["cwd"] = cwd
        if approval_mode:
            body["approvalMode"] = approval_mode
        return await self._request_with_retry(
            "POST", f"/session/{session_id}/resume", json=body
        )

    async def close_session(self, session_id: str) -> None:
        try:
            await self._request_with_retry("DELETE", f"/session/{session_id}")
        except DaemonHttpError as exc:
            if exc.status_code == 404 or exc.code in (
                "session_not_found",
                "session_closing",
            ):
                return
            raise

    def session_pool(
        self, *, size: int = 4, **session_kwargs: Any
    ) -> "AsyncSessionPool":
        """Create a pool of reusable sessions for concurrent embedded tasks."""
        return AsyncSessionPool(self, size=size, **session_kwargs)

    async def set_approval_mode(
        self, session_id: str, mode: str, *, persist: bool = False
    ) -> dict[str, Any]:
        if mode not in APPROVAL_MODES:
            raise ValueError(f"mode must be one of {APPROVAL_MODES}")
        return await self._request(
            "POST",
            f"/session/{session_id}/approval-mode",
            json={"mode": mode, "persist": persist},
        )

    async def respond_permission(
        self, session_id: str, request_id: str, option_id: str
    ) -> dict[str, Any]:
        return await self._request(
            "POST",
            f"/session/{session_id}/permission/{request_id}",
            json={"outcome": {"outcome": "selected", "optionId": option_id}},
        )

    async def cancel_permission(
        self, session_id: str, request_id: str
    ) -> dict[str, Any]:
        return await self._request(
            "POST",
            f"/session/{session_id}/permission/{request_id}",
            json={"outcome": {"outcome": "cancelled"}},
        )

    async def upload_attachment(
        self,
        session_id: str,
        file: Union[str, Path, bytes],
        *,
        name: Optional[str] = None,
        mime_type: Optional[str] = None,
    ) -> dict[str, Any]:
        """Upload a file to a session; returns the attachment reference dict
        usable as a prompt content block. Hard daemon limit: 8 MiB."""
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
        return await self._request(
            "POST",
            f"/session/{session_id}/attachments",
            params={"name": name},
            content=data,
            headers={"Content-Type": mime},
        )

    async def prompt(
        self,
        session_id: str,
        text: str = "",
        *,
        attachments: Optional[list[Attachment]] = None,
        event_detail_mode: Optional[str] = None,
    ) -> PromptAck:
        blocks: list[dict[str, Any]] = []
        if text:
            blocks.append({"type": "text", "text": text})
        for item in attachments or []:
            if isinstance(item, dict):
                blocks.append(item)
            else:
                blocks.append(await self.upload_attachment(session_id, item))
        if not blocks:
            raise ValueError("prompt requires text and/or attachments")
        body: dict[str, Any] = {"prompt": blocks}
        if event_detail_mode:
            body["eventDetailMode"] = event_detail_mode
        resp = await self._request(
            "POST", f"/session/{session_id}/prompt", json=body
        )
        last_event_id = resp.get("lastEventId")
        return PromptAck(
            prompt_id=resp.get("promptId"),
            last_event_id=int(last_event_id) if last_event_id is not None else None,
            event_epoch=resp.get("eventEpoch"),
            raw=resp,
        )

    async def cancel(self, session_id: str) -> None:
        await self._request("POST", f"/session/{session_id}/cancel", json={})

    async def iter_events(
        self,
        session_id: str,
        *,
        last_event_id: Optional[int] = None,
        epoch: Optional[str] = None,
        max_reconnects: int = 3,
        reconnect_delay: float = 1.0,
        deadline: Optional[float] = None,
        max_total_reconnects: Optional[int] = 30,
    ) -> AsyncIterator[Event]:
        """max_reconnects 限制单次断线内的连续重连（重连成功即清零）；
        max_total_reconnects 限制整个调用的累计重连次数，防止连接反复
        闪断时无限挂起（None/0 = 不限）。"""
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
                async with aconnect_sse(
                    self._http,
                    "GET",
                    f"/session/{session_id}/events",
                    headers=headers,
                    timeout=timeout,
                ) as sse:
                    attempt = 0
                    async for frame in sse.aiter_sse():
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
                await asyncio.sleep(reconnect_delay * attempt)
            except httpx.HTTPError:
                attempt += 1
                total_reconnects += 1
                if attempt > max_reconnects or (
                    max_total_reconnects
                    and total_reconnects > max_total_reconnects
                ):
                    raise
                await asyncio.sleep(reconnect_delay * attempt)

    async def run_turn(
        self,
        session_id: str,
        text: str = "",
        *,
        attachments: Optional[list[Attachment]] = None,
        on_text: Optional[Callable[[str], Any]] = None,
        on_event: Optional[Callable[[Event], Any]] = None,
        on_permission: Optional[Callable[[PermissionRequest], Any]] = None,
        turn_timeout: Optional[float] = None,
        max_reconnects: int = 3,
    ) -> TurnResult:
        ack = await self.prompt(session_id, text, attachments=attachments)
        resume_from = ack.last_event_id if ack.last_event_id is not None else 0
        deadline = (
            time.monotonic() + turn_timeout if turn_timeout is not None else None
        )
        parts: list[str] = []
        usage: Optional[dict[str, int]] = None

        async def _maybe_call(fn: Optional[Callable[..., Any]], arg: Any) -> Any:
            if fn is None:
                return None
            out = fn(arg)
            if isinstance(out, Awaitable):
                return await out
            return out

        try:
            async for event in self.iter_events(
                session_id,
                last_event_id=resume_from,
                epoch=ack.event_epoch,
                max_reconnects=max_reconnects,
                deadline=deadline,
            ):
                if deadline is not None and time.monotonic() > deadline:
                    raise TurnTimeoutError(turn_timeout, prompt_id=ack.prompt_id)
                await _maybe_call(on_event, event)
                if event.type == "session_update":
                    chunk = event.text_chunk()
                    if chunk:
                        parts.append(chunk)
                        await _maybe_call(on_text, chunk)
                    seen = event.usage()
                    if seen is not None:
                        usage = seen
                elif event.type == "permission_request":
                    preq = event.permission_request()
                    if preq is not None and on_permission is not None:
                        try:
                            option_id = await _maybe_call(on_permission, preq)
                        except Exception:
                            try:
                                await self.cancel_permission(
                                    session_id, preq.request_id
                                )
                            except QwenServeError:
                                pass
                            raise
                        if option_id is not None:
                            await self.respond_permission(
                                session_id, preq.request_id, option_id
                            )
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
                await self.cancel(session_id)
            except QwenServeError:
                pass
            raise
        if deadline is not None and time.monotonic() >= deadline:
            try:
                await self.cancel(session_id)
            except QwenServeError:
                pass
            raise TurnTimeoutError(turn_timeout, prompt_id=ack.prompt_id)
        raise QwenServeError(
            "event stream ended without turn_complete/turn_error"
        )

    async def ask(
        self,
        text: str,
        *,
        model: Optional[str] = None,
        approval_mode: Optional[str] = "auto",
        cwd: Optional[str] = None,
        reasoning_effort: Optional[str] = None,
        turn_timeout: Optional[float] = None,
        max_retries: int = 1,
        on_text: Optional[Callable[[str], Any]] = None,
    ) -> str:
        """One-shot question with a fresh session per call; transient
        failures are retried (max_retries), each attempt on a new session."""
        last_exc: Optional[QwenServeError] = None
        for attempt in range(max_retries + 1):
            session = await self.create_session(
                cwd=cwd,
                model=model,
                approval_mode=approval_mode,
                reasoning_effort=reasoning_effort,
            )
            try:
                result = await self.run_turn(
                    session.session_id,
                    text,
                    on_text=on_text,
                    turn_timeout=turn_timeout,
                )
                return result.text
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
                await asyncio.sleep(delay)
            finally:
                await self.close_session(session.session_id)
        raise last_exc or QwenServeError("ask failed")

    async def ask_json(
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
        Retries with a correction prompt until valid or max_attempts."""
        session = await self.create_session(
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
                result = await self.run_turn(
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
            await self.close_session(session.session_id)


class AsyncSessionPool:
    """Async variant of SessionPool: a fixed pool of reusable sessions."""

    def __init__(
        self,
        client: AsyncQwenServeClient,
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
        self._cond = asyncio.Condition()

    async def acquire(self) -> str:
        async with self._cond:
            while True:
                if self._closed:
                    raise QwenServeError("session pool is closed")
                if self._idle:
                    return self._idle.pop()
                if self._created < self._size:
                    self._created += 1
                    break
                await self._cond.wait()
        try:
            info = await self._client.create_session(**self._session_kwargs)
            return info.session_id
        except Exception:
            async with self._cond:
                self._created -= 1
                self._cond.notify()
            raise

    async def release(self, session_id: str) -> None:
        async with self._cond:
            if self._closed:
                close_now = True
            else:
                self._idle.append(session_id)
                self._cond.notify()
                close_now = False
        if close_now:
            await self._client.close_session(session_id)

    async def discard(self, session_id: str) -> None:
        """Drop a broken session instead of reusing it."""
        async with self._cond:
            self._created -= 1
            self._cond.notify()
        await self._client.close_session(session_id)

    @asynccontextmanager
    async def session(self) -> AsyncIterator[str]:
        sid = await self.acquire()
        try:
            yield sid
        finally:
            await self.release(sid)

    async def run(
        self, text: str, *, reuse_on_error: bool = False, **run_turn_kwargs: Any
    ) -> TurnResult:
        sid = await self.acquire()
        try:
            result = await self._client.run_turn(sid, text, **run_turn_kwargs)
        except (TurnError, TurnTimeoutError, SessionTerminatedError):
            if reuse_on_error:
                await self.release(sid)
            else:
                await self.discard(sid)
            raise
        else:
            await self.release(sid)
            return result

    async def close(self) -> None:
        async with self._cond:
            self._closed = True
            idle, self._idle = self._idle, []
            self._cond.notify_all()
        for sid in idle:
            try:
                await self._client.close_session(sid)
            except QwenServeError:
                pass

    async def __aenter__(self) -> "AsyncSessionPool":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()


async def ask(
    text: str,
    *,
    base_url: str = DEFAULT_BASE_URL,
    token: Optional[str] = None,
    **kwargs: Any,
) -> str:
    async with AsyncQwenServeClient(base_url, token) as client:
        return await client.ask(text, **kwargs)
