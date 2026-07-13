# CSM AI Teammate — MCP server container image.
#
# Builds the combined MCP server (Snowflake NL-to-SQL + Gainsight CS/PX +
# knowledge base + content build + signals) for Azure Container Apps. The MCP
# server uses only the plain tool registry (TOOL_SPECS), so the Windows-only
# GitHub Copilot SDK is NOT required here.
#
# Base image is Microsoft Container Registry (no Docker Hub pull-rate limits
# during `az acr build`).
FROM mcr.microsoft.com/devcontainers/python:3.12

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    MCP_HOST=0.0.0.0 \
    MCP_PORT=8080

WORKDIR /app

# Install lean server dependencies first (better layer caching).
COPY requirements-server.txt ./
RUN pip install --upgrade pip && pip install -r requirements-server.txt

# Application code + fixtures (Gainsight simulation + SQLite/offline fallbacks).
COPY src ./src
COPY data ./data

EXPOSE 8080

# FastMCP streamable-HTTP server on 0.0.0.0:8080 at path /mcp.
CMD ["python", "-m", "src.mcp.server"]
