# Salesforce Customer Outreach Teammate - Starting Scenario

This example is a starting specification for an Agent 365 teammate that:

1. Reads an approved contact audience from a Salesforce Contact list view.
2. Builds a manager-configurable briefing about an event, product update, or other topic with a specific ask.
3. Routes the proposed campaign through deterministic human-review policy.
4. Sends individualized email through the assigned manager's Microsoft 365 mailbox.
5. Monitors replies and triggers one durable reply-processing workflow per message.
6. Idempotently creates or updates a Salesforce Lead for each qualified reply.
7. Reports campaign progress regularly and notifies the manager when a lead needs action.

The generated experience is hosted in a fresh tabs-only Microsoft Teams app:

- **Outreach Manager** shows AG-UI campaign progress and a generated HITL approval inbox with
   filtering, sortable columns, row actions, selection, and bulk approve/reject/defer.
- **Programme** shows a role-checked, read-only fleet view when the spec includes a fleet UI.
- The same manager dashboard and approval definitions are exposed by the governed MCP server as
   sandboxed MCP Apps when `surfaces` includes `mcp`.

The example intentionally separates identities:

- **Salesforce CRM access:** OAuth 2.0 client credentials using a Salesforce External Client App and assigned integration user.
- **Customer email and reply monitoring:** Work IQ MCP using the manager's delegated OBO token.
- **Autonomous orchestration:** the teammate's own Agent 365 agentic-user identity.
- **Azure OpenAI and shared state:** managed identity.
- **Teams control-plane ingress:** Teams ETS/SSO token for the signed-in manager, validated for
   signature, tenant, issuer, exact A365 blueprint audience, `access_agent_as_user`, and trusted
   Teams caller. This token is then the validated assertion for manager OBO where required.

Salesforce's client-credentials flow does not issue refresh tokens and does not accept scopes on the token request. Configure API scopes and object/field permissions on the External Client App and integration user instead. The token endpoint must use the org's **My Domain** URL, not `login.salesforce.com` or `test.salesforce.com`.

## Values To Collect From The User

### Agent 365 And Manager

- Microsoft Entra tenant ID.
- Manager Entra object ID and display name.
- Fleet/programme owner Entra object ID and roles.
- Azure subscription, resource group, region, and Azure OpenAI deployment.
- Whether every campaign requires review or only selected campaign/topic types.
- Escalation thresholds: positive intent, pricing request, meeting request, complaint, unsubscribe, or legal/compliance language.

### Salesforce

- My Domain base URL, for example `https://acme.my.salesforce.com`.
- Token URL: `<My Domain>/services/oauth2/token`.
- External Client App consumer key/client ID.
- External Client App consumer secret. Collect it through a secret prompt or deployment secret store; never put it in `solution.yaml`.
- Integration user assigned as **Run As** for client credentials.
- API version to pin. This example uses Salesforce REST API `v67.0` (Summer '26).
- Contact list view ID whose columns include at least `Id`, `FirstName`, `LastName`, `Email`, `Account.Name`, `HasOptedOutOfEmail`, and any personalization fields.
- Lead progress list view ID and the fields/statuses used in manager reports.
- Lead source and permitted Lead status values.
- Lead assignment rule behavior and owner/queue.
- A custom Lead field named `Agent_Reply_Key__c`, marked **External ID** and **Unique**, or an approved alternative duplicate-prevention key.
- Field-level permissions for all fields in the fixed OpenAPI contract.

Recommended minimum integration-user access:

- API Enabled.
- Read on Contact and Account plus required fields/list views.
- Create and Update on Lead plus required fields.
- Access to the configured Lead assignment behavior.
- No broad Salesforce administrator profile.

### Microsoft 365 Mail

- Work IQ enabled for the tenant and its delegated scope consented.
- The manager mailbox used to send and receive.
- A reply-correlation scheme: dedicated category, conversation ID, internet message ID, campaign tag, or plus-address.
- Approved sender signature, unsubscribe wording, suppression rules, and legal/compliance footer.
- Maximum recipients per batch and sending-rate/throttling policy.
- Reply polling interval or event/webhook source.

## Salesforce API Contract

The fixed companion OpenAPI document exposes only:

- `get_contact_list`: `GET /services/data/v67.0/sobjects/Contact/listviews/{list_view_id}/results`
- `upsert_reply_lead`: `PATCH /services/data/v67.0/sobjects/Lead/Agent_Reply_Key__c/{external_id}`
- `get_lead_progress`: `GET /services/data/v67.0/sobjects/Lead/listviews/{list_view_id}/results`

The generator wraps these selected operations as typed tools and exposes them through the governed FastMCP façade. It does **not** expose arbitrary SOQL, arbitrary sObject names, arbitrary URLs, or caller-provided authorization headers.

For a different Salesforce data model, update the local OpenAPI document and the matching capability schemas together. The generator validates method, path, parameters, body shape, auth mode, and exposure parity before emitting code.

## Required Generated Implementation

The starting spec is intentionally declarative. The AI Teammate Solution Builder must generate these scenario-specific runtime components before claiming completion:

1. **Campaign fan-out worker**
   - Takes the approved `send_campaign_batch` payload.
   - Extracts and validates Salesforce list-view records.
   - Removes missing email, bounced, opted-out, duplicate, and suppressed contacts.
   - Calls Work IQ `do_action /me/sendMail` once per recipient with the manager OBO token.
   - Uses durable per-recipient idempotency and bounded concurrency.
   - Persists sent/failed/skipped outcomes in shared state.

2. **Reply ingestion worker**
   - Uses Work IQ `fetch` against approved `/me/messages` paths or a governed event source.
   - Correlates replies to campaign/contact without trusting arbitrary message content as identifiers.
   - Starts `process_customer_reply` once per internet message ID.
   - Normalizes and validates the Lead payload before Salesforce upsert.

3. **Outreach delivery MCP server**
   - Implements the declared `send_campaign_batch` operation.
   - Accepts only Tooling Gateway validated delegated tokens.
   - Uses Work IQ for mail; it must not accept raw access tokens as tool parameters.
   - Is exposed only through Agent 365 Tooling Gateway in production.

4. **Progress scheduler**
   - Starts `report_campaign_progress` on the configured cadence.
   - Reads the approved Salesforce Lead list view.
   - Sends a manager summary and action-needed notifications.

5. **Compliance controls**
   - Review exact effect payloads, not a broad approval flag.
   - Preserve unsubscribe and suppression state.
   - Redact message bodies and CRM sensitive fields from telemetry unless explicitly allowlisted.
   - Record the authenticated actor, target manager, Agent ID, campaign, contact, and effect digest.

6. **Generated interactive experience**
    - Publish `show_outreach_progress` and `show_outreach_approvals` as linked MCP Apps resources.
    - Stream progress and review state over AG-UI at `/api/ag-ui`.
    - Keep `resolve_reviews` app-only so the model cannot approve its own proposed effect.
    - Validate every selected review's stored digest and full replacement edits on the server.
    - Host `/api/messages`, Teams tabs, APIs, and AG-UI on one public FQDN so the A365 CLI can
       configure the blueprint's domain-qualified identifier URI for Teams ETS.

## Start The Build

Select **AI Teammate Solution Builder** and ask it to use this Salesforce example as the starting
point. It will create a Spec Studio session, interview for consequential missing values, and show
the complete draft graph before doing any build or tenant work. Chat changes and graphical edits
update the same revisioned specification.

In the studio, review the identity boundaries, workflow, review policy, Salesforce operations,
Teams experience, and deployment assumptions. Select **Scaffold + A365** only when those are
correct, provide the tenant and output path, then type the exact displayed confirmation phrase.
The resulting one-shot grant authorizes the matching scaffold and early A365 setup; any change to
the draft or sidecars invalidates it.

A365 setup pauses only for a real tenant sign-in, Agent ID Developer role, Global Administrator
consent, or later public HTTPS endpoint/approval. Salesforce credentials are collected separately
and stored in the deployment secret store.

After deployment, register the shared Agent/control-plane endpoint before packaging Teams:

```powershell
python .\salesforce-outreach-teammate\scripts\provision_agent365.py endpoint `
   --url https://<shared-host>/api/messages
python .\salesforce-outreach-teammate\scripts\provision_agent365.py publish `
   --control-plane-url https://<shared-host>
Set-Location .\salesforce-outreach-teammate
atk provision --env dev -i false
atk publish --env dev -i false
```

Agents Toolkit creates only the fresh tabs-only Teams catalog app. The A365 CLI remains the sole
owner of the blueprint identity, SSO scope, preauthorized Teams clients, and permissions.

## Salesforce Setup Checklist

1. Create an External Client App in Salesforce.
2. Enable OAuth and the client-credentials flow.
3. Assign the least-privilege integration user as the client-credentials **Run As** user.
4. Grant the app/API scope needed for REST API use.
5. Grant the integration user only the Contact/Account read and Lead create/update fields required above.
6. Create the Contact and Lead list views and record their IDs.
7. Create and secure `Lead.Agent_Reply_Key__c` as a unique External ID, or revise the contract to the approved equivalent.
8. Rotate the consumer secret according to policy and immediately on suspected disclosure.
9. Put the secret in Key Vault or the deployment platform's secret store; never commit it.

## Assumptions To Review

- "Customer responds" means a reply to the manager/agent mailbox, detected through Work IQ or a governed mail event source.
- A reply becomes a Salesforce Lead only after the scenario's qualification rules; adjust if replies should instead create Tasks, Cases, CampaignMember statuses, or Opportunities.
- Existing Salesforce Contacts can legitimately generate Leads in this business process. Confirm duplicate-management and conversion policy with the Salesforce owner.
- Bulk commercial email, consent, suppression, and retention requirements have been approved by legal/compliance.
- Salesforce REST API `v67.0` is supported by the target org when deployed; pin and retest during Salesforce seasonal upgrades.
