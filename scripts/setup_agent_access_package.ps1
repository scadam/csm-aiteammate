<#
.SYNOPSIS
  Create (or remove) the REAL Microsoft Entra ID Governance access package that
  governs the CSM Autopilot agents' Agent 365 user accounts.

.DESCRIPTION
  Honest, no-fakery Agent 365 governance. This creates real Microsoft Entra objects
  with Microsoft Graph (entitlement management) and assigns the three CSM agents to a
  time-bound, sponsor-governed access package:

    1. Security group  sg-CSM-Autopilot-Agents
    2. Catalog         "CSM Autopilot"
    3. Catalog resource = the security group (Member role)
    4. Access package  "CSM Autopilot - Microsoft 365 Grounding and Governance"
       with the group Member role as its resource role
    5. Assignment policy: scoped to the three agents, approval by the programme
       owner, 90-day expiry (the governance lifecycle)
    6. Direct (adminAdd) assignment of the three agents' Agent 365 user accounts,
       each time-bound to 90 days -> they really become members of the group.

  Every step is idempotent (re-running is safe) and verified by reading back. The
  control plane reads this package + its live assignments from Graph and shows them
  on the Technical & governance tab (src/agent_instances.py:access_package_status).

.PREREQUISITES
  - Microsoft.Graph.Authentication PowerShell module (ships Invoke-MgGraphRequest).
  - Connect first with the required scopes (interactive, as a Global Administrator
    or Identity Governance Administrator):
        Connect-MgGraph -Scopes "EntitlementManagement.ReadWrite.All","Group.ReadWrite.All","Directory.Read.All"
  - Microsoft Entra ID P1 (ID Governance for agents) or Microsoft 365 E5.

.EXAMPLE
  ./scripts/setup_agent_access_package.ps1

.EXAMPLE
  ./scripts/setup_agent_access_package.ps1 -Remove
#>
[CmdletBinding()]
param(
  [string]$GroupName   = "sg-CSM-Autopilot-Agents",
  [string]$CatalogName = "CSM Autopilot",
  [string]$PackageName = "CSM Autopilot - Microsoft 365 Grounding and Governance",
  [string]$PolicyName  = "CSM Autopilot agents - sponsor approved, 90 days",
  # Display-name search term that matches the three agents' Agent 365 user accounts.
  [string]$AgentUserMatch = "CSM Autopilot Agent",
  # Programme owner (approver / sponsor). Defaults to Siva Vasireddy's Entra object id.
  [string]$ApproverObjectId = "44f6b89e-de3e-4afe-a77a-d75fdcd2785c",
  [int]$ExpiryDays = 90,
  [switch]$Remove
)

$ErrorActionPreference = "Stop"
$base = "https://graph.microsoft.com/v1.0/identityGovernance/entitlementManagement"

function Need-Graph {
  if (-not (Get-Command Invoke-MgGraphRequest -ErrorAction SilentlyContinue)) {
    throw "Invoke-MgGraphRequest not found. Install-Module Microsoft.Graph.Authentication and Connect-MgGraph first."
  }
  $ctx = Get-MgContext -ErrorAction SilentlyContinue
  if (-not $ctx) { throw "Not connected. Run: Connect-MgGraph -Scopes 'EntitlementManagement.ReadWrite.All','Group.ReadWrite.All','Directory.Read.All'" }
  Write-Host ("Connected as " + $ctx.Account) -ForegroundColor Cyan
}
function MgGet($u){ Invoke-MgGraphRequest -Method GET -Uri $u -Headers @{ ConsistencyLevel = 'eventual' } }
function MgPost($u,$b){ Invoke-MgGraphRequest -Method POST -Uri $u -Body $b }
function MgDelete($u){ Invoke-MgGraphRequest -Method DELETE -Uri $u }

Need-Graph

# Resolve the three agents' Agent 365 user accounts.
$agentUsers = (MgGet ('https://graph.microsoft.com/v1.0/users?$search="displayName:' + $AgentUserMatch + '"&$select=id,displayName,userPrincipalName')).value
if (-not $agentUsers -or $agentUsers.Count -eq 0) { throw "No agent user accounts found matching '$AgentUserMatch'." }
Write-Host ("Found " + $agentUsers.Count + " agent user account(s):") -ForegroundColor Cyan
$agentUsers | ForEach-Object { Write-Host ("  - " + $_.displayName + " (" + $_.userPrincipalName + ")") }

# ── teardown ────────────────────────────────────────────────────────
if ($Remove) {
  $ap = (MgGet ("$base/accessPackages?`$filter=displayName eq '$PackageName'")).value
  if ($ap) {
    $apId = $ap[0].id
    # remove assignments first
    $asgs = (MgGet ("$base/assignments?`$filter=accessPackage/id eq '$apId'&`$expand=target")).value
    foreach ($a in $asgs) {
      try { MgPost "$base/assignmentRequests" @{ requestType='adminRemove'; assignment=@{ id=$a.id } } | Out-Null; Write-Host ("  removing assignment for " + $a.target.displayName) } catch {}
    }
    Start-Sleep -Seconds 5
    try { MgDelete "$base/accessPackages/$apId"; Write-Host "Removed access package." -ForegroundColor Yellow } catch { Write-Host ("AP delete deferred (assignments still draining): " + $_.Exception.Message) }
  }
  $cat = (MgGet ("$base/catalogs?`$filter=displayName eq '$CatalogName'")).value
  if ($cat) { try { MgDelete ("$base/catalogs/" + $cat[0].id); Write-Host "Removed catalog." -ForegroundColor Yellow } catch { Write-Host ("Catalog delete deferred: " + $_.Exception.Message) } }
  $g = (MgGet ("https://graph.microsoft.com/v1.0/groups?`$filter=displayName eq '$GroupName'")).value
  if ($g) { try { MgDelete ("https://graph.microsoft.com/v1.0/groups/" + $g[0].id); Write-Host "Removed security group." -ForegroundColor Yellow } catch {} }
  Write-Host "Teardown complete (some deletes may finish asynchronously)." -ForegroundColor Green
  return
}

# ── 1. security group ───────────────────────────────────────────────
$g = (MgGet ("https://graph.microsoft.com/v1.0/groups?`$filter=displayName eq '$GroupName'")).value
if (-not $g) {
  $g = ,(MgPost 'https://graph.microsoft.com/v1.0/groups' @{ displayName=$GroupName; description='Governed membership for CSM Autopilot agents (assigned via access package).'; mailEnabled=$false; mailNickname=$GroupName; securityEnabled=$true })
  Write-Host "Created security group." -ForegroundColor Green
}
$groupId = $g[0].id; Write-Host ("group: $GroupName -> $groupId")

# ── 2. catalog ──────────────────────────────────────────────────────
$cat = (MgGet ("$base/catalogs?`$filter=displayName eq '$CatalogName'")).value
if (-not $cat) {
  $cat = ,(MgPost "$base/catalogs" @{ displayName=$CatalogName; description='Catalog for governing CSM Autopilot agents.'; state='published'; isExternallyVisible=$false })
  Write-Host "Created catalog." -ForegroundColor Green
}
$catalogId = $cat[0].id; Write-Host ("catalog: $CatalogName -> $catalogId")

# ── 3. add group to catalog as a resource (async) ───────────────────
$res = (MgGet ("$base/catalogs/$catalogId/resources?`$filter=originId eq '$groupId'&`$expand=scopes")).value
if (-not $res) {
  MgPost "$base/resourceRequests" @{ requestType='adminAdd'; resource=@{ originId=$groupId; originSystem='AadGroup'; displayName=$GroupName }; catalog=@{ id=$catalogId } } | Out-Null
  for ($i=0; $i -lt 18 -and -not $res; $i++) { Start-Sleep -Seconds 5; $res = (MgGet ("$base/catalogs/$catalogId/resources?`$filter=originId eq '$groupId'&`$expand=scopes")).value }
}
if (-not $res) { throw "Group resource did not appear in the catalog (timed out)." }
$resourceId = $res[0].id
$memberRoleOriginId = "Member_$groupId"
Write-Host ("resource: $GroupName -> $resourceId (role $memberRoleOriginId)")

# ── 4. access package ───────────────────────────────────────────────
$ap = (MgGet ("$base/accessPackages?`$filter=displayName eq '$PackageName'")).value
if (-not $ap) {
  $ap = ,(MgPost "$base/accessPackages" @{ displayName=$PackageName; description='Time-bound, sponsor-governed access for the CSM Autopilot agents. Grants membership of the sg-CSM-Autopilot-Agents security group.'; catalogId=$catalogId })
  Write-Host "Created access package." -ForegroundColor Green
}
$apId = $ap[0].id; Write-Host ("accessPackage: $PackageName -> $apId")

# ── 5. resource role scope (group Member role) ──────────────────────
$scopes = (MgGet ("$base/accessPackages/$apId/resourceRoleScopes?`$expand=role,scope")).value
if (-not $scopes) {
  MgPost "$base/accessPackages/$apId/resourceRoleScopes" @{
    role  = @{ originId=$memberRoleOriginId; displayName='Member'; originSystem='AadGroup'; resource=@{ id=$resourceId; originId=$groupId; originSystem='AadGroup' } }
    scope = @{ originId=$groupId; originSystem='AadGroup'; isRootScope=$true }
  } | Out-Null
  Write-Host "Attached group Member role to the access package." -ForegroundColor Green
}

# ── 6. assignment policy (sponsor approval + 90-day expiry) ─────────
$pol = (MgGet ("$base/assignmentPolicies?`$filter=displayName eq '$PolicyName'")).value
if (-not $pol) {
  $targets = @($agentUsers | ForEach-Object { @{ '@odata.type'='#microsoft.graph.singleUser'; userId=$_.id } })
  $body = @{
    displayName            = $PolicyName
    description            = 'Agents (or their sponsor) request access; the programme owner approves; access expires after 90 days unless extended.'
    allowedTargetScope     = 'specificDirectoryUsers'
    specificAllowedTargets = $targets
    expiration             = @{ type='afterDuration'; duration="P$($ExpiryDays)D" }
    requestApprovalSettings = @{
      isApprovalRequiredForAdd = $true
      approvalMode             = 'SingleStage'
      approvalStages = @(@{
        approvalStageTimeOutInDays = 14
        isApproverJustificationRequired = $true
        primaryApprovers = @(@{ '@odata.type'='#microsoft.graph.singleUser'; userId=$ApproverObjectId })
      })
    }
    accessPackage = @{ id = $apId }
  }
  MgPost "$base/assignmentPolicies" $body | Out-Null
  Write-Host "Created assignment policy (sponsor approval, $ExpiryDays-day expiry)." -ForegroundColor Green
}
$pol = (MgGet ("$base/assignmentPolicies?`$filter=displayName eq '$PolicyName'")).value
$policyId = $pol[0].id; Write-Host ("policy: $PolicyName -> $policyId")

# ── 7. seed direct (adminAdd) assignments, time-bound to 90 days ────
$existing = (MgGet ("$base/assignments?`$filter=accessPackage/id eq '$apId'&`$expand=target")).value
$haveIds  = @($existing | ForEach-Object { $_.target.objectId })
$end = (Get-Date).ToUniversalTime().AddDays($ExpiryDays).ToString('yyyy-MM-ddTHH:mm:ssZ')
foreach ($u in $agentUsers) {
  if ($haveIds -contains $u.id) { Write-Host ("  assignment already present: " + $u.displayName); continue }
  $req = @{
    requestType = 'adminAdd'
    assignment  = @{ targetId=$u.id; assignmentPolicyId=$policyId; accessPackageId=$apId }
    schedule    = @{ expiration = @{ endDateTime=$end; type='afterDateTime' } }
  }
  try { MgPost "$base/assignmentRequests" $req | Out-Null; Write-Host ("  assigned (90d): " + $u.displayName) -ForegroundColor Green }
  catch { Write-Host ("  assignment ERROR for " + $u.displayName + ": " + $_.Exception.Message) -ForegroundColor Red }
}

Start-Sleep -Seconds 6
Write-Host "`n=== Final state ===" -ForegroundColor Cyan
(MgGet ("$base/assignments?`$filter=accessPackage/id eq '$apId'&`$expand=target")).value | ForEach-Object {
  Write-Host ("  " + $_.target.displayName + " | state=" + $_.state + " | expires=" + $_.schedule.expiration.endDateTime)
}
Write-Host ("`nDone. Access package '$PackageName' governs " + $agentUsers.Count + " agent(s).") -ForegroundColor Green
Write-Host "Set A365__ACCESS_PACKAGE__NAME and A365__ACCESS_PACKAGE__GROUP in the control plane to surface it live on the Technical tab." -ForegroundColor DarkGray
