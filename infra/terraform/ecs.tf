# ECS cluster + task definitions + services (§14.1 + §14.3).
#
# Four services:
#   fg-voice-agent     — WebSocket media stream workers (2 vCPU / 4 GB)
#   fg-voice-api       — webhook svc + reports API (0.5 vCPU / 1 GB)
#   fg-csv-projector   — desiredCount=1 single-writer CSV projector
#   fg-flows           — Prefect enrichment worker (scales on queue depth)
#
# Graceful drain: ECS sends SIGTERM → stopTimeout: 300 → SIGKILL.
# main.py's SIGTERM handler calls mark_draining() so /voice/inbound
# stops accepting new calls; in-flight WebSockets complete naturally.

resource "aws_ecs_cluster" "main" {
  name = "${local.name}"

  setting {
    name  = "containerInsights"
    value = "enabled"
  }

  tags = { Name = "${local.name}" }
}

resource "aws_ecs_cluster_capacity_providers" "main" {
  cluster_name       = aws_ecs_cluster.main.name
  capacity_providers = ["FARGATE", "FARGATE_SPOT"]

  default_capacity_provider_strategy {
    capacity_provider = "FARGATE"
    weight            = 1
    base              = var.voice_agent_min_tasks
  }
}

# ── CloudWatch log groups ────────────────────────────────────────────

resource "aws_cloudwatch_log_group" "agent" {
  name              = "/fg-voice/${var.environment}/agent"
  retention_in_days = 30
}
resource "aws_cloudwatch_log_group" "api" {
  name              = "/fg-voice/${var.environment}/api"
  retention_in_days = 30
}
resource "aws_cloudwatch_log_group" "projector" {
  name              = "/fg-voice/${var.environment}/projector"
  retention_in_days = 14
}
resource "aws_cloudwatch_log_group" "flows" {
  name              = "/fg-voice/${var.environment}/flows"
  retention_in_days = 30
}

# ── Common environment shared across services ────────────────────────

locals {
  common_env = [
    { name = "FG_ENV",          value = var.environment },
    { name = "FG_REGION",       value = var.region },
    { name = "FG_AGENT_VERSION", value = var.image_tag },
    { name = "FG_LOG_LEVEL",    value = "INFO" },
    { name = "RELAY_ENABLED",   value = "true" },
    { name = "CSV_ENABLED",     value = "true" },
    { name = "ALERTS_ENABLED",  value = "true" },
    { name = "ENRICHMENT_ENABLED", value = "true" },
    { name = "EXTRACTOR_TYPE",  value = "claude" },
    { name = "GEOCODER_TYPE",   value = "json_gazetteer" },
    { name = "DEDUPE_TYPE",     value = "text_window" },
    { name = "EFS_CSV_PATH",    value = "/mnt/efs/reports" },
    { name = "MIGRATE_ON_BOOT", value = "false" },
    { name = "SMS_PIN_OFFER_ENABLED", value = "true" },
    {
      name  = "SMS_PIN_OFFER_BASE_URL"
      value = "https://${var.domain_name}"
    },
    {
      name  = "OTEL_EXPORTER_OTLP_ENDPOINT"
      value = "http://localhost:4318"   # OTel sidecar
    },
    {
      name  = "OTEL_SERVICE_NAME"
      value = "fg-voice-${var.environment}"
    },
  ]

  common_secrets = [
    {
      name      = "DATABASE_URL"
      valueFrom = "${aws_secretsmanager_secret.app["rds_password"].arn}:password::"
    },
    {
      name      = "TWILIO_AUTH_TOKEN"
      valueFrom = aws_secretsmanager_secret.app["twilio_auth_token"].arn
    },
    {
      name      = "DEEPGRAM_API_KEY"
      valueFrom = aws_secretsmanager_secret.app["deepgram_api_key"].arn
    },
    {
      name      = "TTS_API_KEY"
      valueFrom = aws_secretsmanager_secret.app["tts_api_key"].arn
    },
    {
      name      = "CALLER_HASH_PEPPER"
      valueFrom = aws_secretsmanager_secret.app["caller_hash_pepper"].arn
    },
    {
      name      = "ADMIN_API_KEY"
      valueFrom = aws_secretsmanager_secret.app["admin_api_key"].arn
    },
    {
      name      = "ANTHROPIC_API_KEY"
      valueFrom = aws_secretsmanager_secret.app["anthropic_api_key"].arn
    },
  ]

  # OTel collector sidecar — forwards OTLP → CloudWatch EMF + X-Ray.
  otel_sidecar = {
    name      = "otel-collector"
    image     = "public.ecr.aws/aws-observability/aws-otel-collector:latest"
    essential = false
    command   = ["--config=/etc/ecs/ecs-xray.yaml"]
    portMappings = [{
      containerPort = 4318
      protocol      = "tcp"
    }]
    logConfiguration = {
      logDriver = "awslogs"
      options = {
        "awslogs-group"         = "/fg-voice/${var.environment}/otel-sidecar"
        "awslogs-region"        = var.region
        "awslogs-stream-prefix" = "otel"
      }
    }
  }
}

resource "aws_cloudwatch_log_group" "otel_sidecar" {
  name              = "/fg-voice/${var.environment}/otel-sidecar"
  retention_in_days = 7
}

# ── Task definition: voice-agent ─────────────────────────────────────

resource "aws_ecs_task_definition" "agent" {
  family                   = "${local.name}-agent"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = var.voice_agent_cpu
  memory                   = var.voice_agent_memory
  execution_role_arn       = aws_iam_role.ecs_execution.arn
  task_role_arn            = aws_iam_role.task_agent.arn

  # EFS for shared CSV writes (CSV projector desiredCount=1, but agent
  # needs the mount to serve /pin/* web form correctly)
  volume {
    name = "efs-reports"
    efs_volume_configuration {
      file_system_id          = aws_efs_file_system.csv.id
      transit_encryption      = "ENABLED"
      authorization_config {
        access_point_id = aws_efs_access_point.reports.id
        iam             = "ENABLED"
      }
    }
  }

  container_definitions = jsonencode([
    {
      name      = "fg-voice-agent"
      image     = "${aws_ecr_repository.app["agent"].repository_url}:${var.image_tag}"
      essential = true
      portMappings = [{ containerPort = 8080, protocol = "tcp" }]

      environment = concat(local.common_env, [
        { name = "S3_RECORDING_ENABLED",  value = "true" },
        { name = "S3_RECORDINGS_BUCKET",  value = aws_s3_bucket.app["recordings"].bucket },
        { name = "S3_TRANSCRIPTS_BUCKET", value = aws_s3_bucket.app["transcripts"].bucket },
        { name = "S3_REPORTS_BUCKET",     value = aws_s3_bucket.app["reports"].bucket },
        { name = "S3_RAG_BUCKET",         value = aws_s3_bucket.app["rag"].bucket },
        { name = "REDIS_URL",             value = "rediss://${aws_elasticache_replication_group.main.primary_endpoint_address}:6379/0" },
        { name = "ALERT_SNS_TOPIC_ARN",   value = aws_sns_topic.alerts.arn },
        { name = "PUBLIC_WSS_BASE",       value = "wss://${var.domain_name}" },
      ])

      secrets = local.common_secrets

      mountPoints = [{
        containerPath = "/mnt/efs/reports"
        sourceVolume  = "efs-reports"
        readOnly      = false
      }]

      logConfiguration = {
        logDriver = "awslogs"
        options = {
          "awslogs-group"         = aws_cloudwatch_log_group.agent.name
          "awslogs-region"        = var.region
          "awslogs-stream-prefix" = "agent"
        }
      }

      healthCheck = {
        command     = ["CMD-SHELL", "curl -sf http://localhost:8080/healthz || exit 1"]
        interval    = 15
        timeout     = 5
        retries     = 3
        startPeriod = 30
      }

      # SIGTERM lands here; stopTimeout (on the service) gives 300 s
      # for in-flight calls to finish before SIGKILL.
      stopTimeout = 300
    },
    local.otel_sidecar,
  ])
}

# ── Task definition: voice-api ───────────────────────────────────────

resource "aws_ecs_task_definition" "api" {
  family                   = "${local.name}-api"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = var.voice_api_cpu
  memory                   = var.voice_api_memory
  execution_role_arn       = aws_iam_role.ecs_execution.arn
  task_role_arn            = aws_iam_role.task_api.arn

  volume {
    name = "efs-reports"
    efs_volume_configuration {
      file_system_id     = aws_efs_file_system.csv.id
      transit_encryption = "ENABLED"
      authorization_config {
        access_point_id = aws_efs_access_point.reports.id
        iam             = "ENABLED"
      }
    }
  }

  container_definitions = jsonencode([
    {
      name      = "fg-voice-api"
      image     = "${aws_ecr_repository.app["api"].repository_url}:${var.image_tag}"
      essential = true
      portMappings = [{ containerPort = 8080, protocol = "tcp" }]

      environment = concat(local.common_env, [
        { name = "S3_REPORTS_BUCKET",  value = aws_s3_bucket.app["reports"].bucket },
        { name = "REDIS_URL",          value = "rediss://${aws_elasticache_replication_group.main.primary_endpoint_address}:6379/0" },
        { name = "ALERT_SNS_TOPIC_ARN", value = aws_sns_topic.alerts.arn },
        { name = "PUBLIC_WSS_BASE",    value = "wss://${var.domain_name}" },
      ])

      secrets = local.common_secrets

      mountPoints = [{
        containerPath = "/mnt/efs/reports"
        sourceVolume  = "efs-reports"
        readOnly      = false
      }]

      logConfiguration = {
        logDriver = "awslogs"
        options = {
          "awslogs-group"         = aws_cloudwatch_log_group.api.name
          "awslogs-region"        = var.region
          "awslogs-stream-prefix" = "api"
        }
      }

      healthCheck = {
        command     = ["CMD-SHELL", "curl -sf http://localhost:8080/healthz || exit 1"]
        interval    = 15
        timeout     = 5
        retries     = 3
        startPeriod = 30
      }

      stopTimeout = 30
    },
    local.otel_sidecar,
  ])
}

# ── Task definition: csv-projector ───────────────────────────────────

resource "aws_ecs_task_definition" "projector" {
  family                   = "${local.name}-projector"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = 256   # 0.25 vCPU
  memory                   = 512
  execution_role_arn       = aws_iam_role.ecs_execution.arn
  task_role_arn            = aws_iam_role.task_projector.arn

  volume {
    name = "efs-reports"
    efs_volume_configuration {
      file_system_id     = aws_efs_file_system.csv.id
      transit_encryption = "ENABLED"
      authorization_config {
        access_point_id = aws_efs_access_point.reports.id
        iam             = "ENABLED"
      }
    }
  }

  container_definitions = jsonencode([{
    name      = "fg-csv-projector"
    image     = "${aws_ecr_repository.app["api"].repository_url}:${var.image_tag}"
    essential = true

    environment = concat(local.common_env, [
      { name = "S3_REPORTS_BUCKET", value = aws_s3_bucket.app["reports"].bucket },
      { name = "REDIS_URL",         value = "rediss://${aws_elasticache_replication_group.main.primary_endpoint_address}:6379/0" },
      { name = "RELAY_ENABLED",     value = "true" },
      { name = "CSV_ENABLED",       value = "true" },
    ])

    secrets = local.common_secrets

    mountPoints = [{
      containerPath = "/mnt/efs/reports"
      sourceVolume  = "efs-reports"
      readOnly      = false
    }]

    logConfiguration = {
      logDriver = "awslogs"
      options = {
        "awslogs-group"         = aws_cloudwatch_log_group.projector.name
        "awslogs-region"        = var.region
        "awslogs-stream-prefix" = "projector"
      }
    }

    stopTimeout = 60
  }])
}

# ── Task definition: flows worker ────────────────────────────────────

resource "aws_ecs_task_definition" "flows" {
  family                   = "${local.name}-flows"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = 1024   # 1 vCPU — enrichment LLM calls can be bursty
  memory                   = 2048
  execution_role_arn       = aws_iam_role.ecs_execution.arn
  task_role_arn            = aws_iam_role.task_flows.arn

  container_definitions = jsonencode([{
    name      = "fg-flows"
    image     = "${aws_ecr_repository.app["flows"].repository_url}:${var.image_tag}"
    essential = true

    environment = concat(local.common_env, [
      { name = "S3_RAG_BUCKET",     value = aws_s3_bucket.app["rag"].bucket },
      { name = "S3_REPORTS_BUCKET", value = aws_s3_bucket.app["reports"].bucket },
      { name = "REDIS_URL",         value = "rediss://${aws_elasticache_replication_group.main.primary_endpoint_address}:6379/0" },
    ])

    secrets = local.common_secrets

    logConfiguration = {
      logDriver = "awslogs"
      options = {
        "awslogs-group"         = aws_cloudwatch_log_group.flows.name
        "awslogs-region"        = var.region
        "awslogs-stream-prefix" = "flows"
      }
    }

    stopTimeout = 120
  }])
}

# ── ECS Services ────────────────────────────────────────────────────

resource "aws_ecs_service" "agent" {
  name            = "${local.name}-agent"
  cluster         = aws_ecs_cluster.main.id
  task_definition = aws_ecs_task_definition.agent.arn
  desired_count   = var.voice_agent_min_tasks
  launch_type     = "FARGATE"

  # Rolling deploy: replace one task at a time; do not go below
  # min_tasks capacity during a deploy (zero dropped calls).
  deployment_minimum_healthy_percent = 100
  deployment_maximum_percent         = 200

  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }

  network_configuration {
    subnets          = aws_subnet.private[*].id
    security_groups  = [aws_security_group.voice_task.id]
    assign_public_ip = false
  }

  load_balancer {
    target_group_arn = aws_lb_target_group.voice_agent.arn
    container_name   = "fg-voice-agent"
    container_port   = 8080
  }

  # Do not let Terraform reset desired_count when autoscaling has
  # moved it — only manage the floor via min_capacity in autoscaling.tf.
  lifecycle {
    ignore_changes = [desired_count]
  }

  depends_on = [aws_lb_listener.https]

  tags = { Name = "${local.name}-agent" }
}

resource "aws_ecs_service" "api" {
  name            = "${local.name}-api"
  cluster         = aws_ecs_cluster.main.id
  task_definition = aws_ecs_task_definition.api.arn
  desired_count   = 2   # min 2 per §14.1
  launch_type     = "FARGATE"

  deployment_minimum_healthy_percent = 100
  deployment_maximum_percent         = 200

  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }

  network_configuration {
    subnets         = aws_subnet.private[*].id
    security_groups = [aws_security_group.voice_task.id]
    assign_public_ip = false
  }

  load_balancer {
    target_group_arn = aws_lb_target_group.voice_api.arn
    container_name   = "fg-voice-api"
    container_port   = 8080
  }

  depends_on = [aws_lb_listener.https]

  tags = { Name = "${local.name}-api" }
}

# CSV projector: desiredCount=1, Redis leader lock prevents two writers
# if a deploy briefly overlaps. Circuit breaker is off — a restart
# cycle is preferable to rolling back the image.
resource "aws_ecs_service" "projector" {
  name            = "${local.name}-projector"
  cluster         = aws_ecs_cluster.main.id
  task_definition = aws_ecs_task_definition.projector.arn
  desired_count   = 1
  launch_type     = "FARGATE"

  deployment_minimum_healthy_percent = 0   # OK to have 0 tasks briefly on deploy
  deployment_maximum_percent         = 100

  network_configuration {
    subnets         = aws_subnet.private[*].id
    security_groups = [aws_security_group.voice_task.id]
    assign_public_ip = false
  }

  tags = { Name = "${local.name}-projector" }
}

resource "aws_ecs_service" "flows" {
  name            = "${local.name}-flows"
  cluster         = aws_ecs_cluster.main.id
  task_definition = aws_ecs_task_definition.flows.arn
  desired_count   = 1
  launch_type     = "FARGATE"

  deployment_minimum_healthy_percent = 0
  deployment_maximum_percent         = 200

  network_configuration {
    subnets         = aws_subnet.private[*].id
    security_groups = [aws_security_group.voice_task.id]
    assign_public_ip = false
  }

  tags = { Name = "${local.name}-flows" }
}

# ── Outputs ─────────────────────────────────────────────────────────

output "ecs_cluster_name" {
  value = aws_ecs_cluster.main.name
}
output "ecs_service_agent" {
  value = aws_ecs_service.agent.name
}
output "ecs_service_api" {
  value = aws_ecs_service.api.name
}
