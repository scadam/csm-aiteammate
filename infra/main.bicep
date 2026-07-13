// ---------------------------------------------------------------------------
// CSM AI Teammate — MCP server on Azure Container Apps
//
// Deploys:
//   • User-assigned Managed Identity (AcrPull on the registry; the OpenAI data
//     role is granted separately by deploy because the OpenAI resource lives in
//     a different resource group)
//   • Azure Container Registry (admin disabled — managed-identity pull)
//   • Log Analytics workspace
//   • Container Apps Environment
//   • Container App running the combined MCP server (FastMCP streamable-HTTP)
//
// The MCP server reaches Azure OpenAI via the managed identity (AZURE_CLIENT_ID)
// and Snowflake via key-pair auth (private key passed as a Container App secret).
// ---------------------------------------------------------------------------

targetScope = 'resourceGroup'

@description('Azure region for all resources.')
param location string = resourceGroup().location

@description('Base name used to derive resource names (lowercase alphanumeric).')
param baseName string = 'csmmcp'

@description('Azure OpenAI /openai/v1/ endpoint.')
param azureOpenAiEndpoint string

@description('AAD scope for the Azure OpenAI bearer-token provider.')
param azureOpenAiScope string = 'https://cognitiveservices.azure.com/.default'

@description('Azure OpenAI deployment used for NL-to-SQL.')
param sqlDeployment string = 'gpt-4.1'

@description('Azure OpenAI deployment used for constrained drafts.')
param draftDeployment string = 'gpt-4.1'

@description('Tenant id to pin DefaultAzureCredential to (demo tenant).')
param azureTenantId string

@description('Snowflake account identifier.')
@secure()
param snowflakeAccount string

@description('Snowflake username.')
@secure()
param snowflakeUser string

@description('Snowflake RSA private key (PEM/PKCS8) for key-pair auth.')
@secure()
param snowflakePrivateKey string

@description('Snowflake database.')
param snowflakeDatabase string = 'CSM_DB'

@description('Snowflake schema.')
param snowflakeSchema string = 'ADOPTION'

@description('Snowflake warehouse.')
param snowflakeWarehouse string = 'GIM_WH'

@description('Snowflake (read-only runtime) role.')
param snowflakeRole string = 'GIM_AGENT_ROLE'

@description('Blueprint Entra app id (used by the BYO MCP remote scope).')
param blueprintAppId string = ''

@description('Container image. Leave blank for the initial infra-only deploy.')
param containerImage string = ''

// ── Derived names ─────────────────────────────────────────────────
var uniqueSuffix = uniqueString(resourceGroup().id, baseName)
var acrName = toLower(replace('${baseName}acr${uniqueSuffix}', '-', ''))
var logName = '${baseName}-logs-${uniqueSuffix}'
var envName = '${baseName}-env-${uniqueSuffix}'
var appName = '${baseName}-app'
var identityName = '${baseName}-id-${uniqueSuffix}'
var costStorageName = toLower('cost${uniqueSuffix}')
var hasImage = containerImage != ''

// ── User-assigned Managed Identity ────────────────────────────────
resource managedIdentity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: identityName
  location: location
}

// ── Azure Container Registry (managed-identity pull, no admin user) ─
resource acr 'Microsoft.ContainerRegistry/registries@2023-07-01' = {
  name: acrName
  location: location
  sku: { name: 'Basic' }
  properties: { adminUserEnabled: false }
}

resource acrPull 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(acr.id, managedIdentity.id, '7f951dda-4ed3-4680-a7ca-43fe172d538d')
  scope: acr
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '7f951dda-4ed3-4680-a7ca-43fe172d538d') // AcrPull
    principalId: managedIdentity.properties.principalId
    principalType: 'ServicePrincipal'
  }
}

// ── Durable cost ledger (Azure Table Storage; managed-identity only) ─
// Persists per-job inference cost points so the cost/token chart survives
// container recycles. Shared-key access is DISABLED (tenant policy) — the
// control-plane identity reads/writes the table over AAD via the data-plane
// "Storage Table Data Contributor" role below.
resource costStorage 'Microsoft.Storage/storageAccounts@2023-05-01' = {
  name: costStorageName
  location: location
  sku: { name: 'Standard_LRS' }
  kind: 'StorageV2'
  properties: {
    allowSharedKeyAccess: false
    minimumTlsVersion: 'TLS1_2'
    allowBlobPublicAccess: false
    publicNetworkAccess: 'Enabled'
  }
}

resource costTableService 'Microsoft.Storage/storageAccounts/tableServices@2023-05-01' = {
  parent: costStorage
  name: 'default'
}

resource costTable 'Microsoft.Storage/storageAccounts/tableServices/tables@2023-05-01' = {
  parent: costTableService
  name: 'costpoints'
}

resource costTableContributor 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(costStorage.id, managedIdentity.id, '0a9a7e1f-b9d0-4cc4-a60d-0319b160aaa3')
  scope: costStorage
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '0a9a7e1f-b9d0-4cc4-a60d-0319b160aaa3') // Storage Table Data Contributor
    principalId: managedIdentity.properties.principalId
    principalType: 'ServicePrincipal'
  }
}

// ── Log Analytics ─────────────────────────────────────────────────
resource logAnalytics 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: logName
  location: location
  properties: {
    sku: { name: 'PerGB2018' }
    retentionInDays: 30
  }
}

// ── Container Apps Environment ────────────────────────────────────
resource containerEnv 'Microsoft.App/managedEnvironments@2024-03-01' = {
  name: envName
  location: location
  properties: {
    appLogsConfiguration: {
      destination: 'log-analytics'
      logAnalyticsConfiguration: {
        customerId: logAnalytics.properties.customerId
        sharedKey: logAnalytics.listKeys().primarySharedKey
      }
    }
  }
}

// ── Container App ─────────────────────────────────────────────────
resource containerApp 'Microsoft.App/containerApps@2024-03-01' = {
  name: appName
  location: location
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${managedIdentity.id}': {}
    }
  }
  properties: {
    managedEnvironmentId: containerEnv.id
    configuration: {
      ingress: {
        external: true
        targetPort: 8080
        transport: 'http'
        allowInsecure: false
      }
      secrets: [
        { name: 'snowflake-account', value: snowflakeAccount }
        { name: 'snowflake-user', value: snowflakeUser }
        { name: 'snowflake-private-key', value: snowflakePrivateKey }
      ]
      registries: hasImage ? [
        {
          server: acr.properties.loginServer
          identity: managedIdentity.id
        }
      ] : []
    }
    template: {
      containers: [
        {
          name: 'csm-mcp'
          image: hasImage ? containerImage : 'mcr.microsoft.com/azuredocs/containerapps-helloworld:latest'
          resources: {
            cpu: json('0.5')
            memory: '1Gi'
          }
          env: [
            { name: 'MCP_HOST', value: '0.0.0.0' }
            { name: 'MCP_PORT', value: '8080' }
            { name: 'MCP__SERVER_NAME', value: 'ext_CsmTeammate' }
            { name: 'AZURE_OPENAI_ENDPOINT', value: azureOpenAiEndpoint }
            { name: 'AZURE_OPENAI_SCOPE', value: azureOpenAiScope }
            { name: 'AZURE_OPENAI_SQL_DEPLOYMENT', value: sqlDeployment }
            { name: 'AZURE_OPENAI_DRAFT_DEPLOYMENT', value: draftDeployment }
            { name: 'AZURE_CLIENT_ID', value: managedIdentity.properties.clientId }
            { name: 'AZURE_TENANT_ID', value: azureTenantId }
            { name: 'AGENT__IDENTITY__TENANT_ID', value: azureTenantId }
            { name: 'AGENT__IDENTITY__BLUEPRINT_ID', value: blueprintAppId }
            { name: 'SNOWFLAKE_ACCOUNT', secretRef: 'snowflake-account' }
            { name: 'SNOWFLAKE_USER', secretRef: 'snowflake-user' }
            { name: 'SNOWFLAKE_PRIVATE_KEY', secretRef: 'snowflake-private-key' }
            { name: 'SNOWFLAKE_DATABASE', value: snowflakeDatabase }
            { name: 'SNOWFLAKE_SCHEMA', value: snowflakeSchema }
            { name: 'SNOWFLAKE_WAREHOUSE', value: snowflakeWarehouse }
            { name: 'SNOWFLAKE_ROLE', value: snowflakeRole }
            // Work IQ is consumed by the agent (OBO), not the standalone MCP server;
            // leave the endpoint unset so M365 grounding tools use the offline fallback.
            { name: 'WORKIQ__MCP__ENDPOINT', value: '' }
            { name: 'GAINSIGHT__LIVE', value: 'false' }
            { name: 'ENABLE_A365_OBSERVABILITY', value: 'false' }
            { name: 'ENABLE_A365_OBSERVABILITY_EXPORTER', value: 'false' }
          ]
        }
      ]
      scale: {
        minReplicas: 1
        maxReplicas: 3
        rules: [
          {
            name: 'http-rule'
            http: { metadata: { concurrentRequests: '20' } }
          }
        ]
      }
    }
  }
}

// ── Outputs ───────────────────────────────────────────────────────
output acrLoginServer string = acr.properties.loginServer
output acrName string = acr.name
output containerAppFqdn string = containerApp.properties.configuration.ingress.fqdn
output mcpEndpoint string = 'https://${containerApp.properties.configuration.ingress.fqdn}/mcp'
output managedIdentityClientId string = managedIdentity.properties.clientId
output managedIdentityPrincipalId string = managedIdentity.properties.principalId
// Durable cost-ledger table endpoint — set as COST_STORE__TABLE_ENDPOINT on the
// control-plane container app so the cost/token chart survives recycles.
output costStoreTableEndpoint string = costStorage.properties.primaryEndpoints.table
