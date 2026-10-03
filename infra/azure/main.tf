// ALM Access Provisioning - Azure. A skeleton: the same shape as infra/gcp,
// validated in CI, to be completed and applied when the client chooses Azure.
//
//   API     Container App with internal ingress only; sign-in is the
//           application's own OIDC against Entra ID (ALM_AUTH_MODE=oidc).
//   Workers Container App without ingress; /healthz for the liveness probe.
//   State   Azure Database for PostgreSQL Flexible Server 16, private access,
//           Entra ID authentication only (no password), PITR.
//   Secrets Key Vault, read by the apps' managed identity.
//   Models  Azure OpenAI through the managed identity (ALM_LLM_PROVIDER=azure_openai).
//
// The network is the client's: this takes an existing virtual network and
// subnets, and never creates or changes the link to the corporate estate
// (ExpressRoute or VPN). Nothing here has a public endpoint.
//
//   terraform init -backend-config=...
//   terraform apply -var-file=envs/test.tfvars

terraform {
  required_version = ">= 1.6"

  required_providers {
    azurerm = {
      source  = "hashicorp/azurerm"
      version = ">= 4.10, < 5.0"
    }
  }

  backend "azurerm" {
    // resource_group_name, storage_account_name, container_name and key
    // supplied by `terraform init -backend-config=...`
  }
}

provider "azurerm" {
  features {}
  subscription_id = var.subscription_id
}

data "azurerm_client_config" "current" {}

locals {
  prefix  = "alm-${var.environment}"
  is_prod = var.environment == "prod"
  tags    = merge(var.tags, { app = "alm-provisioning", environment = var.environment })

  // Every setting the API and the workers share; the same names as on GCP.
  run_env = {
    ALM_ENVIRONMENT             = upper(var.environment)
    ALM_SHADOW_MODE             = tostring(var.shadow_mode)
    ALM_ORCHESTRATION           = var.orchestration
    ALM_LLM_PROVIDER            = "azure_openai"
    ALM_AZURE_OPENAI_ENDPOINT   = var.azure_openai_endpoint
    ALM_AGENT_MODEL             = var.agent_deployment
    ALM_SUPERVISOR_MODEL        = var.supervisor_deployment
    EWM_SERVER                  = var.ewm_server
    JTS_SERVER                  = var.jts_server
    CID                         = var.service_account_cid
    ALM_SECRET_BACKEND          = "azure" // pragma: allowlist secret - names the secret store
    ALM_AZURE_KEY_VAULT_URL     = azurerm_key_vault.main.vault_uri
    ALM_DB_AUTH                 = "azure_ad"
    ALM_POSTGRES_DSN            = "postgresql://${azurerm_user_assigned_identity.app.name}@${azurerm_postgresql_flexible_server.main.fqdn}:5432/alm?sslmode=require"
    AZURE_CLIENT_ID             = azurerm_user_assigned_identity.app.client_id
    ALM_AD_JOB_TRANSPORT        = "store"
    ALM_AUTH_MODE               = "oidc"
    ALM_OIDC_ISSUER             = "https://login.microsoftonline.com/${data.azurerm_client_config.current.tenant_id}/v2.0"
    ALM_OIDC_CLIENT_ID          = var.oidc_client_id
    ALM_ROLE_MAP                = var.role_map
    ALM_RETENTION_DAYS          = tostring(var.retention_days)
    ALM_OTEL_ENABLED            = tostring(var.otel_endpoint != "")
    OTEL_EXPORTER_OTLP_ENDPOINT = var.otel_endpoint
    ALM_CA_BUNDLE               = "/etc/ssl/certs/corporate-ca.pem"
    ALM_APPROVAL_BASE_URL       = "https://${var.api_hostname}"
  }
}

resource "azurerm_resource_group" "main" {
  name     = "rg-${local.prefix}"
  location = var.location
  tags     = local.tags
}

// ------------------------------------------------------------------ identity
// One user-assigned identity for both apps: Key Vault, the database and Azure
// OpenAI all trust it, so there is no key or password anywhere.
resource "azurerm_user_assigned_identity" "app" {
  name                = "id-${local.prefix}-app"
  resource_group_name = azurerm_resource_group.main.name
  location            = azurerm_resource_group.main.location
  tags                = local.tags
}

// ----------------------------------------------------------------- Key Vault
resource "azurerm_key_vault" "main" {
  name                          = "kv-${local.prefix}-${substr(sha1(azurerm_resource_group.main.id), 0, 6)}"
  resource_group_name           = azurerm_resource_group.main.name
  location                      = azurerm_resource_group.main.location
  tenant_id                     = data.azurerm_client_config.current.tenant_id
  sku_name                      = "standard"
  rbac_authorization_enabled    = true
  public_network_access_enabled = false
  purge_protection_enabled      = local.is_prod
  soft_delete_retention_days    = 90
  tags                          = local.tags
}

resource "azurerm_role_assignment" "secrets_reader" {
  scope                = azurerm_key_vault.main.id
  role_definition_name = "Key Vault Secrets User"
  principal_id         = azurerm_user_assigned_identity.app.principal_id
}

resource "azurerm_private_endpoint" "key_vault" {
  name                = "pe-${local.prefix}-kv"
  resource_group_name = azurerm_resource_group.main.name
  location            = azurerm_resource_group.main.location
  subnet_id           = var.endpoints_subnet_id
  tags                = local.tags

  private_service_connection {
    name                           = "kv"
    private_connection_resource_id = azurerm_key_vault.main.id
    subresource_names              = ["vault"]
    is_manual_connection           = false
  }
}

// The secret values are set by an operator (az keyvault secret set), never in
// Terraform state: alm-service-account-password, alm-session-signing-key,
// alm-oidc-client-secret, alm-webhook-hmac-key.

// ------------------------------------------------------------------ database
resource "azurerm_postgresql_flexible_server" "main" {
  name                          = "psql-${local.prefix}"
  resource_group_name           = azurerm_resource_group.main.name
  location                      = azurerm_resource_group.main.location
  version                       = "16"
  sku_name                      = var.db_sku
  storage_mb                    = 65536
  backup_retention_days         = 35
  geo_redundant_backup_enabled  = local.is_prod
  delegated_subnet_id           = var.database_subnet_id
  private_dns_zone_id           = var.postgres_private_dns_zone_id
  public_network_access_enabled = false
  zone                          = "1"
  tags                          = local.tags

  // Entra ID only: no password exists for anyone to leak.
  authentication {
    active_directory_auth_enabled = true
    password_auth_enabled         = false
    tenant_id                     = data.azurerm_client_config.current.tenant_id
  }

  dynamic "high_availability" {
    for_each = local.is_prod ? [1] : []
    content {
      mode = "ZoneRedundant"
    }
  }
}

resource "azurerm_postgresql_flexible_server_database" "alm" {
  name      = "alm"
  server_id = azurerm_postgresql_flexible_server.main.id
  charset   = "UTF8"
  collation = "en_US.utf8"
}

resource "azurerm_postgresql_flexible_server_active_directory_administrator" "admin" {
  server_name         = azurerm_postgresql_flexible_server.main.name
  resource_group_name = azurerm_resource_group.main.name
  tenant_id           = data.azurerm_client_config.current.tenant_id
  object_id           = var.db_admin_group_object_id
  principal_name      = var.db_admin_group_name
  principal_type      = "Group"
}

// ------------------------------------------------------------- Azure OpenAI
resource "azurerm_role_assignment" "openai_user" {
  scope                = var.azure_openai_resource_id
  role_definition_name = "Cognitive Services OpenAI User"
  principal_id         = azurerm_user_assigned_identity.app.principal_id
}

// ------------------------------------------------------------ Container Apps
resource "azurerm_log_analytics_workspace" "main" {
  name                = "log-${local.prefix}"
  resource_group_name = azurerm_resource_group.main.name
  location            = azurerm_resource_group.main.location
  sku                 = "PerGB2018"
  retention_in_days   = 30
  tags                = local.tags
}

resource "azurerm_container_app_environment" "main" {
  name                           = "cae-${local.prefix}"
  resource_group_name            = azurerm_resource_group.main.name
  location                       = azurerm_resource_group.main.location
  log_analytics_workspace_id     = azurerm_log_analytics_workspace.main.id
  infrastructure_subnet_id       = var.apps_subnet_id
  internal_load_balancer_enabled = true
  tags                           = local.tags
}

locals {
  apps = {
    api = {
      image = var.api_image
      env   = merge(local.run_env, { ALM_WORKER_CONCURRENCY = "0" })
      min   = var.api_min_replicas
      max   = var.api_max_replicas
    }
    worker = {
      image = var.worker_image
      env   = merge(local.run_env, { ALM_WORKER_CONCURRENCY = tostring(var.worker_concurrency), ALM_WORKER_HEALTH_PORT = "8080" })
      min   = var.worker_min_replicas
      max   = var.worker_max_replicas
    }
  }
}

resource "azurerm_container_app" "main" {
  for_each                     = local.apps
  name                         = "ca-${local.prefix}-${each.key}"
  resource_group_name          = azurerm_resource_group.main.name
  container_app_environment_id = azurerm_container_app_environment.main.id
  revision_mode                = "Single"
  tags                         = local.tags

  identity {
    type         = "UserAssigned"
    identity_ids = [azurerm_user_assigned_identity.app.id]
  }

  registry {
    server   = var.registry_server
    identity = azurerm_user_assigned_identity.app.id
  }

  // Only the API takes requests, and only from inside the virtual network.
  dynamic "ingress" {
    for_each = each.key == "api" ? [1] : []
    content {
      external_enabled = false
      target_port      = 8080
      transport        = "http"
      traffic_weight {
        latest_revision = true
        percentage      = 100
      }
    }
  }

  template {
    min_replicas = each.value.min
    max_replicas = each.value.max

    volume {
      name         = "tmp"
      storage_type = "EmptyDir"
    }

    container {
      name    = each.key
      image   = each.value.image
      cpu     = 1
      memory  = "2Gi"
      command = each.key == "worker" ? ["python", "-m", "alm_agents.worker"] : null

      dynamic "env" {
        for_each = each.value.env
        content {
          name  = env.key
          value = env.value
        }
      }

      volume_mounts {
        name = "tmp"
        path = "/tmp"
      }

      liveness_probe {
        transport = "HTTP"
        port      = 8080
        path      = "/healthz"
      }
    }
  }
}
