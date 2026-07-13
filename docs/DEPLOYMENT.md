# Deploying the CSM AI Teammate MCP server + BYO registration

This documents how the combined MCP server is hosted on **Azure Container Apps** and
registered as a **BYO (bring-your-own) MCP server** on the Agent 365 Tooling Gateway.

## What is deployed

`src/mcp/server.py` (FastMCP, streamable-HTTP, all 16 tools from `src/tools/TOOL_SPECS`)
runs as a container on Azure Container Apps. It reaches its **real** back ends from the
container using **managed identity** (no keys):

- **Azure OpenAI** (NL-to-SQL + constrained drafts) via the user-assigned managed identity
  (`AZURE_CLIENT_ID`) + `DefaultAzureCredential` — granted **Cognitive Services OpenAI User**.
- **Snowflake** (`CSM_DB.ADOPTION`, read-only role) via key-pair auth; the private key is a
  Container App secret.
- **Gainsight CS/PX** simulated-real REST served in-process from `data/*.json`.

Work IQ (Microsoft 365 grounding) is consumed by the **agent** on the manager's OBO token,
not by the standalone MCP server, so `WORKIQ__MCP__ENDPOINT` is left blank in the container
(the M365 tools use the offline fixture there).

## Live deployment (demo tenant `M365CPI81302533`)

| Resource | Value |
| --- | --- |
| Resource group | `csm-aiteammate-rg` (swedencentral) |
| Subscription | `35b71905-e334-430e-bee1-1575c104c487` |
| ACR | `csmmcpacrdkwnojsef6gzi.azurecr.io` |
| Image | `csm-mcp:v1` |
| Container App | `csmmcp-app` |
| MCP endpoint | `https://csmmcp-app.gentlecliff-c251c2e5.swedencentral.azurecontainerapps.io/mcp` |
| Managed identity (client/principal) | `703d7b02-21e9-4a29-8b92-bc21a272878d` / `c7216fa3-863a-4297-b036-d1def8582838` |

Verify the live endpoint (lists tools; `--call` exercises Snowflake + Gainsight):

```powershell
$env:MCP__PUBLIC_URL='https://csmmcp-app.gentlecliff-c251c2e5.swedencentral.azurecontainerapps.io/mcp'
python -m scripts.verify_remote_mcp --call
```

## Deploy from scratch

> Prereqs: `az login` (as the demo-tenant user), `.env` populated, `secrets/rsa_key.p8` present.

1. **Resource group + infra** (managed identity, ACR, Log Analytics, Container Apps env,
   placeholder app — no image yet):

   ```powershell
   az group create -n csm-aiteammate-rg -l swedencentral
   # Build a params file from .env (includes the Snowflake private key) and deploy:
   az deployment group create -g csm-aiteammate-rg -n main `
     --template-file infra/main.bicep --parameters '@tmp_params.json'
   ```

   The deployment outputs `acrName`, `managedIdentityPrincipalId`, `containerAppFqdn`.

2. **Grant the managed identity access to Azure OpenAI** (cross-RG — the OpenAI resource
   lives in `rg-svasireddy-1279`):

   ```powershell
   az role assignment create --assignee-object-id <managedIdentityPrincipalId> `
     --assignee-principal-type ServicePrincipal --role "Cognitive Services OpenAI User" `
     --scope /subscriptions/35b71905-e334-430e-bee1-1575c104c487/resourceGroups/rg-svasireddy-1279/providers/Microsoft.CognitiveServices/accounts/svasireddy-1279-resource
   ```

3. **Build the image remotely** (no local Docker required):

   ```powershell
   az acr build --registry <acrName> --image csm-mcp:v1 --file Dockerfile .
   ```

   > On Windows PowerShell the `az acr build` *log stream* can crash with a `cp1252`
   > `UnicodeEncodeError` (a colorama/azure-cli bug) — the **server-side build still
   > succeeds**. Check it with
   > `az acr task show-run --registry <acrName> --run-id <id> --query status`.

4. **Re-deploy with the image**:

   ```powershell
   az deployment group create -g csm-aiteammate-rg -n main-img `
     --template-file infra/main.bicep --parameters '@tmp_params.json' `
     --parameters containerImage=<acrLoginServer>/csm-mcp:v1
   ```

5. Confirm the revision is `Running` and `python -m scripts.verify_remote_mcp --call` passes.

## BYO MCP registration on the A365 Tooling Gateway

Registration creates three Entra proxy apps + one `<server> - BYO` app and **two Power
Platform custom connectors** (`shared_<server>`, `shared_<server>P`) in an A365-managed
Dataverse environment, then waits for an IT admin to approve the server in the Microsoft
365 admin center.

```powershell
a365 develop-mcp register-external-mcp-server `
  --server-name ext_CsmTeammate `
  --server-url "https://csmmcp-app.gentlecliff-c251c2e5.swedencentral.azurecontainerapps.io/mcp" `
  --auth-type EntraOAuth `
  --remote-scopes "api://752083a5-dacf-4df6-aa52-0305b288dcbb/access_agent_as_user" `
  --publisher "CSM Autopilot" `
  --description "Digital CSM teammate: Snowflake, Gainsight, KB, content build." `
  --tools "query_csm_database,get_schema,write_outcome,search_knowledge_base,get_account_context,create_review_task,send_email,trigger_in_product_message,get_engagement_history,gainsight_rest,build_draft,detect_signals,decide_next_best_action,search_microsoft_365,ask,list_agents"
```

The CLI then prompts interactively for a one-line description of each tool.

### Constraints learned the hard way

- The server **`--description` must be ≤ 80 characters** (the per-tool descriptions can be longer).
- `--server-name` must start with `ext_` and be ≤ 20 chars.
- The first registration in a tenant provisions an A365-managed Dataverse environment; if it
  reports the environment is *"not usable / ProvisioningState: Creating"*, wait and retry.
- **Connector creation (`shared_<server>` / `...P`) intermittently returns HTTP 400** right
  after the managed environment is provisioned and when many registrations are attempted in
  quick succession (throttling / eventual consistency). The CLI says *"Please retry"* — wait
  a few minutes, **clean up leftovers**, and run the same command again.

### Retrying after a failure (important)

A failed registration **does not roll back** the Entra apps it created, and a soft-deleted
Entra app *retains* its `identifierUri` — so a naive retry fails with *"Another object with
the same value for property identifierUris already exists"*. Leftover connectors likewise
make the next attempt 400. **Always clean up before retrying:**

```powershell
# Purges leftover ext_CsmTeammate Entra apps (soft-delete + purge) and deletes leftover
# ext_CsmTeammate* Power Platform connectors from the A365 MCC environment.
python -m scripts.cleanup_byo_registration
# then re-run the register-external-mcp-server command above.
```

### After approval

Once the IT admin approves the server in the M365 admin center, set the gateway endpoint in
`.env` (`A365__TOOLING_GATEWAY__MCP_ENDPOINT`) so the agent's reasoning loop consumes the
tools **through the gateway**, never the raw MCP endpoint.
