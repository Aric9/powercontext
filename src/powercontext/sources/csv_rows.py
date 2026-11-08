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

"""Row-level CSV Sources that keep one offline table record addressable as evidence.

Each data record becomes its own captured Source whose identity is derived from the
document and the primary key value, so reordering the file or editing a non-key field
never moves a citation to a different record.
"""

from __future__ import annotations

import csv
import io
import json
from collections.abc import Iterator
from urllib.parse import quote

from pydantic import BaseModel, model_validator

from powercontext.limits import MAX_SOURCE_ID_LENGTH
from powercontext.sources.definitions import AdapterSourceDefinition, SourceDefinition
from powercontext.sources.models import Source, SourceMaterialization
from powercontext.sources.projections import TEXT_EVIDENCE_PROJECTION_KEY, TextEvidence

CSV_ROW_SOURCE_NAME = "csv-row"
CSV_ROW_SOURCE_VERSION = "1"
_UTF8_BOM = "\ufeff"
_IDENTITY_PREFIX = "csv"


class CSVDocumentInput(BaseModel):
    """One complete offline CSV file to be captured row by row."""

    document_id: str
    primary_key: str
    content: bytes


class CSVRowInput(BaseModel):
    """One parsed data record plus the document identity it belongs to."""

    document_id: str
    primary_key: str
    primary_key_value: str
    columns: tuple[str, ...]
    values: tuple[str, ...]

    @model_validator(mode="after")
    def validate_row_shape(self) -> CSVRowInput:
        if not self.columns:
            raise ValueError("CSV row must carry at least one column")  # noqa: TRY003
        if len(self.columns) != len(self.values):
            raise ValueError("CSV row values must align with its columns")  # noqa: TRY003
        return self


class CSVRowSource(Source):
    """Captured row whose identity is stable across reordering and edits."""

    source_id: str
    document_id: str
    primary_key: str
    primary_key_value: str
    columns: tuple[str, ...]
    values: tuple[str, ...]

    @model_validator(mode="after")
    def validate_row_identity(self) -> CSVRowSource:
        if self.columns and len(self.columns) != len(self.values):
            raise ValueError("CSV row values must align with its columns")  # noqa: TRY003
        if self.source_id != self.name:
            raise ValueError("CSV row Source name must equal its identity")  # noqa: TRY003
        if self.source_id != csv_row_source_id(self.document_id, self.primary_key_value):
            raise ValueError("CSV row Source identity does not match its document and primary key value")  # noqa: TRY003
        return self


def parse_csv_document(value: CSVDocumentInput, /) -> tuple[CSVRowInput, ...]:
    """Parse and fully validate one CSV document before any row is returned."""

    document_id = _require_document_id(value.document_id)
    primary_key = _require_primary_key(value.primary_key)
    reader = csv.reader(io.StringIO(_decode_utf8(value.content), newline=""), strict=True)
    columns = _read_header(reader)
    if primary_key not in columns:
        raise ValueError("primary_key must exactly match a header column")  # noqa: TRY003

    rows = _read_data_rows(reader, document_id=document_id, primary_key=primary_key, columns=columns)
    # Reject the whole document up front when a row identity could never be stored.
    for row in rows:
        csv_row_source_id(document_id, row.primary_key_value)
    return rows


def csv_row_source_id(document_id: str, primary_key_value: str, /) -> str:
    """Return the delimiter-safe, bounded identity of one CSV row."""

    document_id = _require_document_id(document_id)
    if not isinstance(primary_key_value, str) or not primary_key_value or not primary_key_value.strip():
        raise ValueError("primary_key_value must be a non-empty, non-blank string")  # noqa: TRY003
    encoded_document = quote(document_id, safe="")
    encoded_key = quote(primary_key_value, safe="")
    source_id = f"{_IDENTITY_PREFIX}:{encoded_document}:{encoded_key}"
    if len(source_id) > MAX_SOURCE_ID_LENGTH:
        raise ValueError(f"CSV row Source identity must not exceed {MAX_SOURCE_ID_LENGTH} characters")  # noqa: TRY003
    return source_id


class CSVRowSourceAdapter:
    """Resolve and read the row-level CSV Source."""

    input_class = CSVRowInput
    name = CSV_ROW_SOURCE_NAME
    source_class = CSVRowSource

    async def resolve(self, value: CSVRowInput, /) -> CSVRowSource:
        source_id = csv_row_source_id(value.document_id, value.primary_key_value)
        return CSVRowSource(
            name=source_id,
            source_id=source_id,
            materialization=SourceMaterialization.CAPTURED,
            document_id=value.document_id,
            primary_key=value.primary_key,
            primary_key_value=value.primary_key_value,
            columns=value.columns,
            values=value.values,
        )

    async def read(self, source: CSVRowSource, /) -> CSVRowInput:
        return CSVRowInput(
            document_id=source.document_id,
            primary_key=source.primary_key,
            primary_key_value=source.primary_key_value,
            columns=source.columns,
            values=source.values,
        )


class CSVRowTextEvidenceProjection:
    """Expose one row as standard textual evidence without type coercion."""

    name = TEXT_EVIDENCE_PROJECTION_KEY.name
    version = TEXT_EVIDENCE_PROJECTION_KEY.version
    source_class = CSVRowSource
    output_class: type[BaseModel] = TextEvidence

    def project(self, source: CSVRowSource, /) -> TextEvidence:
        row = dict(zip(source.columns, source.values, strict=True))
        return TextEvidence(
            source_type=CSV_ROW_SOURCE_NAME,
            source_id=source.source_id,
            content=json.dumps(row, ensure_ascii=False, separators=(",", ":")),
            metadata={
                "document_id": source.document_id,
                "primary_key": source.primary_key,
                "primary_key_value": source.primary_key_value,
                "columns": list(source.columns),
                "row": row,
            },
        )


CSV_ROW_SOURCE_ADAPTER = CSVRowSourceAdapter()
CSV_ROW_SOURCE_DEFINITION: SourceDefinition[CSVRowInput, CSVRowSource, CSVRowInput] = AdapterSourceDefinition(
    CSV_ROW_SOURCE_ADAPTER,
    version=CSV_ROW_SOURCE_VERSION,
    projections=(CSVRowTextEvidenceProjection(),),
)


def _read_header(reader: Iterator[list[str]], /) -> tuple[str, ...]:
    try:
        header = next(reader)
    except StopIteration as error:
        raise ValueError("CSV document has no header record") from error  # noqa: TRY003
    except csv.Error as error:
        raise ValueError(f"CSV document is malformed: {error}") from error  # noqa: TRY003
    return _require_header(header)


def _read_data_rows(
    reader: Iterator[list[str]],
    /,
    *,
    document_id: str,
    primary_key: str,
    columns: tuple[str, ...],
) -> tuple[CSVRowInput, ...]:
    key_index = columns.index(primary_key)
    rows: list[CSVRowInput] = []
    seen_keys: set[str] = set()
    try:
        for record in reader:
            row = _row_from_record(
                record, document_id=document_id, primary_key=primary_key, columns=columns, key_index=key_index
            )
            if row is None:
                continue
            if row.primary_key_value in seen_keys:
                raise ValueError("CSV primary key values must be unique within one document")  # noqa: TRY003
            seen_keys.add(row.primary_key_value)
            rows.append(row)
    except csv.Error as error:
        raise ValueError(f"CSV document is malformed: {error}") from error  # noqa: TRY003
    return tuple(rows)


def _row_from_record(
    record: list[str],
    /,
    *,
    document_id: str,
    primary_key: str,
    columns: tuple[str, ...],
    key_index: int,
) -> CSVRowInput | None:
    if not record:
        # A completely empty physical record carries no fields to capture.
        return None
    if len(record) != len(columns):
        raise ValueError("CSV data record width must equal the header width")  # noqa: TRY003
    primary_key_value = record[key_index]
    if not primary_key_value or not primary_key_value.strip():
        raise ValueError("CSV primary key value must not be empty or blank")  # noqa: TRY003
    return CSVRowInput(
        document_id=document_id,
        primary_key=primary_key,
        primary_key_value=primary_key_value,
        columns=columns,
        values=tuple(record),
    )


def _decode_utf8(content: bytes, /) -> str:
    try:
        text = bytes(content).decode("utf-8")
    except UnicodeDecodeError as error:
        raise ValueError("CSV document must be valid UTF-8") from error  # noqa: TRY003
    if text.startswith(_UTF8_BOM):
        text = text[len(_UTF8_BOM) :]
    return text


def _require_document_id(document_id: str, /) -> str:
    if not isinstance(document_id, str) or not document_id or document_id != document_id.strip():
        raise ValueError("document_id must be a non-empty string without surrounding whitespace")  # noqa: TRY003
    return document_id


def _require_primary_key(primary_key: str, /) -> str:
    if not isinstance(primary_key, str) or not primary_key:
        raise ValueError("primary_key must name an existing column")  # noqa: TRY003
    return primary_key


def _require_header(header: list[str], /) -> tuple[str, ...]:
    if not header:
        raise ValueError("CSV document has no header fields")  # noqa: TRY003
    seen: set[str] = set()
    for column in header:
        if not column or not column.strip():
            raise ValueError("CSV header fields must be non-empty and not blank")  # noqa: TRY003
        if column in seen:
            raise ValueError("CSV header field names must be unique")  # noqa: TRY003
        seen.add(column)
    return tuple(header)


__all__ = [
    "CSV_ROW_SOURCE_ADAPTER",
    "CSV_ROW_SOURCE_DEFINITION",
    "CSV_ROW_SOURCE_NAME",
    "CSV_ROW_SOURCE_VERSION",
    "CSVDocumentInput",
    "CSVRowInput",
    "CSVRowSource",
    "CSVRowSourceAdapter",
    "CSVRowTextEvidenceProjection",
    "csv_row_source_id",
    "parse_csv_document",
]
