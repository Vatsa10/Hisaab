// Expense-agent on Azure Container Apps (scale-to-zero). Budget target: ~INR 5,000/month.
targetScope = 'resourceGroup'

param location string = 'centralindia'
param prefix string = 'expagent'

@description('Phase 1 (false) creates only the registry, storage, identity and environment so images can be built; phase 2 (true) adds the Container Apps.')
param deployApps bool = true

@description('Full image reference for the API, e.g. <acr>.azurecr.io/api:<sha>')
param apiImage string = ''
@description('Full image reference for the worker, e.g. <acr>.azurecr.io/worker:<sha>')
param workerImage string = ''

@secure()
param databaseUrl string
@secure()
param supabaseJwtSecret string
@secure()
param anthropicApiKey string
// Cost policy: llm.py refuses any model at or above $1 / MTok, so only Claude Haiku 5.5 starts.
param anthropicModel string = 'claude-haiku-5-5'
@secure()
param typesafeApiKey string
@secure()
param profileMasterKey string
@secure()
param proxyUrlTemplate string

@description('Extra Chrome flags for the containerised worker')
param chromeExtraArgs string = '--no-sandbox --disable-dev-shm-usage'

// Budget, unit costs, limits and notifications. Defaults match server/config.py; the two secrets are optional.
param appUrl string = 'http://localhost:3000'
param monthlyBudgetInr string = '5000'
param proxyInrPerGb string = '400'
param aiInrPer1kTokens string = '0.05'
param workerInrPerMinute string = '0.20'
param fixedMonthlyInr string = '0'
param refreshPerAppPerDay string = '3'
param cartChecksPerDay string = '10'
param syncIntervalH string = '24'
param pausedSyncIntervalH string = '72'
param smtpHost string = ''
param smtpPort string = '587'
param smtpUser string = ''
param smtpStarttls string = '1'
param smtpFrom string = 'Hisaab <no-reply@localhost>'
param vapidPublicKey string = ''
param vapidSubject string = 'mailto:admin@localhost'
@secure()
param smtpPassword string = ''
@secure()
param vapidPrivateKey string = ''

var uniq = uniqueString(resourceGroup().id)
var acrName = toLower('${prefix}${uniq}')
var storageName = toLower(take('${prefix}${uniq}', 24))
var budgetEnv = [
  { name: 'APP_URL', value: appUrl }
  { name: 'MONTHLY_BUDGET_INR', value: monthlyBudgetInr }
  { name: 'PROXY_INR_PER_GB', value: proxyInrPerGb }
  { name: 'AI_INR_PER_1K_TOKENS', value: aiInrPer1kTokens }
  { name: 'WORKER_INR_PER_MINUTE', value: workerInrPerMinute }
  { name: 'FIXED_MONTHLY_INR', value: fixedMonthlyInr }
  { name: 'REFRESH_PER_APP_PER_DAY', value: refreshPerAppPerDay }
  { name: 'CART_CHECKS_PER_DAY', value: cartChecksPerDay }
  { name: 'SYNC_INTERVAL_H', value: syncIntervalH }
  { name: 'PAUSED_SYNC_INTERVAL_H', value: pausedSyncIntervalH }
  { name: 'SMTP_HOST', value: smtpHost }
  { name: 'SMTP_PORT', value: smtpPort }
  { name: 'SMTP_USER', value: smtpUser }
  { name: 'SMTP_STARTTLS', value: smtpStarttls }
  { name: 'SMTP_FROM', value: smtpFrom }
  { name: 'VAPID_PUBLIC_KEY', value: vapidPublicKey }
  { name: 'VAPID_SUBJECT', value: vapidSubject }
]
var notifySecrets = concat(
  empty(smtpPassword) ? [] : [{ name: 'smtp-password', value: smtpPassword }],
  empty(vapidPrivateKey) ? [] : [{ name: 'vapid-private-key', value: vapidPrivateKey }]
)
var notifySecretEnv = concat(
  empty(smtpPassword) ? [] : [{ name: 'SMTP_PASSWORD', secretRef: 'smtp-password' }],
  empty(vapidPrivateKey) ? [] : [{ name: 'VAPID_PRIVATE_KEY', secretRef: 'vapid-private-key' }]
)
var acrPullRoleId = '7f951dda-4ed3-4680-a7ca-43fe172d538d'

resource logs 'Microsoft.OperationalInsights/workspaces@2022-10-01' = {
  name: '${prefix}-logs'
  location: location
  properties: {
    sku: { name: 'PerGB2018' }
    retentionInDays: 30
  }
}

resource acr 'Microsoft.ContainerRegistry/registries@2023-07-01' = {
  name: acrName
  location: location
  sku: { name: 'Basic' }
  properties: {
    adminUserEnabled: false
  }
}

resource identity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: '${prefix}-id'
  location: location
}

resource acrPull 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: acr
  name: guid(acr.id, identity.id, acrPullRoleId)
  properties: {
    principalId: identity.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', acrPullRoleId)
  }
}

resource storage 'Microsoft.Storage/storageAccounts@2023-01-01' = {
  name: storageName
  location: location
  kind: 'StorageV2'
  sku: { name: 'Standard_LRS' }
  properties: {
    minimumTlsVersion: 'TLS1_2'
    allowBlobPublicAccess: false
    supportsHttpsTrafficOnly: true
  }
}

resource blobService 'Microsoft.Storage/storageAccounts/blobServices@2023-01-01' = {
  parent: storage
  name: 'default'
}

resource profilesContainer 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-01-01' = {
  parent: blobService
  name: 'profiles'
  properties: {
    publicAccess: 'None'
  }
}

var storageConnectionString = 'DefaultEndpointsProtocol=https;AccountName=${storage.name};AccountKey=${storage.listKeys().keys[0].value};EndpointSuffix=${environment().suffixes.storage}'

resource env 'Microsoft.App/managedEnvironments@2024-03-01' = {
  name: '${prefix}-env'
  location: location
  properties: {
    appLogsConfiguration: {
      destination: 'log-analytics'
      logAnalyticsConfiguration: {
        customerId: logs.properties.customerId
        sharedKey: logs.listKeys().primarySharedKey
      }
    }
  }
}

var registries = [
  {
    server: acr.properties.loginServer
    identity: identity.id
  }
]

resource api 'Microsoft.App/containerApps@2024-03-01' = if (deployApps) {
  name: '${prefix}-api'
  location: location
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: { '${identity.id}': {} }
  }
  dependsOn: [acrPull]
  properties: {
    managedEnvironmentId: env.id
    configuration: {
      activeRevisionsMode: 'Single'
      ingress: {
        external: true
        targetPort: 8000
        transport: 'auto'
        allowInsecure: false
      }
      registries: registries
      secrets: concat([
        { name: 'database-url', value: databaseUrl }
        { name: 'supabase-jwt-secret', value: supabaseJwtSecret }
        { name: 'anthropic-api-key', value: anthropicApiKey }
        { name: 'typesafe-api-key', value: typesafeApiKey }
        { name: 'profile-master-key', value: profileMasterKey }
      ], notifySecrets)
    }
    template: {
      containers: [
        {
          name: 'api'
          image: apiImage
          resources: { cpu: json('0.5'), memory: '1Gi' }
          env: concat([
            { name: 'DATABASE_URL', secretRef: 'database-url' }
            { name: 'SUPABASE_JWT_SECRET', secretRef: 'supabase-jwt-secret' }
            { name: 'ANTHROPIC_API_KEY', secretRef: 'anthropic-api-key' }
            { name: 'ANTHROPIC_MODEL', value: anthropicModel }
            { name: 'TYPESAFE_API_KEY', secretRef: 'typesafe-api-key' }
            { name: 'PROFILE_MASTER_KEY', secretRef: 'profile-master-key' }
          ], budgetEnv, notifySecretEnv)
          probes: [
            {
              type: 'Liveness'
              httpGet: { path: '/healthz', port: 8000 }
              initialDelaySeconds: 10
              periodSeconds: 30
            }
            {
              type: 'Readiness'
              httpGet: { path: '/healthz', port: 8000 }
              periodSeconds: 10
            }
          ]
        }
      ]
      scale: {
        minReplicas: 0
        maxReplicas: 1
        rules: [
          {
            name: 'http'
            http: { metadata: { concurrentRequests: '20' } }
          }
        ]
      }
    }
  }
}

resource worker 'Microsoft.App/containerApps@2024-03-01' = if (deployApps) {
  name: '${prefix}-worker'
  location: location
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: { '${identity.id}': {} }
  }
  dependsOn: [acrPull]
  properties: {
    managedEnvironmentId: env.id
    configuration: {
      activeRevisionsMode: 'Single'
      registries: registries
      secrets: concat([
        { name: 'database-url', value: databaseUrl }
        { name: 'anthropic-api-key', value: anthropicApiKey }
        { name: 'typesafe-api-key', value: typesafeApiKey }
        { name: 'profile-master-key', value: profileMasterKey }
        { name: 'proxy-url-template', value: proxyUrlTemplate }
        { name: 'profile-store-url', value: storageConnectionString }
      ], notifySecrets)
    }
    template: {
      // On SIGTERM the worker stops claiming and lets the in-flight job run up to DRAIN_S (540 s), then aborts it
      // (sync requeued, connect failed) before this grace period ends.
      terminationGracePeriodSeconds: 600
      containers: [
        {
          name: 'worker'
          image: workerImage
          resources: { cpu: 2, memory: '4Gi' }
          env: concat([
            { name: 'DATABASE_URL', secretRef: 'database-url' }
            { name: 'ANTHROPIC_API_KEY', secretRef: 'anthropic-api-key' }
            { name: 'ANTHROPIC_MODEL', value: anthropicModel }
            { name: 'TYPESAFE_API_KEY', secretRef: 'typesafe-api-key' }
            { name: 'PROFILE_MASTER_KEY', secretRef: 'profile-master-key' }
            { name: 'PROXY_URL_TEMPLATE', secretRef: 'proxy-url-template' }
            { name: 'PROFILE_STORE_URL', secretRef: 'profile-store-url' }
            { name: 'CHROME_EXTRA_ARGS', value: chromeExtraArgs }
            { name: 'PROFILE_ROOT', value: '/tmp/profiles' }
            // SIGTERM drain must end inside terminationGracePeriodSeconds (600); a silent job is reclaimed after 300 s.
            { name: 'DRAIN_S', value: '540' }
            { name: 'STALE_JOB_S', value: '300' }
          ], budgetEnv, notifySecretEnv)
        }
      ]
      scale: {
        minReplicas: 0
        maxReplicas: 2
        rules: [
          {
            name: 'queued-jobs'
            custom: {
              type: 'postgresql'
              metadata: {
                query: 'select count(*) from jobs where status in (\'queued\',\'running\')'
                targetQueryValue: '1'
              }
              auth: [
                { secretRef: 'database-url', triggerParameter: 'connection' }
              ]
            }
          }
        ]
      }
    }
  }
}

// Every 15 minutes: queue due syncs, send budget alerts, dispatch notifications. One run, then exit.
resource scheduler 'Microsoft.App/jobs@2024-03-01' = if (deployApps) {
  name: '${prefix}-scheduler'
  location: location
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: { '${identity.id}': {} }
  }
  dependsOn: [acrPull]
  properties: {
    environmentId: env.id
    configuration: {
      triggerType: 'Schedule'
      scheduleTriggerConfig: {
        cronExpression: '*/15 * * * *'
        parallelism: 1
        replicaCompletionCount: 1
      }
      replicaTimeout: 600
      replicaRetryLimit: 0
      registries: registries
      secrets: concat([
        { name: 'database-url', value: databaseUrl }
      ], notifySecrets)
    }
    template: {
      containers: [
        {
          name: 'scheduler'
          image: apiImage
          command: ['python', '-m', 'server.scheduler', '--once']
          resources: { cpu: json('0.25'), memory: '0.5Gi' }
          env: concat([
            { name: 'DATABASE_URL', secretRef: 'database-url' }
          ], budgetEnv, notifySecretEnv)
        }
      ]
    }
  }
}

output apiUrl string = deployApps ? 'https://${api.?properties.configuration.ingress.fqdn}' : ''
output acrLoginServer string = acr.properties.loginServer
