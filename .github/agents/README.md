# AI Teammate Solution Builder

This installable plugin builds complete Agent 365 AI teammate projects from text, Markdown,
Word, PowerPoint, or an existing scenario specification. It combines:

- `.claude-plugin/plugin.json` and `.mcp.json` - direct Git installation and the local Spec Studio MCP server.
- `.github/agents/pattern-solution-builder.agent.md` - the user-selectable GitHub Copilot coding agent.
- `.github/skills/ai-teammate-solution-builder/` - bounded intake, graphical Spec Studio, v2 schema,
  generator, runtime templates, examples, A365 provisioning runner, and release verifier.

## Install From The Git URL

This is a **GitHub Copilot plugin**. Copilot deliberately uses the `.claude-plugin` compatibility
manifest format for cross-client plugins; it does not require or target the Claude desktop app.

On Windows, install the standalone GitHub Copilot CLI once (PowerShell 6+ and an active Copilot
subscription are required):

```powershell
winget install GitHub.Copilot
```

Open PowerShell in the new repository and start Copilot:

```powershell
Set-Location C:\path\to\new-repository
copilot
```

On first launch, use `/login` if prompted. At the Copilot prompt, install directly from GitHub:

```text
/plugin install https://github.com/scadam/csm-aiteammate.git
```

The repository also exposes a Copilot marketplace catalog. The equivalent two-step flow is:

```text
/plugin marketplace add https://github.com/scadam/csm-aiteammate.git
/plugin install ai-teammate-solution-builder@scadam-ai-teammates
```

Verify with `/plugin manage`, `/agents`, and `/mcp`. The agent identifier is
`ai-teammate-solution-builder:AI Teammate Solution Builder`; its
`ai-teammate-spec-studio` MCP server should report **Connected**. Select that agent or invoke the
`/ai-teammate-solution-builder` skill, then send the requirements prompt.

The first Spec Studio call creates an isolated Python environment under persistent Copilot plugin
data and installs the pinned dependencies. Python 3.10+ and package-network access are required on
that first run; no dependencies are installed into the target repository. In VS Code, run
**GitHub Copilot: Open in Copilot CLI** from the Command Palette to open the same CLI experience
for the current workspace. This VS Code build does not expose a separate "Install Plugin From
Source" command.

> The URL installs the latest committed revision from the repository. Local uncommitted changes
> are not available to another repository until they are committed and pushed.

### Windows smoke-test success criteria

1. `/plugin manage` lists `ai-teammate-solution-builder` as enabled.
2. `/agents` lists `ai-teammate-solution-builder:AI Teammate Solution Builder`.
3. `/mcp` lists `ai-teammate-spec-studio` as **Connected**.
4. The first prompt opens the Spec Studio with Architecture, Specification, Requirements, and
  History tabs. No files are generated yet.
5. A chat or graphical edit increments the revision and changes the digest.
6. Only the graphical app can confirm; after confirmation, any later edit invalidates the grant.
7. Choose **Scaffold only** for the first test so no tenant or Azure operation runs. Confirmed
  output should contain `app/agent.py`, `appPackage/manifest.json`, `infra/main.bicep`, and tests.

## Package The Customization

From this repository:

```powershell
python .github/skills/ai-teammate-solution-builder/scripts/package_agent.py `
  --output dist/ai-teammate-solution-builder.zip
```

The archive contains the plugin manifest, MCP configuration, reusable agent, and skill under their
expected paths. It excludes repository-specific CSM source, credentials, generated A365 identity
state, plugin session data, and build outputs.

## Install A Portable Archive

Extract the package at the root of the target repository:

```powershell
Expand-Archive dist/ai-teammate-solution-builder.zip C:\path\to\new-project -Force
```

Alternatively, install directly without creating an archive:

```powershell
python .github/skills/ai-teammate-solution-builder/scripts/package_agent.py `
  --install C:\path\to\new-project
```

Reload VS Code after installation and select **AI Teammate Solution Builder**.

## Start A New Solution

A useful first prompt can be plain text or point to a local `.txt`, `.md`, `.docx`, or `.pptx`:

```text
Build a new Agent 365 AI teammate from requirements.docx. Extract the requirements,
author a complete solution specification, and open the graphical Spec Studio for review.
Do not build or provision anything until I confirm the exact reviewed draft in the studio.
```

For a richer end-to-end demonstration, paste the bundled
[Autonomous Claims Digital Labor prompt](../skills/ai-teammate-solution-builder/references/demo-claims-digital-labor-prompt.md).
It models 40,000 claims per day and measures touchless intake, straight-through processing, manual
touches avoided, labor hours saved, review SLA, cost per claim, and outcome quality.

The bundled Salesforce example is at:

- `.github/skills/ai-teammate-solution-builder/assets/examples/salesforce-outreach/README.md`
- `.github/skills/ai-teammate-solution-builder/assets/examples/salesforce-outreach/solution.yaml`
- `.github/skills/ai-teammate-solution-builder/assets/examples/salesforce-outreach/openapi/salesforce-rest.yaml`

The plugin extracts the source into a local studio session, authors one schema-and-semantics-valid
draft, and projects that same draft into a graphical architecture, canonical YAML, requirements,
and inspector. Changes requested in chat or
made in the app are revision checked and invalidate prior confirmation. The user must select
**Scaffold only** or **Scaffold + A365**, choose the output and overwrite policy, and type the exact
`CONFIRM <digest-prefix>` phrase. Only then can the one-shot grant invoke the scaffolder. A
scaffold-only grant cannot be escalated to provisioning.

When A365 is explicitly confirmed, setup runs early. The agent continues through requirements,
blueprint creation, MCP permissions, and bot/observability permissions. It pauses only for a real
tenant sign-in, Agent ID Developer role, Global Administrator consent, BYO MCP approval, or a
post-deployment HTTPS endpoint.

Every generated solution also includes a fresh tabs-only Teams app under `appPackage/` and an
Agents Toolkit lifecycle in `m365agents.yml`. The tabs host generated AG-UI dashboards and HITL
queues; declared MCP tools can expose the same views as MCP Apps.

## Release Gate

Inside the source repository containing the customization:

```powershell
python .github/skills/ai-teammate-solution-builder/scripts/verify_scaffold.py --clean
```

For each generated project, also run:

```powershell
pytest -q
python scripts/provision_agent365.py verify
```

After deployment:

```powershell
python scripts/provision_agent365.py endpoint --url https://<shared-agent-control-plane-host>/api/messages
python scripts/provision_agent365.py publish --control-plane-url https://<shared-agent-control-plane-host>
atk provision --env dev -i false
atk publish --env dev -i false
python scripts/provision_agent365.py verify --instance
```

The endpoint and control-plane URL use the same public host. This lets the A365 CLI configure the
blueprint's domain-qualified identifier URI for Teams ETS. `atk` creates/publishes only the fresh
tabs-only catalog app; it does not create another Entra identity.

## Security Boundaries

- Never package `.env`, `.a365/`, `a365.generated.config*.json`, tokens, secrets, or tenant-generated manifests.
- Never call `studio_confirm` on the user's behalf or invoke `scaffold.py` without the exact
  single-use confirmation grant.
- Never use `a365 setup blueprint --show-secret` in agent automation.
- Manager OBO, agentic-user identity, and managed identity are distinct and cannot substitute for each other.
- A fleet role is read-only. A deployed Agent ID accepts mutations only from its assigned manager.
- Salesforce client secrets belong in a platform secret store or Key Vault reference, not `solution.yaml`.
- OpenAPI and MCP tools expose fixed typed operations; do not generate arbitrary URL, method, SOQL, or authorization-header tools.
