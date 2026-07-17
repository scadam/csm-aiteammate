"""Specification-driven data catalog with explicit source provenance."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .spec import DataSource, SolutionSpec


class DataCatalog:
    def __init__(self, spec: SolutionSpec, project_root: Path | None = None):
        self.spec = spec
        self.project_root = project_root or Path(__file__).resolve().parents[1]
        self.sources = {source.id: source for source in spec.data_sources}
        self._records = {source.id: self._load(source) for source in spec.data_sources}

    def _load(self, source: DataSource) -> list[dict[str, Any]]:
        if source.kind == "inline":
            return [dict(record) for record in source.records]
        if source.kind == "json":
            target = (self.project_root / source.path).resolve()
            payload = json.loads(target.read_text(encoding="utf-8"))
            if not isinstance(payload, list):
                raise ValueError(f"JSON source {source.id} must contain an array")
            return [dict(record) for record in payload]
        return []

    def provenance(self, source_id: str) -> str:
        source = self.sources[source_id]
        if source.kind in {"inline", "json"}:
            return f"fixture:{source.kind}:{source.id}"
        return f"unavailable:{source.kind}:{source.id}"

    def records(self, source_id: str, manager_id: str | None = None) -> list[dict[str, Any]]:
        source = self.sources[source_id]
        rows = self._records[source_id]
        if manager_id is not None:
            rows = [row for row in rows if str(row.get(source.manager_field, "")) == manager_id]
        return [dict(row) for row in rows]

    def subject(self, source_id: str, subject_id: str) -> dict[str, Any] | None:
        source = self.sources[source_id]
        return next(
            (dict(row) for row in self._records[source_id] if str(row.get(source.subject_id_field, "")) == subject_id),
            None,
        )

    def query(
        self, source_id: str, *, manager_id: str | None = None, subject_id: str | None = None
    ) -> list[dict[str, Any]]:
        source = self.sources[source_id]
        rows = self.records(source_id, manager_id)
        if subject_id is not None:
            rows = [row for row in rows if str(row.get(source.subject_id_field, "")) == subject_id]
        return rows

    def manager_subjects(self, manager_id: str) -> list[dict[str, Any]]:
        seen: set[tuple[str, str]] = set()
        subjects: list[dict[str, Any]] = []
        for workflow in self.spec.workflows:
            source = self.sources[workflow.subject_source]
            for row in self.records(source.id, manager_id):
                key = (source.id, str(row.get(source.subject_id_field, "")))
                if key in seen:
                    continue
                seen.add(key)
                subjects.append({"sourceId": source.id, "subjectId": key[1], **row})
        return subjects

    def all_subjects(self) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()
        for manager in self.spec.managers:
            for subject in self.manager_subjects(manager.id):
                key = (subject["sourceId"], subject["subjectId"])
                if key not in seen:
                    seen.add(key)
                    result.append(subject)
        return result
