[build-system]
requires = ["setuptools>=75", "wheel"]
build-backend = "setuptools.build_meta"

[project]
name = "__PROJECT_ID__"
version = "0.1.0"
description = "__PROJECT_DESCRIPTION__"
requires-python = ">=3.11"
dependencies = [
  "ag-ui-protocol==0.1.19",
  "aiohttp>=3.11,<4",
  "azure-identity>=1.25,<2",
  "azure-data-tables>=12.7,<13",
  "fastapi>=0.115,<1",
  "httpx>=0.28,<1",
  "mcp==1.24.0",
  "microsoft-agents-activity==1.1.0",
  "microsoft-agents-authentication-msal==1.1.0",
  "microsoft-agents-hosting-aiohttp==1.1.0",
  "microsoft-agents-hosting-core==1.1.0",
  "microsoft-agents-a365-observability-core==0.1.0",
  "microsoft-agents-a365-runtime==0.1.0",
  "openai>=2.11,<3",
  "opentelemetry-api>=1.36,<2",
  "opentelemetry-sdk>=1.36,<2",
  "pydantic>=2.10,<3",
  "PyJWT[crypto]>=2.10,<3",
  "python-dotenv>=1.1,<2",
  "PyYAML>=6,<7",
  "uvicorn[standard]>=0.34,<1",
  "wrapt>=1.17,<3",
]

[project.optional-dependencies]
dev = [
  "pytest>=8,<9",
  "pytest-asyncio>=0.25,<1",
]
copilot = ["github-copilot-sdk==1.0.0"]

[tool.setuptools.packages.find]
include = ["app*"]
exclude = ["tests*"]

[tool.setuptools.package-data]
app = ["solution.yaml", "generated_skills/*/SKILL.md"]

[tool.pytest.ini_options]
pythonpath = ["."]
testpaths = ["tests"]
filterwarnings = [
  "ignore:It is recommended to use web.AppKey instances for keys.:aiohttp.web_exceptions.NotAppKeyWarning",
]
