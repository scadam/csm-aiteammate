targetScope = 'resourceGroup'

@description('Azure region for the Container Apps environment.')
param location string = resourceGroup().location

@description('Lowercase base name used for generated resources.')
@minLength(3)
param baseName string = '__PROJECT_ID__'

@description('Container image for the Microsoft 365 Agents SDK host.')
param agentImage string

@description('Container image for the FastAPI control plane.')
param controlPlaneImage string

@description('Container image for the governed FastMCP facade.')
param mcpImage string

@description('Microsoft Entra tenant ID provisioned by the A365 CLI.')
param tenantId string

@description('Agent Identity Blueprint application/client ID provisioned by the A365 CLI.')
param blueprintClientId string

@description('Per-instance Agent Identity application/client ID.')
param agentInstanceAppId string

@description('Per-instance agentic-user object ID.')
param agenticUserId string

@description('Manager ID from solution.yaml assigned to this agent instance.')
param managerId string

@description('Azure OpenAI /openai/v1 endpoint consumed with managed identity.')
param azureOpenAiEndpoint string

@description('Azure OpenAI deployment used by the agent reasoning loop.')
param azureOpenAiDeployment string = 'gpt-4.1'

@description('Tooling Gateway MCP endpoint after BYO registration and approval.')
param toolingGatewayMcpEndpoint string

@description('Tooling Gateway BYO registration ID.')
param toolingGatewayRegistrationId string

@description('MCP token audience, usually api://<blueprint-app-id>.')
param mcpTokenAudience string

@description('MCP remote scope value, usually access_agent_as_user.')
param mcpRequiredScope string = 'access_agent_as_user'

@description('Azure Bot OAuth connection name created/configured for manager sign-in.')
param azureBotOAuthConnectionName string

@description('Comma-separated client app IDs allowed to call the Teams control plane.')
param controlPlaneAllowedClientIds string = '1fec8e78-bce4-4aaf-ab1b-5451cc387264,5e3ce6c0-2b1f-4285-8d4b-75ee78787346'

@description('Comma-separated Agent 365 Tooling Gateway client app IDs allowed to call MCP.')
param mcpAllowedClientIds string

__INTEGRATION_BICEP_PARAMS__

var suffix = uniqueString(resourceGroup().id, baseName)
var environmentName = '${take(baseName, 18)}-env-${suffix}'
var identityName = '${take(baseName, 18)}-id-${suffix}'
var storageName = 'aitmstate${suffix}'
var tableName = 'aiteammatestate'
var agentName = '${take(baseName, 22)}-agent'
var mcpName = '${take(baseName, 24)}-mcp'
var issuer = '${az.environment().authentication.loginEndpoint}${tenantId}/v2.0'
var jwks = '${az.environment().authentication.loginEndpoint}${tenantId}/discovery/v2.0/keys'
var controlPlaneDomain = '${agentName}.${environment.properties.defaultDomain}'
var controlPlaneTokenAudience = 'api://${controlPlaneDomain}/${blueprintClientId}'

resource identity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: identityName
  location: location
}

resource storage 'Microsoft.Storage/storageAccounts@2023-05-01' = {
  name: storageName
  location: location
  sku: { name: 'Standard_LRS' }
  kind: 'StorageV2'
  properties: {
    allowSharedKeyAccess: false
    allowBlobPublicAccess: false
    minimumTlsVersion: 'TLS1_2'
    publicNetworkAccess: 'Enabled'
  }
}

resource tableService 'Microsoft.Storage/storageAccounts/tableServices@2023-05-01' = {
  parent: storage
  name: 'default'
}

resource stateTable 'Microsoft.Storage/storageAccounts/tableServices/tables@2023-05-01' = {
  parent: tableService
  name: tableName
}

resource tableContributor 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(storage.id, identity.id, '0a9a7e1f-b9d0-4cc4-a60d-0319b160aaa3')
  scope: storage
  properties: {
    roleDefinitionId: subscriptionResourceId(
      'Microsoft.Authorization/roleDefinitions',
      '0a9a7e1f-b9d0-4cc4-a60d-0319b160aaa3'
    )
    principalId: identity.properties.principalId
    principalType: 'ServicePrincipal'
  }
}

resource environment 'Microsoft.App/managedEnvironments@2024-03-01' = {
  name: environmentName
  location: location
  properties: {}
}

var mcpPublicUrl = 'https://${mcpName}.${environment.properties.defaultDomain}'

var sharedEnv = [
  { name: 'AI_TEAMMATE_OFFLINE', value: 'false' }
  { name: 'AI_TEAMMATE_DEVELOPMENT_MODE', value: 'false' }
  { name: 'AGENT__IDENTITY__AGENT_ID', value: agentInstanceAppId }
  { name: 'AGENT__IDENTITY__BLUEPRINT_ID', value: blueprintClientId }
  { name: 'AGENT__IDENTITY__TENANT_ID', value: tenantId }
  { name: 'AGENT__IDENTITY__INSTANCE_APP_ID', value: agentInstanceAppId }
  { name: 'AGENT__IDENTITY__AGENTIC_USER_ID', value: agenticUserId }
  { name: 'AGENT__MANAGER__USER_ID', value: managerId }
  { name: 'AGENT__OBO__HANDLER_ID', value: 'OBO' }
  { name: 'AGENT__AGENTIC__HANDLER_ID', value: 'AGENTIC' }
  { name: 'AZURE_CLIENT_ID', value: identity.properties.clientId }
  { name: 'AZURE_OPENAI_ENDPOINT', value: azureOpenAiEndpoint }
  { name: '__MODEL_ENV__', value: azureOpenAiDeployment }
  { name: 'ENABLE_A365_OBSERVABILITY', value: 'true' }
  { name: 'ENABLE_A365_OBSERVABILITY_EXPORTER', value: 'true' }
  { name: 'STATE_TABLE_ENDPOINT', value: storage.properties.primaryEndpoints.table }
  { name: 'STATE_TABLE_NAME', value: tableName }
__INTEGRATION_BICEP_ENV__
  { name: 'A365__TOOLING_GATEWAY__MCP_ENDPOINT', value: toolingGatewayMcpEndpoint }
  { name: 'A365__TOOLING_GATEWAY__REGISTRATION_ID', value: toolingGatewayRegistrationId }
  { name: 'A365__TOOLING_GATEWAY__REMOTE_SCOPE', value: 'api://${blueprintClientId}/access_agent_as_user' }
  { name: 'CONTROL_PLANE_TOKEN_ISSUER', value: issuer }
  { name: 'CONTROL_PLANE_TOKEN_AUDIENCE', value: controlPlaneTokenAudience }
  { name: 'CONTROL_PLANE_JWKS_URL', value: jwks }
  { name: 'CONTROL_PLANE_REQUIRED_SCOPE', value: 'access_agent_as_user' }
  { name: 'CONTROL_PLANE_ALLOWED_CLIENT_IDS', value: controlPlaneAllowedClientIds }
  { name: 'MCP_TOKEN_ISSUER', value: issuer }
  { name: 'MCP_TOKEN_AUDIENCE', value: mcpTokenAudience }
  { name: 'MCP_JWKS_URL', value: jwks }
  { name: 'MCP_REQUIRED_SCOPE', value: mcpRequiredScope }
  { name: 'MCP_ALLOWED_CLIENT_IDS', value: mcpAllowedClientIds }
  { name: 'MCP_RESOURCE_SERVER_URL', value: '${mcpPublicUrl}/mcp' }
  { name: 'MCP__PUBLIC_URL', value: mcpPublicUrl }
  { name: 'CONNECTIONS__SERVICE_CONNECTION__SETTINGS__AUTHTYPE', value: 'FederatedCredentials' }
  { name: 'CONNECTIONS__SERVICE_CONNECTION__SETTINGS__CLIENTID', value: blueprintClientId }
  { name: 'CONNECTIONS__SERVICE_CONNECTION__SETTINGS__FEDERATEDCLIENTID', value: identity.properties.clientId }
  { name: 'CONNECTIONS__SERVICE_CONNECTION__SETTINGS__TENANTID', value: tenantId }
  { name: 'CONNECTIONS__OBO__SETTINGS__AUTHTYPE', value: 'FederatedCredentials' }
  { name: 'CONNECTIONS__OBO__SETTINGS__CLIENTID', value: blueprintClientId }
  { name: 'CONNECTIONS__OBO__SETTINGS__FEDERATEDCLIENTID', value: identity.properties.clientId }
  { name: 'CONNECTIONS__OBO__SETTINGS__TENANTID', value: tenantId }
  { name: 'AGENTAPPLICATION__USERAUTHORIZATION__AUTO_SIGN_IN', value: 'true' }
  { name: 'AGENTAPPLICATION__USERAUTHORIZATION__HANDLERS__OBO__SETTINGS__AZUREBOTOAUTHCONNECTIONNAME', value: azureBotOAuthConnectionName }
  { name: 'AGENTAPPLICATION__USERAUTHORIZATION__HANDLERS__OBO__SETTINGS__OBOCONNECTIONNAME', value: 'OBO' }
  { name: 'AGENTAPPLICATION__USERAUTHORIZATION__HANDLERS__AGENTIC__SETTINGS__TYPE', value: 'AgenticUserAuthorization' }
]

resource agent 'Microsoft.App/containerApps@2024-03-01' = {
  name: agentName
  location: location
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: { '${identity.id}': {} }
  }
  properties: {
    managedEnvironmentId: environment.id
    configuration: {
      ingress: { external: true, targetPort: 8080, transport: 'http', allowInsecure: false }
      secrets: [
    __INTEGRATION_BICEP_SECRETS__
      ]
    }
    template: {
      containers: [{
        name: 'agent'
        image: agentImage
        env: concat(sharedEnv, [
          { name: '__AGENT_PORT_ENV__', value: '8080' }
          { name: 'CONTROL_PLANE_INTERNAL_URL', value: 'http://127.0.0.1:8000' }
        ])
        resources: { cpu: json('0.5'), memory: '1Gi' }
      }, {
        name: 'control-plane'
        image: controlPlaneImage
        env: concat(sharedEnv, [{ name: '__CONTROL_PLANE_PORT_ENV__', value: '8000' }])
        resources: { cpu: json('0.5'), memory: '1Gi' }
      }]
      // Agent SDK OAuth/conversation state is process-local; keep one replica until replaced.
      scale: { minReplicas: 1, maxReplicas: 1 }
    }
  }
}

resource mcp 'Microsoft.App/containerApps@2024-03-01' = {
  name: mcpName
  location: location
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: { '${identity.id}': {} }
  }
  properties: {
    managedEnvironmentId: environment.id
    configuration: {
      ingress: { external: true, targetPort: 8080, transport: 'http', allowInsecure: false }
      secrets: [
    __INTEGRATION_BICEP_SECRETS__
      ]
    }
    template: {
      containers: [{
        name: 'mcp'
        image: mcpImage
        env: concat(sharedEnv, [
          { name: 'MCP_HOST', value: '0.0.0.0' }
          { name: '__MCP_PORT_ENV__', value: '8080' }
          { name: 'MCP_ALLOW_DEV_NO_AUTH', value: 'false' }
        ])
        resources: { cpu: json('0.5'), memory: '1Gi' }
      }]
      scale: { minReplicas: 1, maxReplicas: 5 }
    }
  }
}

output agentEndpoint string = 'https://${agent.properties.configuration.ingress.fqdn}/api/messages'
output controlPlaneEndpoint string = 'https://${agent.properties.configuration.ingress.fqdn}'
output controlPlaneDomain string = agent.properties.configuration.ingress.fqdn
output controlPlaneTokenAudience string = controlPlaneTokenAudience
output mcpEndpoint string = '${mcpPublicUrl}/mcp'
output managedIdentityClientId string = identity.properties.clientId
output managedIdentityPrincipalId string = identity.properties.principalId
output stateTableEndpoint string = storage.properties.primaryEndpoints.table
output stateTableName string = stateTable.name
