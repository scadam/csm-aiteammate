"""
Agent memory — the agent "learns" through a maintained memory file.

Each manager (the human the agent acts for) has a ``data/memory/<manager_id>.md``
that is:

* **always in context** — :func:`load` is injected into the reasoning system
  prompt on every turn, and surfaced in the cockpit, so the agent carries its
  accumulated experience into each decision;
* **maintained to a reasonable size** — capped at ``MEMORY_MAX_CHARS``; when a
  reflection would exceed the cap, the agent **condenses** the file (a real
  Azure OpenAI summarisation) so it stays compact and useful; and
* **updated by reflection** — after each job the agent runs a real Azure OpenAI
  reflection over what happened (signal, decision, outcome, whether it was
  accepted) and appends a concise learning under "What worked" / "What to avoid"
  / "Account notes". This is how the agent improves its own performance over
  time while remaining non-deterministic.

Memory is a Markdown document with stable sections so it reads naturally and can
be shown to a human.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from datetime import date

from . import config
from . import openai_client

logger = logging.getLogger(__name__)

_LOCK = threading.RLock()

_SECTIONS = ["Insights", "What worked", "What to avoid", "Account notes"]

_TEMPLATE = """# CSM Autopilot — Working Memory for {name}

_The agent maintains this file. It records what it has learned working {name}'s
book of business, so it improves over time. Kept concise on purpose._

## Insights
- (none yet)

## What worked
- (none yet)

## What to avoid
- (none yet)

## Account notes
- (none yet)
"""


def _path(manager_id: str):
    return config.MEMORY_DIR / f"{manager_id}.md"


def load(manager_id: str, manager_name: str = "") -> str:
    """Return the manager's memory markdown, creating it from the template if absent."""
    p = _path(manager_id)
    with _LOCK:
        if p.exists():
            try:
                return p.read_text(encoding="utf-8")
            except Exception as exc:  # pragma: no cover
                logger.warning("memory read failed: %s", exc)
                return ""
        config.MEMORY_DIR.mkdir(parents=True, exist_ok=True)
        text = _TEMPLATE.format(name=manager_name or manager_id)
        try:
            p.write_text(text, encoding="utf-8")
        except Exception as exc:  # pragma: no cover
            logger.warning("memory init failed: %s", exc)
        return text


def _write(manager_id: str, text: str) -> None:
    with _LOCK:
        config.MEMORY_DIR.mkdir(parents=True, exist_ok=True)
        _path(manager_id).write_text(text, encoding="utf-8")


def append_learning(manager_id: str, section: str, note: str, manager_name: str = "") -> str:
    """Append a one-line learning under a section (creates the file if needed)."""
    if section not in _SECTIONS:
        section = "Insights"
    note = note.strip().lstrip("-").strip()
    if not note:
        return load(manager_id, manager_name)
    text = load(manager_id, manager_name)
    lines = text.splitlines()
    out: list[str] = []
    bullet = f"- {note}  _({date.today().isoformat()})_"
    in_section = False
    inserted = False
    for line in lines:
        header = line.strip().startswith("## ")
        if in_section and "(none yet)" in line:
            continue  # drop the placeholder under our section
        if in_section and header and not inserted:
            out.append(bullet)   # add at the end of the section, before the next header
            inserted = True
            in_section = False
        out.append(line)
        if line.strip().lower() == f"## {section}".lower():
            in_section = True
    if in_section and not inserted:  # section was the last in the file
        out.append(bullet)
        inserted = True
    if not inserted:  # section header not found — append a fresh one
        out += ["", f"## {section}", bullet]
    text2 = "\n".join(out).rstrip() + "\n"
    _write(manager_id, text2)
    return text2


_REFLECT_SYSTEM = """You maintain an AI agent's working memory. Given a short
record of one job the agent just completed for a Customer Success Manager, write
ONE concise, durable lesson (max 25 words) the agent should remember to do better
next time. Focus on what worked or what to avoid for this kind of account/signal.
Reply with just the lesson text — no preamble, no quotes."""


def _reflect_sync(record: str) -> str:
    resp = openai_client.chat_completion(
        model=config.OPENAI_DRAFT_MODEL,
        messages=[{"role": "system", "content": _REFLECT_SYSTEM},
                  {"role": "user", "content": record}],
        temperature=0.3, max_tokens=80,
    )
    return (resp.choices[0].message.content or "").strip()


async def reflect_on_job(*, manager_id: str, manager_name: str, summary: str,
                         section: str = "What worked") -> str | None:
    """Run a real Azure OpenAI reflection over a finished job and store the lesson."""
    if not config.ENABLE_MEMORY_REFLECTION:
        return None
    try:
        lesson = await asyncio.to_thread(_reflect_sync, summary)
    except Exception as exc:  # pragma: no cover - depends on live Azure OpenAI
        logger.info("memory reflection skipped: %s", exc)
        return None
    if not lesson:
        return None
    append_learning(manager_id, section, lesson, manager_name)
    _maybe_condense(manager_id, manager_name)
    return lesson


def _maybe_condense(manager_id: str, manager_name: str) -> None:
    """Condense the memory file when it grows past the soft cap (real summarisation)."""
    text = load(manager_id, manager_name)
    if len(text) <= config.MEMORY_MAX_CHARS:
        return
    try:
        resp = openai_client.chat_completion(
            model=config.OPENAI_DRAFT_MODEL,
            messages=[
                {"role": "system", "content": (
                    "Condense this AI agent working-memory Markdown. Keep the four sections "
                    "(Insights, What worked, What to avoid, Account notes), keep the most "
                    "useful, durable, distinct bullets, merge duplicates, drop the stalest. "
                    "Stay well under " + str(config.MEMORY_MAX_CHARS) + " characters. Return only Markdown.")},
                {"role": "user", "content": text},
            ],
            temperature=0.2, max_tokens=1200,
        )
        condensed = (resp.choices[0].message.content or "").strip()
        if condensed and "##" in condensed:
            _write(manager_id, condensed)
    except Exception as exc:  # pragma: no cover
        logger.info("memory condense skipped: %s", exc)


def stats(manager_id: str) -> dict:
    text = load(manager_id)
    bullets = sum(1 for ln in text.splitlines() if ln.strip().startswith("- ") and "(none yet)" not in ln)
    return {"chars": len(text), "bullets": bullets, "cap": config.MEMORY_MAX_CHARS,
            "path": f"data/memory/{manager_id}.md"}
