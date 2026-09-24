from __future__ import annotations

from dataclasses import dataclass
from typing import Any, TypeAlias

from pydantic import BaseModel

JsonScalar: TypeAlias = str | int | float | bool | None
JsonValue: TypeAlias = JsonScalar | dict[str, "JsonValue"] | list["JsonValue"]
JsonObject: TypeAlias = dict[str, JsonValue]


@dataclass(slots=True)
class Notification:
    method: str
    payload: JsonObject


@dataclass(slots=True)
class IncomingRequest:
    id: str | int
    method: str
    payload: JsonObject


class ServerInfo(BaseModel):
    name: str | None = None
    version: str | None = None


class InitializeResponse(BaseModel):
    serverInfo: ServerInfo | None = None


class SessionDescriptor(BaseModel):
    sessionId: str
    cwd: str | None = None
    createdAt: int
    title: str | None = None


class SessionListEntry(SessionDescriptor):
    live: bool
    persisted: bool
    # 旧 server 不带这个标志：缺省读作"未归档"，与 TS 客户端同款宽容合成。
    archived: bool = False


class SessionListResult(BaseModel):
    sessions: list[SessionListEntry]


class SessionHistoryResult(BaseModel):
    session: SessionDescriptor
    # `list[dict[str, Any]]`, not `list[JsonObject]`: JsonValue is an implicit
    # recursive alias, and pydantic 2.13 cannot build a schema for it
    # (RecursionError at import). The wire shape is the same JSON objects.
    events: list[dict[str, Any]]
    truncated: bool


class SessionResumeResult(BaseModel):
    sessionId: str
    resumed: bool


class SessionRenameResult(BaseModel):
    sessionId: str
    title: str


class SessionArchiveResult(BaseModel):
    """`session/archive` / `session/unarchive`: the id and its membership after the call."""

    sessionId: str
    archived: bool


class SessionAbortResult(BaseModel):
    """`session/abort`: whether a running turn was actually cancelled."""

    sessionId: str
    aborted: bool
