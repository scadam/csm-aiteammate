"""Revisioned draft state, validation, and digest-bound confirmation grants."""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import shutil
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Literal

import yaml

from .grants import GrantLedger, exclusive_lock, read_receipt
from .validation import validate_spec


Action = Literal["scaffold", "scaffold_and_provision"]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def canonical_digest(spec: dict[str, Any], sidecars: dict[str, str] | None = None) -> str:
    value = {"spec": spec, "sidecars": dict(sorted((sidecars or {}).items()))}
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ValidationReport:
    valid: bool
    errors: tuple[str, ...]
    warnings: tuple[str, ...]
    digest: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "valid": self.valid,
            "errors": list(self.errors),
            "warnings": list(self.warnings),
            "digest": self.digest,
        }


class DraftStore:
    """One local authoring session. Confirmation is invalidated by every mutation."""

    def __init__(self, root: str | Path, schema_path: str | Path):
        self.root = Path(root).expanduser().resolve()
        self.schema_path = Path(schema_path).resolve()
        self.session_file = self.root / "session.json"
        self.source_manifest_file = self.root / "source-manifest.json"
        self.source_file = self.root / "extracted" / "source.md"
        self.draft_file = self.root / "draft" / "solution.yaml"
        self.history_dir = self.root / "history"
        self.validation_file = self.root / "validation.json"
        self.confirmation_file = self.root / "confirmation.json"
        self.execution_file = self.root / "execution.json"
        self.snapshot_dir = self.root / "confirmed"
        self.lock_file = self.root / ".studio.lock"
        self.grants = GrantLedger(self.root)
        self.root.mkdir(parents=True, exist_ok=True)
        if not self.session_file.exists():
            self._write_json(
                self.session_file,
                {
                    "schemaVersion": 1,
                    "sessionId": self.root.name,
                    "revision": 0,
                    "status": "intake",
                    "createdAt": _now(),
                    "updatedAt": _now(),
                    "draftDigest": "",
                },
            )

    @property
    def session(self) -> dict[str, Any]:
        return json.loads(self.session_file.read_text(encoding="utf-8"))

    def ingest(self, extracted_markdown: str, manifest: dict[str, Any]) -> dict[str, Any]:
        with exclusive_lock(self.lock_file):
            self.grants.assert_mutable(self.session["sessionId"])
            _atomic_write(self.source_file, extracted_markdown.rstrip() + "\n")
            self._write_json(self.source_manifest_file, manifest)
            session = self.session
            session.update({"status": "drafting", "updatedAt": _now()})
            self._write_json(self.session_file, session)
            self._invalidate_confirmation()
            return session

    def seed_from(self, spec_path: str | Path) -> ValidationReport:
        """Seed a draft and only the local sidecars explicitly referenced by it."""
        source = Path(spec_path).resolve()
        spec = yaml.safe_load(source.read_text(encoding="utf-8"))
        if not isinstance(spec, dict):
            raise ValueError("Seed specification must contain an object")
        self.draft_file.parent.mkdir(parents=True, exist_ok=True)
        for relative in _sidecar_paths(spec):
            origin = _confined(source.parent, relative)
            if not origin.is_file():
                raise ValueError(f"Seed sidecar does not exist: {relative}")
            target = _confined(self.draft_file.parent, relative)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(origin, target)
        return self.set_draft(spec, actor="studio", channel="chat")

    def write_sidecar(
        self,
        relative_path: str,
        content: str,
        *,
        base_revision: int,
        actor: str,
        channel: Literal["chat", "ui"],
    ) -> ValidationReport:
        with exclusive_lock(self.lock_file):
            session = self.session
            self.grants.assert_mutable(session["sessionId"])
            if base_revision != session["revision"]:
                raise ValueError(
                    f"Stale draft revision {base_revision}; current revision is {session['revision']}"
                )
            if relative_path not in _sidecar_paths(self.draft()):
                raise ValueError("Sidecar is not referenced by the current specification")
            target = _confined(self.draft_file.parent, relative_path)
            _atomic_write(target, content)
            report = self.validate()
            self._record_history(
                [{"op": "sidecar", "path": relative_path}],
                actor=actor,
                channel=channel,
                base_digest=session.get("draftDigest", ""),
                result_digest=report.digest,
            )
            self._advance(report)
            return report

    def draft(self) -> dict[str, Any]:
        if not self.draft_file.is_file():
            raise FileNotFoundError("No draft has been set for this studio session")
        value = yaml.safe_load(self.draft_file.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError("Draft specification must be an object")
        return value

    def set_draft(self, spec: dict[str, Any], *, actor: str = "agent", channel: str = "chat") -> ValidationReport:
        with exclusive_lock(self.lock_file):
            session = self.session
            self.grants.assert_mutable(session["sessionId"])
            before = session.get("draftDigest", "")
            self._write_yaml(self.draft_file, spec)
            report = self.validate()
            self._record_history(
                [{"op": "replace", "path": "", "value": spec}],
                actor=actor,
                channel=channel,
                base_digest=before,
                result_digest=report.digest,
            )
            self._advance(report)
            return report

    def apply_patch(
        self,
        operations: list[dict[str, Any]],
        *,
        base_revision: int,
        actor: str,
        channel: Literal["chat", "ui"],
    ) -> ValidationReport:
        try:
            import jsonpatch
        except ImportError as exc:
            raise RuntimeError("Install the Spec Studio dependencies to apply JSON Patch") from exc
        with exclusive_lock(self.lock_file):
            session = self.session
            self.grants.assert_mutable(session["sessionId"])
            if base_revision != session["revision"]:
                raise ValueError(
                    f"Stale draft revision {base_revision}; current revision is {session['revision']}"
                )
            current = self.draft()
            base_digest = canonical_digest(current, self.sidecar_hashes())
            try:
                updated = jsonpatch.JsonPatch(operations).apply(current, in_place=False)
            except (jsonpatch.JsonPatchException, TypeError, KeyError) as exc:
                raise ValueError(f"Invalid JSON Patch: {exc}") from exc
            if not isinstance(updated, dict):
                raise ValueError("Patch must leave the specification as an object")
            self._write_yaml(self.draft_file, updated)
            report = self.validate()
            self._record_history(
                operations,
                actor=actor,
                channel=channel,
                base_digest=base_digest,
                result_digest=report.digest,
            )
            self._advance(report)
            return report

    def validate(self) -> ValidationReport:
        spec = self.draft()
        messages: tuple[str, ...]
        try:
            validate_spec(spec, self.draft_file, schema_path=self.schema_path)
            messages = ()
        except ValueError as exc:
            messages = tuple(
                line.removeprefix("- ")
                for line in str(exc).splitlines()
                if line and not line.endswith("validation failed:")
            )
        warnings: list[str] = []
        source_manifest = (
            json.loads(self.source_manifest_file.read_text(encoding="utf-8"))
            if self.source_manifest_file.is_file()
            else {}
        )
        warnings.extend(str(item) for item in source_manifest.get("warnings", []))
        digest = canonical_digest(spec, self.sidecar_hashes())
        report = ValidationReport(not messages, messages, tuple(warnings), digest)
        self._write_json(self.validation_file, report.as_dict())
        return report

    def confirm(
        self,
        *,
        action: Action,
        output_path: str | Path,
        force: bool = False,
        tenant_id: str = "",
        ttl_minutes: int = 60,
        expected_revision: int | None = None,
        expected_digest: str = "",
    ) -> dict[str, Any]:
        with exclusive_lock(self.lock_file):
            session = self.session
            self.grants.assert_mutable(session["sessionId"])
            report = self.validate()
            if not report.valid:
                raise ValueError("Cannot confirm an invalid specification")
            if expected_revision is not None and expected_revision != session["revision"]:
                raise ValueError("Draft revision changed before confirmation")
            if expected_digest and expected_digest != report.digest:
                raise ValueError("Draft digest changed before confirmation")
            if action == "scaffold_and_provision" and not tenant_id:
                raise ValueError("Tenant ID is required when A365 provisioning is authorized")
            output = Path(output_path).expanduser().resolve()
            if output == self.root or self.root in output.parents:
                raise ValueError("Generated output cannot be inside studio state")
            now = datetime.now(timezone.utc)
            grant_id = secrets.token_urlsafe(18)
            snapshot = self.snapshot_dir / grant_id
            self.export_draft(snapshot)
            snapshot_spec = snapshot / self.draft_file.name
            grant = {
                "grantId": grant_id,
                "sessionId": session["sessionId"],
                "revision": session["revision"],
                "digest": report.digest,
                "action": action,
                "outputPath": str(output),
                "force": bool(force),
                "tenantId": tenant_id,
                "snapshotPath": str(snapshot_spec),
                "issuedAt": now.isoformat(),
                "expiresAt": (now + timedelta(minutes=max(1, ttl_minutes))).isoformat(),
            }
            self.grants.issue(grant)
            self._write_json(
                self.confirmation_file,
                {
                    "schemaVersion": 2,
                    "grantId": grant_id,
                    "sessionId": session["sessionId"],
                },
            )
            session.update({"status": "confirmed", "updatedAt": _now()})
            self._write_json(self.session_file, session)
            return self.grants.get(grant_id) or grant

    def active_confirmation(self) -> dict[str, Any] | None:
        if not self.confirmation_file.is_file():
            return None
        try:
            _, receipt = read_receipt(self.confirmation_file)
        except ValueError:
            return None
        if receipt["sessionId"] != self.session["sessionId"]:
            return None
        grant = self.grants.get(receipt["grantId"])
        return grant if grant and grant["status"] in {"issued", "claimed", "checkpoint"} else None

    def sidecar_hashes(self) -> dict[str, str]:
        if not self.draft_file.is_file():
            return {}
        spec = self.draft()
        hashes: dict[str, str] = {}
        draft_root = self.draft_file.parent.resolve()
        for relative in sorted(_sidecar_paths(spec)):
            target = _confined(draft_root, relative)
            if not target.is_file():
                hashes[relative] = "missing"
            else:
                hashes[relative] = hashlib.sha256(target.read_bytes()).hexdigest()
        return hashes

    def export_draft(self, destination: str | Path) -> Path:
        destination = Path(destination).resolve()
        destination.mkdir(parents=True, exist_ok=True)
        for child in self.draft_file.parent.iterdir():
            target = destination / child.name
            if child.is_dir():
                shutil.copytree(child, target, dirs_exist_ok=True)
            else:
                shutil.copy2(child, target)
        return destination

    def _advance(self, report: ValidationReport) -> None:
        self._invalidate_confirmation()
        session = self.session
        session["revision"] += 1
        session.update(
            {
                "status": "review" if report.valid else "drafting",
                "updatedAt": _now(),
                "draftDigest": report.digest,
            }
        )
        self._write_json(self.session_file, session)

    def _record_history(
        self,
        operations: list[dict[str, Any]],
        *,
        actor: str,
        channel: str,
        base_digest: str,
        result_digest: str,
    ) -> None:
        sequence = self.session["revision"] + 1
        self._write_json(
            self.history_dir / f"{sequence:06d}.patch.json",
            {
                "sequence": sequence,
                "actor": actor,
                "channel": channel,
                "timestamp": _now(),
                "baseDigest": base_digest,
                "resultDigest": result_digest,
                "patch": operations,
            },
        )

    def _invalidate_confirmation(self) -> None:
        self.grants.revoke_session_issued(self.session["sessionId"])
        if self.confirmation_file.exists():
            invalidated = self.confirmation_file.with_name(
                f"confirmation.invalidated.{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S%f')}.json"
            )
            os.replace(self.confirmation_file, invalidated)

    @staticmethod
    def _write_json(path: Path, value: dict[str, Any]) -> None:
        _atomic_write(path, json.dumps(value, indent=2, sort_keys=True) + "\n")

    @staticmethod
    def _write_yaml(path: Path, value: dict[str, Any]) -> None:
        _atomic_write(path, yaml.safe_dump(value, sort_keys=False, allow_unicode=False))


def consume_confirmation(
    confirmation_path: str | Path,
    *,
    spec_path: str | Path,
    output_path: str | Path,
    force: bool,
    provision_a365: bool,
    tenant_id: str,
    sidecar_hashes: Callable[[dict[str, Any], Path], dict[str, str]] | None = None,
) -> dict[str, Any]:
    """Atomically claim an opaque confirmation receipt against its authoritative ledger."""
    path, receipt = read_receipt(confirmation_path)
    ledger = GrantLedger(path.parent)
    grant = ledger.get(receipt["grantId"])
    if grant is None or grant["sessionId"] != receipt["sessionId"]:
        raise ValueError("Confirmation receipt does not reference an authoritative grant")
    spec_file = Path(spec_path).resolve()
    spec = yaml.safe_load(spec_file.read_text(encoding="utf-8"))
    if not isinstance(spec, dict):
        raise ValueError("Specification must contain an object")
    hashes = sidecar_hashes(spec, spec_file) if sidecar_hashes else {}
    digest = canonical_digest(spec, hashes)
    if digest != grant["digest"]:
        raise ValueError("Specification or sidecar content changed after confirmation")
    if Path(output_path).resolve() != Path(grant["outputPath"]).resolve():
        raise ValueError("Output path does not match confirmation")
    if bool(force) != grant["force"]:
        raise ValueError("Force policy does not match confirmation")
    expected_provision = grant["action"] == "scaffold_and_provision"
    if bool(provision_a365) != expected_provision:
        raise ValueError("A365 provisioning authorization does not match confirmation")
    if provision_a365 and tenant_id != grant["tenantId"]:
        raise ValueError("Tenant ID does not match confirmation")
    return ledger.claim(grant["grantId"])


def _sidecar_paths(spec: dict[str, Any]) -> set[str]:
    paths: set[str] = set()
    for source in spec.get("openapi_sources", []):
        if isinstance(source, dict) and source.get("document"):
            paths.add(str(source["document"]))
    for source in spec.get("data_sources", []):
        if isinstance(source, dict) and source.get("kind") == "json" and source.get("path"):
            paths.add(str(source["path"]))
    return paths


def _confined(root: Path, relative: str) -> Path:
    candidate = Path(relative)
    if candidate.is_absolute() or candidate.drive or ".." in candidate.parts:
        raise ValueError(f"Path must stay within studio draft: {relative}")
    resolved_root = root.resolve()
    resolved = (resolved_root / candidate).resolve()
    if not resolved.is_relative_to(resolved_root):
        raise ValueError(f"Path escapes studio draft: {relative}")
    return resolved
