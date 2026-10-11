"""Thin Python client for the qwen serve HTTP daemon (REST + SSE)."""

from .async_client import AsyncQwenServeClient, AsyncSessionPool
from .async_client import ask as async_ask
from .client import (
    APPROVAL_MODES,
    DEFAULT_BASE_URL,
    MAX_ATTACHMENT_BYTES,
    PromptAck,
    QwenServeClient,
    SessionInfo,
    SessionPool,
    ask,
)
from .errors import (
    DaemonHttpError,
    JsonOutputError,
    QwenServeError,
    SessionTerminatedError,
    TurnError,
    TurnTimeoutError,
)
from .events import Event, PermissionRequest, TurnResult

__all__ = [
    "APPROVAL_MODES",
    "DEFAULT_BASE_URL",
    "MAX_ATTACHMENT_BYTES",
    "AsyncQwenServeClient",
    "AsyncSessionPool",
    "DaemonHttpError",
    "Event",
    "JsonOutputError",
    "PermissionRequest",
    "PromptAck",
    "QwenServeClient",
    "QwenServeError",
    "SessionInfo",
    "SessionPool",
    "SessionTerminatedError",
    "TurnError",
    "TurnResult",
    "TurnTimeoutError",
    "ask",
    "async_ask",
]

__version__ = "0.2.0"
