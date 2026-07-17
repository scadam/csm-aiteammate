#!/usr/bin/env python3
"""Bootstrap the plugin Spec Studio MCP server in an isolated persistent venv."""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
import traceback
from pathlib import Path


SCRIPT = Path(__file__).resolve()
SKILL_ROOT = SCRIPT.parents[1]
PLUGIN_ROOT = Path(os.environ.get("AI_TEAMMATE_PLUGIN_ROOT", SCRIPT.parents[4])).resolve()
DATA_ROOT = Path(
    os.environ.get(
        "AI_TEAMMATE_STUDIO_DATA",
        os.environ.get("CLAUDE_PLUGIN_DATA", Path.home() / ".copilot" / "ai-teammate-solution-builder"),
    )
).expanduser().resolve()
REQUIREMENTS = SKILL_ROOT / "requirements-studio.txt"
VENV = DATA_ROOT / "venv"
STAMP = DATA_ROOT / "requirements.sha256"


def _python() -> Path:
    return VENV / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def _prepare() -> Path:
    DATA_ROOT.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(REQUIREMENTS.read_bytes()).hexdigest()
    python = _python()
    installed = STAMP.read_text(encoding="utf-8").strip() if STAMP.is_file() else ""
    if not python.is_file():
        subprocess.run([sys.executable, "-m", "venv", str(VENV)], check=True)
    if installed != digest:
        subprocess.run(
            [
                str(python),
                "-m",
                "pip",
                "install",
                "--disable-pip-version-check",
                "--quiet",
                "-r",
                str(REQUIREMENTS),
            ],
            check=True,
            stdout=sys.stderr,
            stderr=sys.stderr,
        )
        STAMP.write_text(digest + "\n", encoding="utf-8")
    return python


def main() -> None:
    try:
        python = _prepare()
        environment = os.environ.copy()
        environment["PYTHONPATH"] = str(SKILL_ROOT)
        environment["CLAUDE_PLUGIN_ROOT"] = str(PLUGIN_ROOT)
        environment["AI_TEAMMATE_STUDIO_DATA"] = str(DATA_ROOT / "sessions")
        completed = subprocess.run(
            [str(python), "-m", "studio.server"],
            env=environment,
            stdin=sys.stdin.buffer,
            stdout=sys.stdout.buffer,
            stderr=sys.stderr.buffer,
            check=False,
        )
        raise SystemExit(completed.returncode)
    except Exception:
        DATA_ROOT.mkdir(parents=True, exist_ok=True)
        (DATA_ROOT / "bootstrap-error.log").write_text(traceback.format_exc(), encoding="utf-8")
        raise


if __name__ == "__main__":
    main()
