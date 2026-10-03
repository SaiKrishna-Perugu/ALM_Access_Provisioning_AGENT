output "console_url" {
  description = "The console, through the internal ALB. Point the internal DNS name at alb_dns_name."
  value       = "https://${var.api_hostname}"
}

output "alb_dns_name" {
  description = "The internal ALB."
  value       = aws_lb.api.dns_name
}

output "database_address" {
  description = "RDS endpoint (private)."
  value       = aws_db_instance.main.address
}

output "database_iam_user" {
  description = "Create this user once, as the admin: CREATE USER alm_app; GRANT rds_iam TO alm_app; then apply the grants from python -m alm_core.store.admin grants --app-role alm_app."
  value       = local.db_user
}

output "task_role_arn" {
  description = "The role the API and workers run as."
  value       = aws_iam_role.task.arn
}

output "secrets_to_fill" {
  description = "Secrets created empty; an operator sets their values."
  value       = [for s in aws_secretsmanager_secret.app : s.name]
}
