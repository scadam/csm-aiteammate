"""Bounded, local extraction for text, Markdown, DOCX, and PPTX intake."""

from __future__ import annotations

import hashlib
import io
import re
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Iterable
from xml.etree import ElementTree


MAX_SOURCE_BYTES = 25 * 1024 * 1024
MAX_ARCHIVE_MEMBERS = 5000
MAX_UNCOMPRESSED_BYTES = 100 * 1024 * 1024
MAX_COMPRESSION_RATIO = 100
MAX_EXTRACTED_CHARS = 1_000_000
SUPPORTED_SUFFIXES = {".txt", ".md", ".markdown", ".docx", ".pptx"}
_WORD_NS = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
_DRAWING_NS = {"a": "http://schemas.openxmlformats.org/drawingml/2006/main"}
_PRESENTATION_NS = {"p": "http://schemas.openxmlformats.org/presentationml/2006/main"}
_REL_NS = {"r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships"}
_PACKAGE_REL_NS = {"pr": "http://schemas.openxmlformats.org/package/2006/relationships"}


class IntakeError(ValueError):
    pass


@dataclass(frozen=True)
class ExtractedSource:
    source_name: str
    source_type: str
    sha256: str
    markdown: str
    warnings: tuple[str, ...] = ()

    def manifest(self) -> dict[str, object]:
        return {
            "sourceName": self.source_name,
            "sourceType": self.source_type,
            "sha256": self.sha256,
            "characters": len(self.markdown),
            "warnings": list(self.warnings),
        }


def extract_source(source: str | Path, *, text: str | None = None) -> ExtractedSource:
    """Extract one trusted local input; extracted text is data, never instructions."""
    if text is not None:
        name = str(source or "chat-input.md")
        content = text.encode("utf-8")
        return _result(name, "text", content, _normalize_text(text))
    path = Path(source).expanduser().resolve()
    if not path.is_file():
        raise IntakeError(f"Input file does not exist: {path}")
    suffix = path.suffix.lower()
    if suffix not in SUPPORTED_SUFFIXES:
        raise IntakeError(
            f"Unsupported input type {suffix or '<none>'}; use text, Markdown, DOCX, or PPTX"
        )
    size = path.stat().st_size
    if size > MAX_SOURCE_BYTES:
        raise IntakeError(f"Input exceeds {MAX_SOURCE_BYTES} bytes")
    content = path.read_bytes()
    if suffix in {".txt", ".md", ".markdown"}:
        try:
            text_value = content.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise IntakeError("Text and Markdown input must be UTF-8") from exc
        return _result(path.name, "markdown" if suffix != ".txt" else "text", content, _normalize_text(text_value))
    members = _preflight_ooxml(content, suffix)
    if suffix == ".docx":
        markdown, warnings = _extract_docx(content, members)
        return _result(path.name, "docx", content, markdown, warnings)
    markdown, warnings = _extract_pptx(content, members)
    return _result(path.name, "pptx", content, markdown, warnings)


def _result(
    name: str,
    source_type: str,
    content: bytes,
    markdown: str,
    warnings: Iterable[str] = (),
) -> ExtractedSource:
    if not markdown.strip():
        raise IntakeError("Input did not contain extractable text")
    if len(markdown) > MAX_EXTRACTED_CHARS:
        raise IntakeError(f"Extracted text exceeds {MAX_EXTRACTED_CHARS} characters")
    return ExtractedSource(
        source_name=Path(name).name,
        source_type=source_type,
        sha256=hashlib.sha256(content).hexdigest(),
        markdown=markdown.rstrip() + "\n",
        warnings=tuple(warnings),
    )


def _normalize_text(value: str) -> str:
    value = value.replace("\r\n", "\n").replace("\r", "\n").replace("\x00", "")
    lines = [line.rstrip() for line in value.split("\n")]
    normalized: list[str] = []
    blank = False
    for line in lines:
        if line.strip():
            normalized.append(line)
            blank = False
        elif not blank:
            normalized.append("")
            blank = True
    return "\n".join(normalized).strip()


def _preflight_ooxml(content: bytes, suffix: str) -> set[str]:
    if not content.startswith(b"PK"):
        raise IntakeError(f"{suffix.upper()} input is not a valid OOXML ZIP archive")
    try:
        archive = zipfile.ZipFile(io.BytesIO(content))
    except zipfile.BadZipFile as exc:
        raise IntakeError("Office input is not a valid ZIP archive") from exc
    infos = archive.infolist()
    if len(infos) > MAX_ARCHIVE_MEMBERS:
        raise IntakeError("Office input contains too many archive members")
    total = 0
    names: set[str] = set()
    for info in infos:
        pure = PurePosixPath(info.filename)
        if pure.is_absolute() or ".." in pure.parts or "\\" in info.filename:
            raise IntakeError("Office input contains an unsafe archive path")
        if info.is_dir():
            continue
        unix_mode = (info.external_attr >> 16) & 0o170000
        if unix_mode == 0o120000:
            raise IntakeError("Office input contains a symbolic-link archive member")
        total += info.file_size
        if total > MAX_UNCOMPRESSED_BYTES:
            raise IntakeError("Office input expands beyond the configured limit")
        compressed = max(1, info.compress_size)
        if info.file_size / compressed > MAX_COMPRESSION_RATIO:
            raise IntakeError("Office input contains a suspicious compression ratio")
        names.add(info.filename)
    forbidden = {
        name
        for name in names
        if name.lower().endswith(("vbaproject.bin", "activex.bin"))
        or "/activex/" in name.lower()
    }
    if forbidden:
        raise IntakeError("Macro-enabled or active Office content is not supported")
    with archive:
        for name in names:
            if not name.endswith(".rels"):
                continue
            raw = archive.read(name)
            if b"TargetMode=\"External\"" in raw or b"TargetMode='External'" in raw:
                raise IntakeError("Office input contains an external relationship")
    required = "word/document.xml" if suffix == ".docx" else "ppt/presentation.xml"
    if required not in names:
        raise IntakeError(f"Office input is missing {required}")
    if "EncryptedPackage" in names or "EncryptionInfo" in names:
        raise IntakeError("Encrypted or protected Office input is not supported")
    return names


def _xml(content: bytes, name: str) -> ElementTree.Element:
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        try:
            raw = archive.read(name)
        except KeyError as exc:
            raise IntakeError(f"Office input is missing {name}") from exc
    if b"<!DOCTYPE" in raw or b"<!ENTITY" in raw:
        raise IntakeError("Office input contains forbidden XML declarations")
    try:
        return ElementTree.fromstring(raw)
    except ElementTree.ParseError as exc:
        raise IntakeError(f"Office XML is malformed: {name}") from exc


def _extract_docx(content: bytes, members: set[str]) -> tuple[str, tuple[str, ...]]:
    root = _xml(content, "word/document.xml")
    blocks: list[str] = []
    body = root.find("w:body", _WORD_NS)
    if body is None:
        raise IntakeError("Word document has no body")
    for child in body:
        kind = _local(child.tag)
        if kind == "p":
            text = _word_paragraph(child)
            if not text:
                continue
            style = child.find("w:pPr/w:pStyle", _WORD_NS)
            style_name = style.attrib.get(f"{{{_WORD_NS['w']}}}val", "") if style is not None else ""
            match = re.match(r"Heading\s*([1-6])", style_name, flags=re.IGNORECASE)
            blocks.append(f"{'#' * int(match.group(1))} {text}" if match else text)
        elif kind == "tbl":
            rows = []
            for row in child.findall("w:tr", _WORD_NS):
                cells = [_word_paragraph(cell) for cell in row.findall("w:tc", _WORD_NS)]
                if cells:
                    rows.append(cells)
            blocks.extend(_markdown_table(rows))
    warnings = []
    if any(name.startswith("word/media/") for name in members):
        warnings.append("Images were not interpreted; describe relevant visual requirements in chat.")
    if any(name.startswith("word/embeddings/") for name in members):
        warnings.append("Embedded objects were ignored.")
    return _normalize_text("\n\n".join(blocks)), tuple(warnings)


def _word_paragraph(node: ElementTree.Element) -> str:
    values = []
    for text_node in node.iterfind(".//w:t", _WORD_NS):
        if text_node.text:
            values.append(text_node.text)
    return "".join(values).strip()


def _extract_pptx(content: bytes, members: set[str]) -> tuple[str, tuple[str, ...]]:
    presentation = _xml(content, "ppt/presentation.xml")
    rels_name = "ppt/_rels/presentation.xml.rels"
    rels = _xml(content, rels_name) if rels_name in members else None
    targets: dict[str, str] = {}
    if rels is not None:
        for rel in rels.findall("pr:Relationship", _PACKAGE_REL_NS):
            targets[rel.attrib.get("Id", "")] = rel.attrib.get("Target", "")
    slides: list[str] = []
    slide_ids = presentation.findall("p:sldIdLst/p:sldId", _PRESENTATION_NS)
    for index, slide_id in enumerate(slide_ids, start=1):
        rel_id = slide_id.attrib.get(f"{{{_REL_NS['r']}}}id", "")
        target = targets.get(rel_id, f"slides/slide{index}.xml")
        slide_path = _resolve_part("ppt/presentation.xml", target)
        if slide_path not in members:
            continue
        root = _xml(content, slide_path)
        texts = [node.text.strip() for node in root.iterfind(".//a:t", _DRAWING_NS) if node.text and node.text.strip()]
        if not texts:
            continue
        title, *body = texts
        section = [f"## Slide {index}: {title}"]
        section.extend(f"- {item}" for item in body)
        notes_path = f"ppt/notesSlides/notesSlide{index}.xml"
        if notes_path in members:
            notes = _xml(content, notes_path)
            note_text = [node.text.strip() for node in notes.iterfind(".//a:t", _DRAWING_NS) if node.text and node.text.strip()]
            if note_text:
                section.extend(["", "### Speaker notes", *note_text])
        slides.append("\n".join(section))
    warnings = []
    if any(name.startswith("ppt/media/") for name in members):
        warnings.append("Slide images were not interpreted; describe relevant diagrams or screenshots in chat.")
    if any("chart" in name.lower() for name in members):
        warnings.append("Chart semantics were not inferred; only visible text was extracted.")
    return _normalize_text("\n\n".join(slides)), tuple(warnings)


def _resolve_part(base: str, target: str) -> str:
    if target.startswith("/") or ".." in PurePosixPath(target).parts:
        raise IntakeError("Office relationship targets an unsafe path")
    parent = PurePosixPath(base).parent
    return str(parent / PurePosixPath(target))


def _markdown_table(rows: list[list[str]]) -> list[str]:
    if not rows:
        return []
    width = max(len(row) for row in rows)
    padded = [row + [""] * (width - len(row)) for row in rows]
    escaped = [[cell.replace("|", "\\|").replace("\n", " ") for cell in row] for row in padded]
    result = ["| " + " | ".join(escaped[0]) + " |", "| " + " | ".join(["---"] * width) + " |"]
    result.extend("| " + " | ".join(row) + " |" for row in escaped[1:])
    return result


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]
