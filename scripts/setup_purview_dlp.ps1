<#
.SYNOPSIS
  Configure the REAL Microsoft Purview DLP-for-AI policy that the CSM AI Teammate
  honours, scoped to this agent's own Entra application.

.DESCRIPTION
  The CSM AI Teammate is a Microsoft Entra-REGISTERED AI app. For that app class,
  Microsoft Purview DLP support today is: "block prompts based on sensitive
  information types", configured with a DLP policy scoped to the specific Entra app
  and honoured by the app's Microsoft Purview `processContent` integration
  (which this agent implements in src/purview.py).
  Ref: https://learn.microsoft.com/purview/ai-entra-registered  (Data loss prevention)
       https://learn.microsoft.com/powershell/module/exchange/new-dlpcompliancerule  (Example 4)

  This script (idempotently):
    1. Creates a custom sensitive information type "Customer Confidential ID"
       (keyword CUSTOMER-CONFIDENTIAL + account-id pattern ACC-####) so the platform DLP
       rule looks for the SAME confidential identifiers as the agent's cross-customer
       data fence (src/scenarios.py).
    2. Creates a DLP policy SCOPED TO THIS AGENT'S ENTRA APP ONLY (blast radius = the
       agent; no other workload is affected).
    3. Adds two rules that return RestrictAccess=Block on UploadText (the prompt):
         - customer-confidential identifiers (the custom SIT), and
         - high-impact built-in SITs (Credit Card Number, U.S. SSN).
       The agent's processContent integration receives the block action and stops the
       prompt — visible live on the Technical & governance dashboard.

  Re-running is safe: existing objects are detected and left in place.

.PREREQUISITES
  - PowerShell 7+ with the ExchangeOnlineManagement module:
        Install-Module ExchangeOnlineManagement -Scope CurrentUser
  - A role that can author Copilot/AI DLP policies (Global Admin, Compliance
    Administrator, or Purview Data Security AI Admin).
  - Pay-as-you-go billing enabled for AI interactions in the tenant.

.EXAMPLE
  ./scripts/setup_purview_dlp.ps1 -Upn admin@contoso.onmicrosoft.com `
      -AppId 61656391-5ec7-44d9-a3fa-9d4299ddb164

.NOTES
  Remove everything again with: ./scripts/setup_purview_dlp.ps1 -Remove
#>
[CmdletBinding()]
param(
  # Sign-in UPN for Security & Compliance PowerShell (interactive / device-code auth).
  [string]$Upn = "",
  # The agent's Entra application id (must equal PURVIEW__APP_LOCATION_ID / the blueprint app id).
  [string]$AppId = "61656391-5ec7-44d9-a3fa-9d4299ddb164",
  [string]$AppName = "CSM Autopilot",
  [string]$PolicyName = "CSM Autopilot AI DLP",
  [string]$SitName = "Customer Confidential ID",
  # Alert recipient for DLP incidents.
  [string]$AlertEmail = "",
  # Tear down the policy + custom SIT created by this script.
  [switch]$Remove
)

$ErrorActionPreference = "Stop"

function Connect-Scc {
  if (Get-Command Get-DlpCompliancePolicy -ErrorAction SilentlyContinue) { return }
  Import-Module ExchangeOnlineManagement -ErrorAction Stop
  Write-Host "Connecting to Security & Compliance PowerShell..." -ForegroundColor Cyan
  if ($Upn) { Connect-IPPSSession -UserPrincipalName $Upn | Out-Null }
  else      { Connect-IPPSSession | Out-Null }
}

Connect-Scc

# ── teardown ────────────────────────────────────────────────────────
if ($Remove) {
  if (Get-DlpCompliancePolicy -Identity $PolicyName -ErrorAction SilentlyContinue) {
    Remove-DlpCompliancePolicy -Identity $PolicyName -Confirm:$false
    Write-Host "Removed DLP policy '$PolicyName'." -ForegroundColor Yellow
  }
  if (Get-DlpSensitiveInformationTypeRulePackage -ErrorAction SilentlyContinue |
        Where-Object { $_.Name -eq "CSM Autopilot Rule Pack" }) {
    Remove-DlpSensitiveInformationTypeRulePackage -Identity "CSM Autopilot Rule Pack" -Confirm:$false
    Write-Host "Removed custom SIT rule package." -ForegroundColor Yellow
  }
  Write-Host "Teardown complete." -ForegroundColor Green
  return
}

# ── 1. custom sensitive information type ────────────────────────────
# Fixed GUIDs keep the rule package idempotent across re-runs.
$rulePackId = "7b1f6a2e-3c4d-4e5f-9a0b-1c2d3e4f5a60"
$publisherId = "9c2e7b3f-4d5e-4f60-8b1c-2d3e4f5a6b71"
$entityId    = "a3d8c1e4-5f60-4718-9b2c-3d4e5f6a7b82"

$existingSit = Get-DlpSensitiveInformationType -ErrorAction SilentlyContinue |
  Where-Object { $_.Name -eq $SitName }

if (-not $existingSit) {
  Write-Host "Creating custom sensitive information type '$SitName'..." -ForegroundColor Cyan
  # Boost.RegEx — NO ^ or $ anchors (per Microsoft guidance for custom SITs).
  $rulePackXml = @"
<?xml version="1.0" encoding="utf-16"?>
<RulePackage xmlns="http://schemas.microsoft.com/office/2011/mce">
  <RulePack id="$rulePackId">
    <Version major="1" minor="0" build="0" revision="0" />
    <Publisher id="$publisherId" />
    <Details defaultLangCode="en-us">
      <LocalizedDetails langcode="en-us">
        <PublisherName>CSM Autopilot</PublisherName>
        <Name>CSM Autopilot Rule Pack</Name>
        <Description>Custom sensitive information types for the CSM AI Teammate.</Description>
      </LocalizedDetails>
    </Details>
  </RulePack>
  <Rules>
    <Entity id="$entityId" patternsProximity="300" recommendedConfidence="85">
      <Pattern confidenceLevel="85">
        <IdMatch idRef="Keyword_customer_confidential" />
      </Pattern>
      <Pattern confidenceLevel="75">
        <IdMatch idRef="Regex_account_id" />
      </Pattern>
    </Entity>
    <Keyword id="Keyword_customer_confidential">
      <Group matchStyle="word">
        <Term caseSensitive="false">CUSTOMER-CONFIDENTIAL</Term>
        <Term caseSensitive="false">Customer Confidential</Term>
      </Group>
    </Keyword>
    <Regex id="Regex_account_id">\bACC-\d{4}\b</Regex>
    <LocalizedStrings>
      <Resource idRef="$entityId">
        <Name default="true" langcode="en-us">$SitName</Name>
        <Description default="true" langcode="en-us">A customer confidential identifier (account id or CUSTOMER-CONFIDENTIAL marker).</Description>
      </Resource>
    </LocalizedStrings>
  </Rules>
</RulePackage>
"@
  $bytes = [System.Text.Encoding]::Unicode.GetBytes($rulePackXml)
  New-DlpSensitiveInformationTypeRulePackage -FileData $bytes | Out-Null
  Write-Host "  Custom SIT created." -ForegroundColor Green
} else {
  Write-Host "Custom SIT '$SitName' already exists - leaving in place." -ForegroundColor DarkGray
}

# ── 2. DLP policy scoped to THIS agent's Entra app only ─────────────
# Verified Example 4 location JSON: Workload=Applications, LocationSource=Entra.
$locations = "[{`"Workload`":`"Applications`",`"Location`":`"$AppId`",`"LocationDisplayName`":`"$AppName`",`"LocationSource`":`"Entra`",`"LocationType`":`"Individual`",`"Inclusions`":[{`"Type`":`"Tenant`",`"Identity`":`"All`"}]}]"

if (-not (Get-DlpCompliancePolicy -Identity $PolicyName -ErrorAction SilentlyContinue)) {
  Write-Host "Creating DLP policy '$PolicyName' scoped to Entra app $AppId..." -ForegroundColor Cyan
  New-DlpCompliancePolicy -Name $PolicyName -Mode Enable -Locations $locations -EnforcementPlanes @("Application") | Out-Null
  Write-Host "  Policy created (Mode=Enable, EnforcementPlanes=Application)." -ForegroundColor Green
} else {
  Write-Host "DLP policy '$PolicyName' already exists - leaving in place." -ForegroundColor DarkGray
}

# ── 3. rules: block the prompt (UploadText) on confidential / PII SITs ─
$alertArgs = @{}
if ($AlertEmail) { $alertArgs = @{ GenerateAlert = $true; NotifyUser = @($AlertEmail) } }

$rule1 = "Block customer-confidential identifiers in prompts"
if (-not (Get-DlpComplianceRule -Policy $PolicyName -ErrorAction SilentlyContinue | Where-Object { $_.Name -eq $rule1 })) {
  Write-Host "Adding rule: $rule1" -ForegroundColor Cyan
  New-DlpComplianceRule -Name $rule1 -Policy $PolicyName `
    -ContentContainsSensitiveInformation @{ Name = $SitName } `
    -RestrictAccess @(@{ setting = "UploadText"; value = "Block" }) `
    -ReportSeverityLevel High @alertArgs | Out-Null
  Write-Host "  Rule added." -ForegroundColor Green
} else { Write-Host "Rule '$rule1' already exists." -ForegroundColor DarkGray }

$rule2 = "Block payment/PII sensitive information in prompts"
if (-not (Get-DlpComplianceRule -Policy $PolicyName -ErrorAction SilentlyContinue | Where-Object { $_.Name -eq $rule2 })) {
  Write-Host "Adding rule: $rule2" -ForegroundColor Cyan
  New-DlpComplianceRule -Name $rule2 -Policy $PolicyName `
    -ContentContainsSensitiveInformation @(@{ Name = "Credit Card Number" }, @{ Name = "U.S. Social Security Number (SSN)" }) `
    -RestrictAccess @(@{ setting = "UploadText"; value = "Block" }) `
    -ReportSeverityLevel High @alertArgs | Out-Null
  Write-Host "  Rule added." -ForegroundColor Green
} else { Write-Host "Rule '$rule2' already exists." -ForegroundColor DarkGray }

Write-Host ""
Write-Host "Done. DLP-for-AI policy '$PolicyName' is live for Entra app $AppId." -ForegroundColor Green
Write-Host "Set PURVIEW__DLP_POLICY='$PolicyName' in the control plane so the dashboard shows it as configured." -ForegroundColor Green
Write-Host "Allow a short propagation window, then trigger a journey whose draft prompt contains a blocked SIT to see processContent return RestrictAccess=Block." -ForegroundColor DarkGray
