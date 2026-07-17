from __future__ import annotations

import asyncio
import copy
import io
import json
import os
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest
import yaml


SKILL_ROOT = Path(__file__).resolve().parents[1]
REPOSITORY_ROOT = SKILL_ROOT.parents[2]
SCHEMA = SKILL_ROOT / "assets" / "solution.schema.json"
EXAMPLE = SKILL_ROOT / "assets" / "solution.example.yaml"
OPENAPI = SKILL_ROOT / "assets" / "openapi" / "incident-api.yaml"

if str(SKILL_ROOT) not in sys.path:
    sys.path.insert(0, str(SKILL_ROOT))

from studio.app import RESOURCE_MIME_TYPE, RESOURCE_URI, project_spec_graph, studio_ag_ui_events
from studio.core import DraftStore, consume_confirmation
from studio.extract import IntakeError, extract_source
import studio.server as studio_server
from studio.server import server


def _archive(path: Path, members: dict[str, str | bytes]) -> Path:
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, value in members.items():
            archive.writestr(name, value)
    return path


def _docx(path: Path) -> Path:
    return _archive(
        path,
        {
            "word/document.xml": """<?xml version="1.0" encoding="UTF-8"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>
<w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr><w:r><w:t>Claims Digital Labor</w:t></w:r></w:p>
<w:p><w:r><w:t>Triage 40,000 inbound claims each day.</w:t></w:r></w:p>
</w:body></w:document>""",
        },
    )


def _pptx(path: Path) -> Path:
    return _archive(
        path,
        {
            "ppt/presentation.xml": """<?xml version="1.0" encoding="UTF-8"?>
<p:presentation xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"
 xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
<p:sldIdLst><p:sldId id="256" r:id="rId1"/></p:sldIdLst></p:presentation>""",
            "ppt/_rels/presentation.xml.rels": """<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="slide" Target="slides/slide1.xml"/></Relationships>""",
            "ppt/slides/slide1.xml": """<?xml version="1.0" encoding="UTF-8"?>
<p:sld xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"
 xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"><p:cSld><p:spTree>
<p:sp><p:txBody><a:p><a:r><a:t>Exception Operations</a:t></a:r></a:p>
<a:p><a:r><a:t>Autonomously resolve routine invoice disputes.</a:t></a:r></a:p></p:txBody></p:sp>
</p:spTree></p:cSld></p:sld>""",
        },
    )


def _store(tmp_path: Path) -> DraftStore:
    store = DraftStore(tmp_path / "session", SCHEMA)
    store.ingest("# Requirements\n\nBuild high-volume digital labor.\n", {"warnings": []})
    store.seed_from(EXAMPLE)
    return store


def test_extracts_text_markdown_docx_and_pptx(tmp_path: Path) -> None:
    text = extract_source("request.txt", text="Resolve 10,000 cases daily.\r\n")
    assert text.source_type == "text"
    assert "10,000 cases" in text.markdown

    markdown_path = tmp_path / "request.md"
    markdown_path.write_text("# Intake\n\nManager review is required.\n", encoding="utf-8")
    markdown = extract_source(markdown_path)
    assert markdown.source_type == "markdown"
    assert markdown.markdown.startswith("# Intake")

    document = extract_source(_docx(tmp_path / "request.docx"))
    assert document.source_type == "docx"
    assert "# Claims Digital Labor" in document.markdown
    assert "40,000 inbound claims" in document.markdown

    slides = extract_source(_pptx(tmp_path / "request.pptx"))
    assert slides.source_type == "pptx"
    assert "Slide 1: Exception Operations" in slides.markdown
    assert "invoice disputes" in slides.markdown


def test_ooxml_rejects_traversal_external_relationships_and_entities(tmp_path: Path) -> None:
    traversal = _archive(
        tmp_path / "traversal.docx",
        {"word/document.xml": "<document/>", "../escape.txt": "bad"},
    )
    with pytest.raises(IntakeError, match="unsafe archive path"):
        extract_source(traversal)

    external = _archive(
        tmp_path / "external.docx",
        {
            "word/document.xml": "<document/>",
            "word/_rels/document.xml.rels": '<Relationships><Relationship TargetMode="External" Target="https://example.test"/></Relationships>',
        },
    )
    with pytest.raises(IntakeError, match="external relationship"):
        extract_source(external)

    entity = _archive(
        tmp_path / "entity.docx",
        {"word/document.xml": '<!DOCTYPE x [<!ENTITY value "bad">]><x>&value;</x>'},
    )
    with pytest.raises(IntakeError, match="forbidden XML"):
        extract_source(entity)


def test_revision_patch_and_confirmation_invalidation(tmp_path: Path) -> None:
    store = _store(tmp_path)
    revision = store.session["revision"]
    report = store.apply_patch(
        [{"op": "replace", "path": "/solution/description", "value": "Revised digital labor scope."}],
        base_revision=revision,
        actor="user",
        channel="ui",
    )
    assert report.valid
    with pytest.raises(ValueError, match="Stale draft revision"):
        store.apply_patch(
            [{"op": "replace", "path": "/solution/domain", "value": "stale"}],
            base_revision=revision,
            actor="agent",
            channel="chat",
        )

    output = tmp_path / "generated"
    store.confirm(action="scaffold", output_path=output)
    assert store.confirmation_file.is_file()
    store.apply_patch(
        [{"op": "replace", "path": "/solution/domain", "value": "digital_operations"}],
        base_revision=store.session["revision"],
        actor="agent",
        channel="chat",
    )
    assert not store.confirmation_file.exists()
    history = json.loads(sorted(store.history_dir.glob("*.json"))[-2].read_text(encoding="utf-8"))
    assert history["channel"] == "ui"


def test_confirmation_is_exact_single_use_and_cannot_escalate(tmp_path: Path) -> None:
    store = _store(tmp_path)
    output = tmp_path / "generated"
    store.confirm(action="scaffold", output_path=output)

    with pytest.raises(ValueError, match="provisioning authorization"):
        consume_confirmation(
            store.confirmation_file,
            spec_path=store.draft_file,
            output_path=output,
            force=False,
            provision_a365=True,
            tenant_id="tenant-a",
            sidecar_hashes=lambda _spec, _path: store.sidecar_hashes(),
        )

    consume_confirmation(
        store.confirmation_file,
        spec_path=store.draft_file,
        output_path=output,
        force=False,
        provision_a365=False,
        tenant_id="",
        sidecar_hashes=lambda _spec, _path: store.sidecar_hashes(),
    )
    with pytest.raises(ValueError, match="status claimed"):
        consume_confirmation(
            store.confirmation_file,
            spec_path=store.draft_file,
            output_path=output,
            force=False,
            provision_a365=False,
            tenant_id="",
            sidecar_hashes=lambda _spec, _path: store.sidecar_hashes(),
        )


def test_receipt_tamper_copy_replay_and_old_revision_are_rejected(tmp_path: Path) -> None:
    store = _store(tmp_path)
    output = tmp_path / "generated"
    grant = store.confirm(action="scaffold", output_path=output)
    original_receipt = store.confirmation_file.read_bytes()

    tampered = json.loads(original_receipt)
    tampered.update({"action": "scaffold_and_provision", "tenantId": "tenant-a"})
    store.confirmation_file.write_text(json.dumps(tampered), encoding="utf-8")
    with pytest.raises(ValueError, match="invalid shape"):
        consume_confirmation(
            store.confirmation_file,
            spec_path=grant["snapshotPath"],
            output_path=output,
            force=False,
            provision_a365=True,
            tenant_id="tenant-a",
            sidecar_hashes=lambda _spec, _path: store.sidecar_hashes(),
        )

    store.confirmation_file.write_bytes(original_receipt)
    copied = store.root / "copied-receipt.json"
    copied.write_bytes(original_receipt)
    kwargs = {
        "spec_path": grant["snapshotPath"],
        "output_path": output,
        "force": False,
        "provision_a365": False,
        "tenant_id": "",
        "sidecar_hashes": lambda _spec, _path: store.sidecar_hashes(),
    }
    consume_confirmation(store.confirmation_file, **kwargs)
    with pytest.raises(ValueError, match="status claimed"):
        consume_confirmation(copied, **kwargs)

    other = _store(tmp_path / "other")
    other_output = tmp_path / "other-output"
    other.confirm(action="scaffold", output_path=other_output)
    invalidated = other.confirmation_file.read_bytes()
    spec = other.draft()
    spec["solution"]["description"] = "A changed revision."
    other.set_draft(spec)
    stale = other.root / "stale-receipt.json"
    stale.write_bytes(invalidated)
    with pytest.raises(ValueError, match="invalidated|changed after confirmation"):
        consume_confirmation(
            stale,
            spec_path=other.draft_file,
            output_path=other_output,
            force=False,
            provision_a365=False,
            tenant_id="",
            sidecar_hashes=lambda _spec, _path: other.sidecar_hashes(),
        )


def test_semantic_invalid_draft_cannot_be_confirmed_and_snapshot_is_immutable(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    invalid = copy.deepcopy(store.draft())
    invalid["identity"]["default_manager_id"] = "missing-manager"
    report = store.set_draft(invalid)
    assert not report.valid
    with pytest.raises(ValueError, match="invalid specification"):
        store.confirm(action="scaffold", output_path=tmp_path / "denied")

    store.seed_from(EXAMPLE)
    grant = store.confirm(action="scaffold", output_path=tmp_path / "generated")
    snapshot = Path(grant["snapshotPath"])
    before = snapshot.read_bytes()
    store.draft_file.write_text("schema_version: tampered\n", encoding="utf-8")
    assert snapshot.read_bytes() == before


def test_scaffolder_cli_denies_unconfirmed_and_consumes_valid_grant(tmp_path: Path) -> None:
    scaffold = SKILL_ROOT / "scripts" / "scaffold.py"
    missing = subprocess.run(
        [sys.executable, str(scaffold), "--spec", str(EXAMPLE), "--output", str(tmp_path / "denied")],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )
    assert missing.returncode != 0
    assert "--confirmation-file" in missing.stdout

    store = _store(tmp_path / "confirmed")
    output = tmp_path / "generated"
    store.confirm(action="scaffold", output_path=output)
    completed = subprocess.run(
        [
            sys.executable,
            str(scaffold),
            "--spec",
            str(store.draft_file),
            "--output",
            str(output),
            "--confirmation-file",
            str(store.confirmation_file),
        ],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout
    assert (output / "app" / "agent.py").is_file()
    receipt = json.loads(store.confirmation_file.read_text(encoding="utf-8"))
    assert set(receipt) == {"schemaVersion", "grantId", "sessionId"}
    assert store.grants.get(receipt["grantId"])["status"] == "complete"


def test_a365_checkpoint_resumes_with_the_original_grant(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(studio_server, "DATA_ROOT", tmp_path)
    session_id = "checkpoint-resume"
    store = DraftStore(tmp_path / session_id, SCHEMA)
    store.ingest("# Checkpoint test\n", {"warnings": []})
    store.seed_from(EXAMPLE)
    output = tmp_path / "generated"
    provisioner = output / "scripts" / "provision_agent365.py"
    provisioner.parent.mkdir(parents=True)
    provisioner.write_text(
        "import json, sys\n"
        "from pathlib import Path\n"
        "Path(__file__).with_name('resume-args.json').write_text(json.dumps(sys.argv[1:]))\n",
        encoding="utf-8",
    )
    grant = store.confirm(
        action="scaffold_and_provision",
        output_path=output,
        tenant_id="tenant-a",
    )
    store.grants.claim(grant["grantId"])
    store.grants.update_status(grant["grantId"], "checkpoint")

    result = json.loads(studio_server.studio_execute(session_id))

    assert result["status"] == "complete"
    assert store.grants.get(grant["grantId"])["status"] == "complete"
    arguments = json.loads(
        provisioner.with_name("resume-args.json").read_text(encoding="utf-8")
    )
    assert arguments == ["--tenant-id", "tenant-a", "early"]


def test_graph_ag_ui_and_mcp_app_metadata(tmp_path: Path) -> None:
    store = _store(tmp_path)
    graph = project_spec_graph(store.draft())
    assert {lane["id"] for lane in graph["lanes"]} >= {
        "Purpose",
        "Identity",
        "Runtime",
        "Skills",
        "Workflows",
        "Capabilities",
        "Integrations",
        "Data",
        "Experience",
        "Governance",
    }
    events = studio_ag_ui_events(store, thread_id="thread-1", run_id="run-1")
    event_types = [event["type"] for event in events]
    assert event_types[0] == "RUN_STARTED"
    assert "STATE_SNAPSHOT" in event_types
    assert "ACTIVITY_SNAPSHOT" in event_types
    assert event_types[-1] == "RUN_FINISHED"
    assert events[-1]["outcome"]["type"] == "interrupt"

    async def inspect() -> None:
        tools = await server.list_tools()
        by_name = {tool.name: tool.meta for tool in tools}
        assert by_name["studio_get_state"]["ui"]["resourceUri"] == RESOURCE_URI
        assert by_name["studio_confirm"]["ui"]["visibility"] == ["app"]
        assert by_name["studio_patch"]["ui"]["visibility"] == ["app"]
        assert by_name["studio_chat_patch"]["ui"]["visibility"] == ["model"]
        assert by_name["studio_execute"]["ui"]["visibility"] == ["model"]
        resources = await server.list_resources()
        assert any(str(resource.uri) == RESOURCE_URI for resource in resources)
        resource = await server.read_resource(RESOURCE_URI)
        assert resource[0].mime_type == RESOURCE_MIME_TYPE
        assert "AI Teammate Spec Studio" in resource[0].content
        assert "Specification</button>" in resource[0].content
        assert "STATE.canonicalSpec" in resource[0].content

    asyncio.run(inspect())


def test_plugin_manifest_and_mcp_config_are_self_contained() -> None:
    manifest = json.loads((REPOSITORY_ROOT / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
    assert manifest["agents"] == ["./.github/agents/pattern-solution-builder.agent.md"]
    assert manifest["version"] == "1.0.0"
    assert manifest["skills"] == ["./.github/skills/ai-teammate-solution-builder/"]
    assert manifest["mcpServers"] == "./.mcp.json"
    marketplace = json.loads(
        (REPOSITORY_ROOT / ".claude-plugin" / "marketplace.json").read_text(encoding="utf-8")
    )
    assert marketplace["name"] == "scadam-ai-teammates"
    assert marketplace["plugins"] == [
        {
            "name": "ai-teammate-solution-builder",
            "source": "./",
            "description": "Turn text, Markdown, Word, or PowerPoint requirements into a reviewed and tested Agent 365 AI teammate solution.",
            "version": "1.0.0",
            "category": "development",
            "tags": ["agent-365", "digital-labor", "mcp-apps", "ag-ui"],
            "strict": True,
        }
    ]
    mcp_config = json.loads((REPOSITORY_ROOT / ".mcp.json").read_text(encoding="utf-8"))
    config = mcp_config["mcpServers"]["ai-teammate-spec-studio"]
    assert config["command"] == "python"
    assert "${CLAUDE_PLUGIN_ROOT}" in config["args"][0]
    assert "${CLAUDE_PLUGIN_DATA}" in config["env"]["AI_TEAMMATE_STUDIO_DATA"]
    assert "CLAUDE_PROJECT_DIR" not in config["env"]
