# ElastiCache Redis 7 — session state, TTS cache, outbox spillover (§14.1).
#
# Cluster mode OFF per spec — multi-AZ with automatic failover, 2 nodes
# (primary + one replica). The voice agent only needs simple key/value
# and pub/sub — cluster-mode topology would add complexity with no
# benefit at this scale.

resource "aws_elasticache_subnet_group" "main" {
  name       = "${local.name}-redis-subnet-group"
  subnet_ids = aws_subnet.data[*].id
  tags       = { Name = "${local.name}-redis-subnet-group" }
}

resource "aws_elasticache_parameter_group" "redis7" {
  name   = "${local.name}-redis7"
  family = "redis7"

  # Disable dangerous commands that can nuke the whole keyspace.
  parameter {
    name  = "rename-commands"
    value = "FLUSHALL BANNED FLUSHDB BANNED"
  }

  tags = { Name = "${local.name}-redis7-params" }
}

resource "aws_elasticache_replication_group" "main" {
  replication_group_id = "${local.name}-redis"
  description          = "Sentinel voice-agent session state + TTS cache"

  node_type            = var.redis_node_type
  num_cache_clusters   = 2      # 1 primary + 1 replica
  port                 = 6379
  parameter_group_name = aws_elasticache_parameter_group.redis7.name
  subnet_group_name    = aws_elasticache_subnet_group.main.name
  security_group_ids   = [aws_security_group.redis.id]

  engine_version = "7.1"

  automatic_failover_enabled = true
  multi_az_enabled           = true

  at_rest_encryption_enabled = true
  transit_encryption_enabled = true
  # TLS with the default self-signed cert; no auth token because
  # network-level isolation (SG) is the primary control.
  auth_token = null

  snapshot_retention_limit = 3
  snapshot_window          = "02:00-03:00"  # UTC low-traffic

  auto_minor_version_upgrade = true

  log_delivery_configuration {
    destination      = aws_cloudwatch_log_group.redis.name
    destination_type = "cloudwatch-logs"
    log_format       = "json"
    log_type         = "slow-log"
  }

  tags = { Name = "${local.name}-redis" }
}

resource "aws_cloudwatch_log_group" "redis" {
  name              = "/fg-voice/${var.environment}/redis-slow-log"
  retention_in_days = 30
}

# ── Outputs ─────────────────────────────────────────────────────────

output "redis_primary_endpoint" {
  value       = aws_elasticache_replication_group.main.primary_endpoint_address
  description = "Redis primary endpoint — used in DATABASE_URL / REDIS_URL"
}

output "redis_reader_endpoint" {
  value = aws_elasticache_replication_group.main.reader_endpoint_address
}

output "redis_port" {
  value = aws_elasticache_replication_group.main.port
}
