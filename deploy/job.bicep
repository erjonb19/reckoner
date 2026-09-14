// Container Apps Job for the scheduled pipeline.
//
// Sized at 2 vCPU / 4 GiB deliberately. Consumption allows up to 4 vCPU / 8 GiB,
// but this pipeline has peaked at 9,808 MiB on a sharded mart run -- above even
// the maximum. Asking for 8 GiB would not make that stage fit and would double
// the consumption of every stage that does. The entrypoint logs the ceiling
// against each stage's measured peak so an OOM is a prediction rather than a
// silent kill.
param location string = resourceGroup().location
param environmentName string = 'reckoner-env'
param jobName string = 'reckoner-pipeline'
param image string = 'ghcr.io/erjonb19/reckoner/reckoner-job:latest'
param storageAccount string = 'reckonerlake0914'
param identityId string
param identityClientId string

@description('Cron in UTC. Monthly: payer files update monthly, hospital annually.')
param cronExpression string = '0 6 1 * *'

resource env 'Microsoft.App/managedEnvironments@2024-03-01' existing = {
  name: environmentName
}

resource job 'Microsoft.App/jobs@2024-03-01' = {
  name: jobName
  location: location
  identity: {
    // User-assigned rather than a secret: the identity holds Storage Blob Data
    // Contributor and DefaultAzureCredential picks it up. Nothing to store, so
    // nothing to leak or rotate.
    type: 'UserAssigned'
    userAssignedIdentities: { '${identityId}': {} }
  }
  properties: {
    environmentId: env.id
    configuration: {
      triggerType: 'Schedule'
      replicaTimeout: 3600
      replicaRetryLimit: 1
      scheduleTriggerConfig: {
        cronExpression: cronExpression
        parallelism: 1
        replicaCompletionCount: 1
      }
    }
    template: {
      containers: [
        {
          name: 'pipeline'
          image: image
          args: ['--stage', 'manifest']
          resources: {
            cpu: json('2.0')
            memory: '4Gi'
          }
          env: [
            { name: 'RECKONER_STORAGE', value: 'adls' }
            { name: 'RECKONER_ADLS_ACCOUNT', value: storageAccount }
            { name: 'RECKONER_ADLS_ROOT', value: 'lake' }
            { name: 'AZURE_CLIENT_ID', value: identityClientId }
          ]
        }
      ]
    }
  }
}

output jobId string = job.id
