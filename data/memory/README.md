# Agent working memory

This folder holds each manager's `**<manager_id>.md**` working-memory file — the
agent's accumulated, self-maintained learnings. Files are created at runtime by
`src/memory.py` (one per CSM) and are always injected into the agent's context so
it improves over time. They are intentionally kept small (condensed when over the
cap). This file just keeps the folder in version control.
