// Azure Bot Service registration for the Movate Teams bot.
//
// The Bot Service is the **router** between Microsoft Teams (the
// channel) and the bot's webhook (the Container App we deploy
// alongside). When a Teams user mentions ``@movate``, the chain is:
//
//   Teams client
//     ↓ (Bot Framework Activity over Bot Service connector)
//   Microsoft.BotService/botServices
//     ↓ (HTTPS POST signed with the bot's AAD app id)
//   <fqdn>/api/messages    ← our containerapp-teams-bot
//
// The Bot Service resource needs:
//   * The endpoint URL it forwards Activities to (the Container App fqdn)
//   * The bot's AAD app id (a separate AAD app registration; created
//     outside Bicep via `az ad app create` or the Azure portal —
//     reasons documented in the bot-app-id param description below)
//
// The "msteams" channel resource attaches Teams as a delivery
// destination — without it the bot exists but Teams can't reach it.

@description('Bot Service resource name. Default mirrors the deployment env (movate-teams-{env}).')
param name string

@description('Azure region. Bot Service supports only a handful of regions — `global` is the canonical home for new resources as of 2025.')
param location string = 'global'

@description('SKU — `F0` (free, up to 10k messages / month) for dev; `S1` (paid, unlimited) for prod.')
@allowed([
  'F0'
  'S1'
])
param sku string = 'F0'

@description('Display name shown in the Bot Service portal + Teams Admin Center. Different from the Teams app manifest\'s display name (which the user sees).')
param displayName string = 'Movate Agent Runner'

@description('''
AAD app id (UUID) for the bot. Created OUTSIDE Bicep — Bot Service
requires the AAD app registration to exist BEFORE the Bot Service
resource is created, because the Bot Service is bound to it at create
time, not updatable later. Create with:
    az ad app create --display-name "movate-teams-bot-<env>"
Then copy the appId into your .bicepparam file.
''')
param botAppId string

@description('Webhook URL the Bot Service forwards Activities to. Typically `https://<container-app-fqdn>/api/messages`. Set by main.bicep from the containerapp-teams-bot output.')
param messagingEndpoint string

@description('Common tags.')
param tags object = {}

resource bot 'Microsoft.BotService/botServices@2022-09-15' = {
  name: name
  location: location
  tags: tags
  sku: {
    name: sku
  }
  kind: 'azurebot'
  properties: {
    displayName: displayName
    description: 'Movate Development Kit — run + evaluate Movate agents from Teams.'
    // Microsoft Single Tenant: only users in this tenant can interact.
    // Multi-tenant deployments would flip this to MultiTenant; for
    // the alpha pilot single-tenant matches the security posture.
    msaAppType: 'SingleTenant'
    msaAppId: botAppId
    msaAppTenantId: subscription().tenantId
    endpoint: messagingEndpoint
    // Disable local-auth so the connector requires signed JWTs on
    // the webhook side. The bot's JWT-validation logic lands in the
    // hardening PR (#70); flipping this on now sets the right
    // expectation in prod even before the validation code is live.
    disableLocalAuth: true
  }
}

// Attach the Microsoft Teams channel — without this the bot exists
// but isn't reachable from a Teams client. SMS, Slack, Webex, etc.
// would each be separate channel resources; we only ship Teams.
resource teamsChannel 'Microsoft.BotService/botServices/channels@2022-09-15' = {
  parent: bot
  name: 'MsTeamsChannel'
  location: location
  properties: {
    channelName: 'MsTeamsChannel'
    properties: {
      // Default settings — no calling, no media, no per-tenant filter.
      // Add ``acceptedTerms: true`` is set automatically when the
      // resource is created (regulatory acceptance).
      isEnabled: true
    }
  }
}

@description('Bot Service resource id — operators reference this from the Teams Admin Center when publishing the app.')
output botServiceId string = bot.id

@description('The bot\'s display name — surfaces in the Teams app catalog.')
output displayName string = bot.properties.displayName
