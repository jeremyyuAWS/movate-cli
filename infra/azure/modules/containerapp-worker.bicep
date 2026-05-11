// movate-worker Container App — runs `movate worker` (no ingress).
// Same image as the API; only the command differs.
//
// Scales horizontally on **queue depth** via a KEDA postgresql
// scaler. Counts claimable jobs (status='queued' AND retry window
// elapsed) and adds one replica per ``targetQueryValue`` queued
// jobs. Queue depth is a *leading* indicator (the load is visible
// before any pod's CPU rises); CPU was a lagging indicator.

@description('Container App name.')
param name string

@description('Azure region.')
param location string

@description('Container Apps Environment id.')
param environmentId string

@description('ACR login server.')
param acrLoginServer string

@description('ACR resource id.')
param acrResourceId string

@description('Image tag, e.g. movate:0.5.0.')
param image string

@description('Key Vault URI for secret references.')
param keyVaultUri string

@description('Postgres FQDN.')
param postgresFqdn string

@description('Postgres database name.')
param postgresDatabase string

@description('Postgres admin username.')
param postgresAdminUsername string

@description('Min replicas. Workers can scale to zero on dev/staging; prod stays warm.')
@minValue(0)
@maxValue(30)
param minReplicas int = 1

@description('Max replicas.')
@minValue(1)
@maxValue(30)
param maxReplicas int = 2

@description('CPU per replica.')
param cpu string = '0.5'

@description('Memory per replica.')
param memory string = '1.0Gi'

@description('''
Queue depth per replica that triggers a scale-up. KEDA evaluates this
roughly every 30s by running the SQL query and computing
``ceil(query_result / targetQueryValue)`` for the desired replica count.
Default 5: at 50 queued jobs, ceil(50/5)=10 replicas; with maxReplicas=2
the worker pegs at 2 until the queue drains. Tune up for cheaper agents
(small target → many replicas), down for expensive agents that need
exclusive CPU.
''')
@minValue(1)
@maxValue(1000)
param queueDepthPerReplica int = 5

@description('Common tags.')
param tags object = {}

resource worker 'Microsoft.App/containerApps@2024-03-01' = {
  name: name
  location: location
  tags: tags
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    environmentId: environmentId
    configuration: {
      // No ingress — workers don't accept inbound traffic, they pull
      // from the queue. Setting ingress = null at the Bicep level is
      // expressed by simply omitting the `ingress` block.
      registries: [
        {
          server: acrLoginServer
          identity: 'system'
        }
      ]
      secrets: [
        {
          name: 'pg-password'
          keyVaultUrl: '${keyVaultUri}secrets/pg-admin-password'
          identity: 'system'
        }
        {
          // Full libpq connection string for the KEDA postgresql
          // scaler. Distinct from PGPASSWORD because KEDA runs
          // OUTSIDE the worker container (in the ACA env's scaler
          // sidecar) and needs a self-contained DSN. Populate this
          // KV secret during the two-pass deploy:
          //   az keyvault secret set --vault-name $KV
          //     --name pg-connection-string
          //     --value "host=$PG_FQDN port=5432 user=movate
          //              password=$PG_PASSWORD dbname=$PG_DB sslmode=require"
          name: 'pg-connection-string'
          keyVaultUrl: '${keyVaultUri}secrets/pg-connection-string'
          identity: 'system'
        }
        {
          name: 'openai-api-key'
          keyVaultUrl: '${keyVaultUri}secrets/openai-api-key'
          identity: 'system'
        }
        {
          name: 'anthropic-api-key'
          keyVaultUrl: '${keyVaultUri}secrets/anthropic-api-key'
          identity: 'system'
        }
        {
          name: 'langfuse-secret-key'
          keyVaultUrl: '${keyVaultUri}secrets/langfuse-secret-key'
          identity: 'system'
        }
        {
          name: 'langfuse-public-key'
          keyVaultUrl: '${keyVaultUri}secrets/langfuse-public-key'
          identity: 'system'
        }
      ]
    }
    template: {
      containers: [
        {
          name: 'movate-worker'
          image: '${acrLoginServer}/${image}'
          command: ['movate']
          args: ['worker', '--poll-interval', '1.0']
          resources: {
            cpu: json(cpu)
            memory: memory
          }
          env: [
            {
              name: 'MOVATE_DB_URL'
              value: 'postgresql://${postgresAdminUsername}:@${postgresFqdn}:5432/${postgresDatabase}?sslmode=require'
            }
            {
              name: 'PGPASSWORD'
              secretRef: 'pg-password'
            }
            {
              // KEDA's postgresql scaler reads from this env var
              // (see ``connectionFromEnv`` in the scale rule below).
              // It's set on the container by ACA but consumed by the
              // KEDA sidecar that lives in the ACA environment, not
              // by the worker process itself.
              name: 'KEDA_PG_CONNECTION_STRING'
              secretRef: 'pg-connection-string'
            }
            {
              name: 'OPENAI_API_KEY'
              secretRef: 'openai-api-key'
            }
            {
              name: 'ANTHROPIC_API_KEY'
              secretRef: 'anthropic-api-key'
            }
            {
              name: 'LANGFUSE_SECRET_KEY'
              secretRef: 'langfuse-secret-key'
            }
            {
              name: 'LANGFUSE_PUBLIC_KEY'
              secretRef: 'langfuse-public-key'
            }
            {
              name: 'MOVATE_AGENTS_PATH'
              value: '/app/agents'
            }
          ]
        }
      ]
      scale: {
        minReplicas: minReplicas
        maxReplicas: maxReplicas
        rules: [
          {
            // KEDA postgresql scaler — leading indicator (queue depth)
            // beats lagging (CPU). The query filters on the same
            // claimable-set the worker's claim_next_job uses:
            //   status='queued' AND (next_retry_at IS NULL OR <= now)
            // so re-queued jobs awaiting backoff don't artificially
            // inflate the scale-up signal.
            //
            // ACA evaluates this ~every 30s. Desired replicas =
            // ceil(queryResult / targetQueryValue), clamped to
            // [minReplicas, maxReplicas].
            name: 'queue-depth'
            custom: {
              type: 'postgresql'
              metadata: {
                connectionFromEnv: 'KEDA_PG_CONNECTION_STRING'
                query: 'SELECT COUNT(*) FROM jobs WHERE status = \'queued\' AND (next_retry_at IS NULL OR next_retry_at <= NOW())'
                targetQueryValue: string(queueDepthPerReplica)
              }
            }
          }
        ]
      }
    }
  }
}

output workerName string = worker.name
output principalId string = worker.identity.principalId
output appResourceId string = worker.id
output acrResourceIdEcho string = acrResourceId
