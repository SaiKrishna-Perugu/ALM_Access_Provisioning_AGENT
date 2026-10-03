// ALM Access Provisioning - AWS. A skeleton: the same shape as infra/gcp,
// validated in CI, to be completed and applied when the client chooses AWS.
//
//   API     ECS Fargate service behind an internal ALB (HTTPS); sign-in is the
//           application's own OIDC (ALM_AUTH_MODE=oidc).
//   Workers ECS Fargate service, no load balancer; /healthz for the probe.
//   State   RDS for PostgreSQL 16, IAM database authentication, PITR, KMS.
//   Secrets Secrets Manager, read by name through the task role.
//   Models  Bedrock through the task role (ALM_LLM_PROVIDER=bedrock).
//
// The network is the client's: this configuration takes an existing VPC,
// private subnets and route tables, and never creates or changes the link to
// the corporate estate (Direct Connect or VPN). Nothing here is public.
//
//   terraform init -backend-config=...
//   terraform apply -var-file=envs/test.tfvars

terraform {
  required_version = ">= 1.6"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 5.80, < 7.0"
    }
  }

  backend "s3" {
    // bucket, key, region and dynamodb_table supplied by `terraform init -backend-config=...`
  }
}

provider "aws" {
  region = var.region
  default_tags {
    tags = merge(var.tags, { app = "alm-provisioning", environment = var.environment })
  }
}

locals {
  prefix    = "alm-${var.environment}"
  db_user   = "alm_app"
  is_prod   = var.environment == "prod"
  log_group = "/alm/${var.environment}"

  // Every setting the API and the workers share; the same names as on GCP.
  run_env = {
    ALM_ENVIRONMENT             = upper(var.environment)
    ALM_SHADOW_MODE             = tostring(var.shadow_mode)
    ALM_ORCHESTRATION           = var.orchestration
    ALM_LLM_PROVIDER            = "bedrock"
    ALM_AGENT_MODEL             = var.agent_model
    ALM_SUPERVISOR_MODEL        = var.supervisor_model
    AWS_REGION                  = var.region
    EWM_SERVER                  = var.ewm_server
    JTS_SERVER                  = var.jts_server
    CID                         = var.service_account_cid
    ALM_SECRET_BACKEND          = "aws" // pragma: allowlist secret - names the secret store
    ALM_DB_AUTH                 = "aws_iam"
    ALM_POSTGRES_DSN            = "postgresql://${local.db_user}@${aws_db_instance.main.address}:5432/alm?sslmode=require"
    ALM_AD_JOB_TRANSPORT        = "store"
    ALM_AUTH_MODE               = "oidc"
    ALM_OIDC_ISSUER             = var.oidc_issuer
    ALM_OIDC_CLIENT_ID          = var.oidc_client_id
    ALM_ROLE_MAP                = var.role_map
    ALM_RETENTION_DAYS          = tostring(var.retention_days)
    ALM_OTEL_ENABLED            = tostring(var.otel_endpoint != "")
    ALM_CA_BUNDLE               = "/etc/ssl/certs/corporate-ca.pem"
    ALM_APPROVAL_BASE_URL       = "https://${var.api_hostname}"
    OTEL_EXPORTER_OTLP_ENDPOINT = var.otel_endpoint
    // The secrets below are per environment; the application looks them up by these names.
    ALM_PASSWORD_SECRET_NAME    = "${local.prefix}/alm-service-account-password"
    ALM_SESSION_SECRET_NAME     = "${local.prefix}/alm-session-signing-key"
    ALM_OIDC_CLIENT_SECRET_NAME = "${local.prefix}/alm-oidc-client-secret"
    ALM_WEBHOOK_SECRET_NAME     = "${local.prefix}/alm-webhook-hmac-key"
  }
}

data "aws_caller_identity" "current" {}

// ------------------------------------------------------------ security groups
resource "aws_security_group" "alb" {
  name        = "${local.prefix}-alb"
  description = "Internal ALB: HTTPS from the corporate ranges only"
  vpc_id      = var.vpc_id

  ingress {
    description = "HTTPS from the corporate network"
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = var.corporate_cidrs
  }
  egress {
    description = "To the API tasks"
    from_port   = 8080
    to_port     = 8080
    protocol    = "tcp"
    cidr_blocks = var.private_subnet_cidrs
  }
}

resource "aws_security_group" "tasks" {
  name        = "${local.prefix}-tasks"
  description = "API and worker tasks: in from the ALB only; out to the VPC and the estate"
  vpc_id      = var.vpc_id

  ingress {
    description     = "The ALB to the API"
    from_port       = 8080
    to_port         = 8080
    protocol        = "tcp"
    security_groups = [aws_security_group.alb.id]
  }
  egress {
    description = "HTTPS to EWM, JTS, the VPC endpoints and the directory"
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = concat(var.private_subnet_cidrs, var.corporate_cidrs)
  }
  egress {
    description = "Postgres"
    from_port   = 5432
    to_port     = 5432
    protocol    = "tcp"
    cidr_blocks = var.private_subnet_cidrs
  }
}

resource "aws_security_group" "db" {
  name        = "${local.prefix}-db"
  description = "Postgres: from the tasks only"
  vpc_id      = var.vpc_id

  ingress {
    description     = "Postgres from the tasks"
    from_port       = 5432
    to_port         = 5432
    protocol        = "tcp"
    security_groups = [aws_security_group.tasks.id]
  }
}

resource "aws_security_group" "endpoints" {
  name        = "${local.prefix}-endpoints"
  description = "Interface VPC endpoints: HTTPS from the tasks"
  vpc_id      = var.vpc_id

  ingress {
    description     = "HTTPS from the tasks"
    from_port       = 443
    to_port         = 443
    protocol        = "tcp"
    security_groups = [aws_security_group.tasks.id]
  }
}

// ------------------------------------------------------------ VPC endpoints
// AWS APIs over private endpoints: no NAT, no internet path.
resource "aws_vpc_endpoint" "interface" {
  for_each            = toset(["secretsmanager", "logs", "ecr.api", "ecr.dkr", "bedrock-runtime", "kms"])
  vpc_id              = var.vpc_id
  service_name        = "com.amazonaws.${var.region}.${each.value}"
  vpc_endpoint_type   = "Interface"
  subnet_ids          = var.private_subnet_ids
  security_group_ids  = [aws_security_group.endpoints.id]
  private_dns_enabled = true
}

resource "aws_vpc_endpoint" "s3" {
  vpc_id            = var.vpc_id
  service_name      = "com.amazonaws.${var.region}.s3"
  vpc_endpoint_type = "Gateway"
  route_table_ids   = var.private_route_table_ids
}

// ----------------------------------------------------------------- database
resource "aws_db_subnet_group" "main" {
  name       = "${local.prefix}-db"
  subnet_ids = var.private_subnet_ids
}

resource "aws_db_instance" "main" {
  identifier     = "${local.prefix}-pg"
  engine         = "postgres"
  engine_version = "16"
  instance_class = var.db_instance_class
  db_name        = "alm"

  allocated_storage     = 50
  max_allocated_storage = 500
  storage_type          = "gp3"
  storage_encrypted     = true
  kms_key_id            = var.db_kms_key_arn != "" ? var.db_kms_key_arn : null

  // The master password is generated and kept by RDS in Secrets Manager; the
  // application never uses it. It signs in as alm_app with an IAM token.
  username                            = "alm_admin"
  manage_master_user_password         = true
  iam_database_authentication_enabled = true

  db_subnet_group_name   = aws_db_subnet_group.main.name
  vpc_security_group_ids = [aws_security_group.db.id]
  publicly_accessible    = false
  multi_az               = local.is_prod

  backup_retention_period   = 35
  backup_window             = "02:00-03:00"
  maintenance_window        = "sun:03:30-sun:04:30"
  deletion_protection       = local.is_prod
  skip_final_snapshot       = !local.is_prod
  final_snapshot_identifier = local.is_prod ? "${local.prefix}-pg-final" : null
  copy_tags_to_snapshot     = true
}

// ------------------------------------------------------------------ secrets
// Created empty; the values are set out of band (never in Terraform state).
resource "aws_secretsmanager_secret" "app" {
  for_each = toset([
    "alm-service-account-password", "alm-session-signing-key",
    "alm-oidc-client-secret", "alm-webhook-hmac-key",
  ])
  name        = "${local.prefix}/${each.value}"
  description = "ALM ${each.value}. Value set by an operator, never by Terraform."
}

// --------------------------------------------------------------------- IAM
data "aws_iam_policy_document" "tasks_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ecs-tasks.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "execution" {
  name               = "${local.prefix}-execution"
  assume_role_policy = data.aws_iam_policy_document.tasks_assume.json
}

resource "aws_iam_role_policy_attachment" "execution" {
  role       = aws_iam_role.execution.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

resource "aws_iam_role" "task" {
  name               = "${local.prefix}-task"
  assume_role_policy = data.aws_iam_policy_document.tasks_assume.json
}

data "aws_iam_policy_document" "task" {
  statement {
    sid       = "ReadOwnSecrets"
    actions   = ["secretsmanager:GetSecretValue"]
    resources = [for s in aws_secretsmanager_secret.app : s.arn]
  }
  statement {
    sid       = "DatabaseIamLogin"
    actions   = ["rds-db:connect"]
    resources = ["arn:aws:rds-db:${var.region}:${data.aws_caller_identity.current.account_id}:dbuser:${aws_db_instance.main.resource_id}/${local.db_user}"]
  }
  statement {
    sid       = "Models"
    actions   = ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream", "bedrock:Converse", "bedrock:ConverseStream"]
    resources = var.bedrock_model_arns
  }
}

resource "aws_iam_role_policy" "task" {
  name   = "${local.prefix}-task"
  role   = aws_iam_role.task.id
  policy = data.aws_iam_policy_document.task.json
}

// --------------------------------------------------------------------- ECS
resource "aws_cloudwatch_log_group" "main" {
  name              = local.log_group
  retention_in_days = 30
}

resource "aws_ecs_cluster" "main" {
  name = local.prefix
  setting {
    name  = "containerInsights"
    value = "enabled"
  }
}

locals {
  // One container definition per service; the root filesystem is read-only
  // and /tmp is the only writable path, as in every image target.
  containers = {
    api = {
      image   = var.api_image
      env     = merge(local.run_env, { ALM_WORKER_CONCURRENCY = "0" })
      command = null
    }
    worker = {
      image   = var.worker_image
      env     = merge(local.run_env, { ALM_WORKER_CONCURRENCY = tostring(var.worker_concurrency), ALM_WORKER_HEALTH_PORT = "8080" })
      command = ["python", "-m", "alm_agents.worker"]
    }
  }
}

resource "aws_ecs_task_definition" "main" {
  for_each                 = local.containers
  family                   = "${local.prefix}-${each.key}"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = 1024
  memory                   = 4096
  execution_role_arn       = aws_iam_role.execution.arn
  task_role_arn            = aws_iam_role.task.arn

  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = "X86_64"
  }

  volume {
    name = "tmp"
  }

  container_definitions = jsonencode([{
    name                   = each.key
    image                  = each.value.image
    essential              = true
    command                = each.value.command
    readonlyRootFilesystem = true
    user                   = "10001"
    portMappings           = [{ containerPort = 8080, protocol = "tcp" }]
    mountPoints            = [{ sourceVolume = "tmp", containerPath = "/tmp", readOnly = false }]
    environment            = [for k, v in each.value.env : { name = k, value = v }]
    healthCheck = {
      command     = ["CMD-SHELL", "curl -fsS http://127.0.0.1:8080/healthz || exit 1"]
      interval    = 30
      timeout     = 5
      retries     = 3
      startPeriod = 60
    }
    logConfiguration = {
      logDriver = "awslogs"
      options = {
        awslogs-group         = local.log_group
        awslogs-region        = var.region
        awslogs-stream-prefix = each.key
      }
    }
    // A worker drains on SIGTERM; this is how long ECS waits before SIGKILL.
    stopTimeout = 120
  }])
}

resource "aws_ecs_service" "api" {
  name            = "${local.prefix}-api"
  cluster         = aws_ecs_cluster.main.id
  task_definition = aws_ecs_task_definition.main["api"].arn
  desired_count   = var.api_count
  launch_type     = "FARGATE"

  network_configuration {
    subnets          = var.private_subnet_ids
    security_groups  = [aws_security_group.tasks.id]
    assign_public_ip = false
  }

  load_balancer {
    target_group_arn = aws_lb_target_group.api.arn
    container_name   = "api"
    container_port   = 8080
  }

  depends_on = [aws_lb_listener.https]
}

resource "aws_ecs_service" "worker" {
  name            = "${local.prefix}-worker"
  cluster         = aws_ecs_cluster.main.id
  task_definition = aws_ecs_task_definition.main["worker"].arn
  desired_count   = var.worker_count
  launch_type     = "FARGATE"

  network_configuration {
    subnets          = var.private_subnet_ids
    security_groups  = [aws_security_group.tasks.id]
    assign_public_ip = false
  }
}

// --------------------------------------------------------------- internal ALB
resource "aws_lb" "api" {
  name                       = "${local.prefix}-api"
  internal                   = true
  load_balancer_type         = "application"
  security_groups            = [aws_security_group.alb.id]
  subnets                    = var.private_subnet_ids
  drop_invalid_header_fields = true
}

resource "aws_lb_target_group" "api" {
  name        = "${local.prefix}-api"
  port        = 8080
  protocol    = "HTTP"
  target_type = "ip"
  vpc_id      = var.vpc_id

  health_check {
    path    = "/healthz"
    matcher = "200"
  }
}

resource "aws_lb_listener" "https" {
  load_balancer_arn = aws_lb.api.arn
  port              = 443
  protocol          = "HTTPS"
  ssl_policy        = "ELBSecurityPolicy-TLS13-1-2-2021-06"
  certificate_arn   = var.certificate_arn

  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.api.arn
  }
}
