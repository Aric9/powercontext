# Copyright (c) 2026 OceanBase.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Capture one Pi session branch as immutable, incrementally imported evidence.

A Pi session file is an append-ordered tree: every record names its parent, so a
selected leaf plus its ancestors describes one visible history path. This module
turns the visible nodes of that path into ``pi-event`` Sources and keeps the import
progress in a Connector checkpoint so branch switches, appends and restarts only
import what is still missing.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote

from pydantic import BaseModel, JsonValue, model_validator

from powercontext.limits import MAX_SOURCE_ID_LENGTH
from powercontext.sources.connectors import (
    ConnectorRunCompletion,
    ConnectorRunSession,
    ConnectorRunStatus,
)
from powercontext.sources.definitions import AdapterSourceDefinition, SourceDefinition
from powercontext.sources.models import Source, SourceMaterialization
from powercontext.sources.projections import TEXT_EVIDENCE_PROJECTION_KEY, TextEvidence

PI_EVENT_SOURCE_NAME = "pi-event"
PI_EVENT_SOURCE_VERSION = "1"
PI_SESSION_CONNECTOR_NAME = "pi-session-history"
PI_SESSION_CONNECTOR_VERSION = "1"
PI_SESSION_FORMAT_VERSION = 3
_UTF8_BOM = "\ufeff"
_IDENTITY_PREFIX = "pi"
_CHECKPOINT_VERSION = 1
_UNKNOWN_TOOL_NAME = "unknown"


class PiSessionInput(BaseModel):
    """One complete UTF-8 JSONL Pi session file and the optional selected leaf."""

    content: bytes
    leaf_id: str | None = None


class PiEventInput(BaseModel):
    """One visible event on the selected branch, retained with its full record."""

    session_id: str
    event_id: str
    parent_id: str | None = None
    event_type: str
    role: str
    content: str
    record: dict[str, JsonValue]

    @model_validator(mode="after")
    def validate_event_identity(self) -> PiEventInput:
        pi_event_source_id(self.session_id, self.event_id)
        return self


class PiEventSource(Source):
    """Captured Pi event whose identity depends only on session and event ID."""

    source_id: str
    session_id: str
    event_id: str
    parent_id: str | None = None
    event_type: str
    role: str
    content: str
    record: dict[str, JsonValue]

    @model_validator(mode="after")
    def validate_event_source(self) -> PiEventSource:
        if self.source_id != self.name:
            raise ValueError("Pi event Source name must equal its identity")  # noqa: TRY003
        if self.source_id != pi_event_source_id(self.session_id, self.event_id):
            raise ValueError("Pi event Source identity does not match its session and event ID")  # noqa: TRY003
        return self


def parse_pi_session(value: PiSessionInput, /) -> tuple[PiEventInput, ...]:
    """Return the visible events of the selected branch ordered root to leaf."""

    archive = _load_archive(value.content)
    return archive.branch_events(value.leaf_id)


def pi_event_source_id(session_id: str, event_id: str, /) -> str:
    """Return the delimiter-safe, bounded identity of one Pi event."""

    for field, part in (("session_id", session_id), ("event_id", event_id)):
        if not isinstance(part, str) or not part:
            raise ValueError(f"{field} must be a non-empty string")  # noqa: TRY003
    source_id = f"{_IDENTITY_PREFIX}:{quote(session_id, safe='')}:{quote(event_id, safe='')}"
    if len(source_id) > MAX_SOURCE_ID_LENGTH:
        raise ValueError(f"Pi event Source identity must not exceed {MAX_SOURCE_ID_LENGTH} characters")  # noqa: TRY003
    return source_id


class PiEventSourceAdapter:
    """Resolve and read the Pi event Source."""

    input_class = PiEventInput
    name = PI_EVENT_SOURCE_NAME
    source_class = PiEventSource

    async def resolve(self, value: PiEventInput, /) -> PiEventSource:
        source_id = pi_event_source_id(value.session_id, value.event_id)
        return PiEventSource(
            name=source_id,
            source_id=source_id,
            materialization=SourceMaterialization.CAPTURED,
            session_id=value.session_id,
            event_id=value.event_id,
            parent_id=value.parent_id,
            event_type=value.event_type,
            role=value.role,
            content=value.content,
            record=value.record,
        )

    async def read(self, source: PiEventSource, /) -> PiEventInput:
        return PiEventInput(
            session_id=source.session_id,
            event_id=source.event_id,
            parent_id=source.parent_id,
            event_type=source.event_type,
            role=source.role,
            content=source.content,
            record=source.record,
        )


class PiEventTextEvidenceProjection:
    """Expose one event as standard textual evidence."""

    name = TEXT_EVIDENCE_PROJECTION_KEY.name
    version = TEXT_EVIDENCE_PROJECTION_KEY.version
    source_class = PiEventSource
    output_class: type[BaseModel] = TextEvidence

    def project(self, source: PiEventSource, /) -> TextEvidence:
        return TextEvidence(
            source_type=PI_EVENT_SOURCE_NAME,
            source_id=source.source_id,
            content=source.content,
            metadata={
                "session_id": source.session_id,
                "event_id": source.event_id,
                "parent_id": source.parent_id,
                "event_type": source.event_type,
                "role": source.role,
            },
        )


PI_EVENT_SOURCE_ADAPTER = PiEventSourceAdapter()
PI_EVENT_SOURCE_DEFINITION: SourceDefinition[PiEventInput, PiEventSource, PiEventInput] = AdapterSourceDefinition(
    PI_EVENT_SOURCE_ADAPTER,
    version=PI_EVENT_SOURCE_VERSION,
    projections=(PiEventTextEvidenceProjection(),),
)


@dataclass(frozen=True, slots=True)
class _Archive:
    """Fully validated session tree with every visible event materialized once."""

    session_id: str
    order: tuple[str, ...]
    parents: dict[str, str | None]
    events: dict[str, PiEventInput]

    def branch_events(self, leaf_id: str | None) -> tuple[PiEventInput, ...]:
        leaf = leaf_id if leaf_id is not None else (self.order[-1] if self.order else None)
        if leaf is None or leaf not in self.parents:
            raise ValueError(f"leaf {leaf!r} is not present in the session")  # noqa: TRY003
        chain: list[str] = []
        current: str | None = leaf
        while current is not None:
            if current in chain:
                raise ValueError("session parent chain contains a cycle")  # noqa: TRY003
            chain.append(current)
            current = self.parents[current]
        chain.reverse()
        return tuple(self.events[node] for node in chain if node in self.events)


def _load_archive(content: bytes, /) -> _Archive:
    records = _read_records(content)
    header = records[0]
    if header.get("type") != "session":
        raise ValueError("Pi session must start with a session header")  # noqa: TRY003
    if header.get("version") != PI_SESSION_FORMAT_VERSION or isinstance(header.get("version"), bool):
        raise ValueError(f"Pi session header must declare version {PI_SESSION_FORMAT_VERSION}")  # noqa: TRY003
    session_id = header.get("id")
    if not isinstance(session_id, str) or not session_id or not session_id.strip():
        raise ValueError("Pi session header must declare a non-empty session ID")  # noqa: TRY003

    order: list[str] = []
    parents: dict[str, str | None] = {}
    events: dict[str, PiEventInput] = {}
    for record in records[1:]:
        event_id = record.get("id")
        if not isinstance(event_id, str) or not event_id or not event_id.strip():
            raise ValueError("every Pi session record must declare a non-empty ID")  # noqa: TRY003
        if event_id in parents:
            raise ValueError(f"duplicate Pi session record ID {event_id!r}")  # noqa: TRY003
        parent_id = record.get("parentId")
        if parent_id is not None and parent_id not in parents:
            raise ValueError(f"parent {parent_id!r} of {event_id!r} has not appeared yet")  # noqa: TRY003
        parents[event_id] = parent_id
        order.append(event_id)
        event = _event_from_record(session_id, event_id, parent_id, record)
        if event is not None:
            events[event_id] = event
    return _Archive(session_id=session_id, order=tuple(order), parents=parents, events=events)


def _read_records(content: bytes, /) -> list[dict[str, Any]]:
    try:
        text = bytes(content).decode("utf-8")
    except UnicodeDecodeError as error:
        raise ValueError("Pi session file must be valid UTF-8") from error  # noqa: TRY003
    if text.startswith(_UTF8_BOM):
        text = text[len(_UTF8_BOM) :]
    records: list[dict[str, Any]] = []
    for number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as error:
            raise ValueError(f"Pi session line {number} is not valid JSON") from error  # noqa: TRY003
        if not isinstance(record, dict):
            raise ValueError(f"Pi session line {number} must be a JSON object")  # noqa: TRY003, TRY004
        records.append(record)
    if not records:
        raise ValueError("Pi session file contains no records")  # noqa: TRY003
    return records


def _event_from_record(
    session_id: str,
    event_id: str,
    parent_id: str | None,
    record: dict[str, Any],
    /,
) -> PiEventInput | None:
    event_type = record.get("type")
    if event_type == "message":
        message = record.get("message")
        if not isinstance(message, dict):
            return None
        role = message.get("role")
        if not isinstance(role, str) or not role:
            return None
        body = _message_body(role, message)
    elif event_type in {"compaction", "branch_summary"}:
        role = str(event_type)
        summary = record.get("summary")
        body = summary if isinstance(summary, str) else ""
    elif event_type == "custom_message":
        role = "custom"
        body = _text_blocks(record.get("content"))
    else:
        return None
    if not body:
        return None
    return PiEventInput(
        session_id=session_id,
        event_id=event_id,
        parent_id=parent_id,
        event_type=str(event_type),
        role=role,
        content=f"{role}: {body}",
        record=record,
    )


def _message_body(role: str, message: dict[str, Any], /) -> str:
    if role == "bashExecution":
        command = message.get("command")
        output = message.get("output")
        return f"$ {command if isinstance(command, str) else ''}\n{output if isinstance(output, str) else ''}"
    body = _text_blocks(message.get("content"))
    if role == "toolResult":
        tool_name = _text_or_default(message.get("toolName"))
        call_id = _text_or_default(message.get("toolCallId"))
        return f"[tool_result {tool_name} {call_id}] {body}"
    return body


def _text_blocks(content: object, /) -> str:
    """Join visible text and tool-call blocks, ignoring thinking and images."""

    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        block_type = block.get("type")
        if block_type == "text":
            text = block.get("text")
            if isinstance(text, str):
                parts.append(text)
        elif block_type == "toolCall":
            name = _text_or_default(block.get("name"))
            call_id = _text_or_default(block.get("id"))
            parts.append(f"[tool_call {name} {call_id}] {_compact_json(block.get('arguments'))}")
    return "\n".join(parts)


def _compact_json(value: object, /) -> str:
    return json.dumps(value if value is not None else {}, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _text_or_default(value: object, /) -> str:
    return value if isinstance(value, str) and value else _UNKNOWN_TOOL_NAME


def _record_digest(record: dict[str, JsonValue], /) -> str:
    encoded = json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def _checkpoint_events(checkpoint: JsonValue | None, /) -> dict[str, str]:
    if checkpoint is None:
        return {}
    if not isinstance(checkpoint, dict):
        raise ValueError("Pi connector checkpoint must be a JSON object")  # noqa: TRY003, TRY004
    events = checkpoint.get("events")
    if events is None:
        return {}
    if not isinstance(events, dict) or not all(
        isinstance(key, str) and isinstance(value, str) for key, value in events.items()
    ):
        raise ValueError("Pi connector checkpoint events must map identities to digests")  # noqa: TRY003
    return dict(events)


def _checkpoint_payload(session_id: str, events: dict[str, str], /) -> dict[str, JsonValue]:
    return {"version": _CHECKPOINT_VERSION, "session_id": session_id, "events": dict(sorted(events.items()))}


class PiSessionConnector:
    """Import new visible events of one Pi session branch into captured Sources."""

    name = PI_SESSION_CONNECTOR_NAME
    version = PI_SESSION_CONNECTOR_VERSION
    source_definitions = frozenset({PI_EVENT_SOURCE_NAME})

    def __init__(self, path: str | Path, leaf_id: str | None = None, /) -> None:
        self._path = Path(path)
        self._leaf_id = leaf_id

    async def run(self, session: ConnectorRunSession, /) -> ConnectorRunCompletion:
        archive = _load_archive(self._path.read_bytes())
        imported = _checkpoint_events(session.checkpoint)

        # A previously imported event that changed identity-preserving content is a
        # history rewrite: refuse the whole run before submitting anything new.
        for event in archive.events.values():
            identity = pi_event_source_id(event.session_id, event.event_id)
            known = imported.get(identity)
            if known is not None and known != _record_digest(event.record):
                raise ValueError(f"imported Pi event {event.event_id!r} was rewritten")  # noqa: TRY003

        branch = archive.branch_events(self._leaf_id)
        for event in branch:
            identity = pi_event_source_id(event.session_id, event.event_id)
            if identity in imported:
                continue
            await session.submit(identity, PI_EVENT_SOURCE_NAME, event)
            imported[identity] = _record_digest(event.record)

        return ConnectorRunCompletion(
            status=ConnectorRunStatus.COMPLETE,
            checkpoint=_checkpoint_payload(archive.session_id, imported),
        )


__all__ = [
    "PI_EVENT_SOURCE_ADAPTER",
    "PI_EVENT_SOURCE_DEFINITION",
    "PI_EVENT_SOURCE_NAME",
    "PI_EVENT_SOURCE_VERSION",
    "PI_SESSION_CONNECTOR_NAME",
    "PI_SESSION_CONNECTOR_VERSION",
    "PI_SESSION_FORMAT_VERSION",
    "PiEventInput",
    "PiEventSource",
    "PiEventSourceAdapter",
    "PiEventTextEvidenceProjection",
    "PiSessionConnector",
    "PiSessionInput",
    "parse_pi_session",
    "pi_event_source_id",
]
