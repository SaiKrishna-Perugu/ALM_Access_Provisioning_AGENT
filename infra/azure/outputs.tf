output "console_url" {
  description = "The console. Point the internal DNS name at api_internal_fqdn."
  value       = "https://${var.api_hostname}"
}

output "api_internal_fqdn" {
  description = "The API's internal ingress."
  value       = azurerm_container_app.main["api"].ingress[0].fqdn
}

output "database_fqdn" {
  description = "PostgreSQL Flexible Server (private)."
  value       = azurerm_postgresql_flexible_server.main.fqdn
}

output "database_app_role" {
  description = "As a database admin, once: SELECT * FROM pgaadauth_create_principal('<this>', false, false); then apply python -m alm_core.store.admin grants --app-role '<this>'."
  value       = azurerm_user_assigned_identity.app.name
}

output "key_vault_uri" {
  description = "Set the secret values here: alm-service-account-password, alm-session-signing-key, alm-oidc-client-secret, alm-webhook-hmac-key."
  value       = azurerm_key_vault.main.vault_uri
}

output "app_identity_client_id" {
  description = "The managed identity the apps run as."
  value       = azurerm_user_assigned_identity.app.client_id
}
