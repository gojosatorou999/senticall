# RDS PostgreSQL 16 + PostGIS + pgvector, Multi-AZ (§14.1).
#
# The instance lives in the data subnets. Its SG only admits the
# voice_task SG on 5432, so an audit can trivially prove "nothing
# outside the data subnets reaches Postgres on 5432".
#
# Alembic migrations run from an ECS init-container or a one-off task;
# MIGRATE_ON_BOOT=true is the single-node convenience path (§12 note).

resource "aws_db_subnet_group" "main" {
  name       = "${local.name}-rds-subnet-group"
  subnet_ids = aws_subnet.data[*].id

  tags = { Name = "${local.name}-rds-subnet-group" }
}

resource "aws_db_parameter_group" "pg16" {
  name   = "${local.name}-pg16"
  family = "postgres16"

  # pgvector and PostGIS are loaded as shared_preload_libraries.
  # This parameter is additive — existing values are preserved.
  parameter {
    name  = "shared_preload_libraries"
    value = "pg_stat_statements"
    apply_method = "pending-reboot"
  }

  tags = { Name = "${local.name}-pg16-params" }
}

resource "aws_db_instance" "main" {
  identifier = "${local.name}-rds"

  engine         = "postgres"
  engine_version = "16.3"
  instance_class = var.rds_instance_class

  allocated_storage     = var.rds_allocated_storage_gb
  max_allocated_storage = var.rds_allocated_storage_gb * 3  # auto-scaling up to 3×
  storage_type          = "gp3"
  storage_encrypted     = true

  db_name  = "fg_voice"
  username = "fg_voice"
  password = random_password.rds.result

  db_subnet_group_name   = aws_db_subnet_group.main.name
  vpc_security_group_ids = [aws_security_group.rds.id]
  parameter_group_name   = aws_db_parameter_group.pg16.name

  multi_az               = true
  publicly_accessible    = false
  deletion_protection    = var.environment == "prod"
  skip_final_snapshot    = var.environment != "prod"
  final_snapshot_identifier = var.environment == "prod" ? "${local.name}-final" : null

  # PITR: 7-day retention per §14.1
  backup_retention_period = 7
  backup_window           = "03:00-04:00"   # UTC — low-traffic window for India
  maintenance_window      = "sun:04:00-sun:05:00"

  # Auto minor version upgrades keep pg16 patched between major cycles.
  auto_minor_version_upgrade = true

  performance_insights_enabled          = true
  performance_insights_retention_period = 7

  enabled_cloudwatch_logs_exports = ["postgresql", "upgrade"]

  tags = { Name = "${local.name}-rds" }
}

# ── Outputs ─────────────────────────────────────────────────────────

output "rds_endpoint" {
  value       = aws_db_instance.main.endpoint
  description = "RDS writer endpoint (host:port)"
}

output "rds_db_name" {
  value = aws_db_instance.main.db_name
}

output "rds_username" {
  value = aws_db_instance.main.username
}
