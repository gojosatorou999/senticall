# CloudWatch alarms + Managed Grafana + SNS ops topic (§16.1 – §16.3).
#
# Every alarm maps to a metric from §16.2. Page-level alarms publish
# to the ops SNS topic; warn-level alarms publish to a separate topic
# so they don't pollute on-call paging.

# ── SNS topics ──────────────────────────────────────────────────────

resource "aws_sns_topic" "alerts" {
  name = "${local.name}-alerts"
  # KMS key not explicitly set — CMK can be overlaid in prod via a
  # separate tf apply once the KMS infrastructure is in place.
  tags = { Name = "${local.name}-alerts" }
}

resource "aws_sns_topic" "warnings" {
  name = "${local.name}-warnings"
  tags = { Name = "${local.name}-warnings" }
}

# ── Helper: one alarm per metric ────────────────────────────────────

locals {
  # Alarms that PAGE (publish to the alerts topic)
  page_alarms = {
    # §16.2: fg_voice_submission_failures_total > 0 → page. This must be zero.
    submission_failures = {
      alarm_name          = "${local.name}-submission-failures"
      alarm_description   = "Outbox submission failures detected — data loss risk. Investigate immediately."
      metric_name         = "fg_voice_submission_failures_total"
      comparison_operator = "GreaterThanThreshold"
      threshold           = 0
      evaluation_periods  = 1
      period              = 60
      statistic           = "Sum"
      treat_missing_data  = "notBreaching"
    }

    # §16.2: fg_voice_turn_latency_ms p95 > 1200 for 5 min → page
    turn_latency_p95 = {
      alarm_name          = "${local.name}-turn-latency-p95"
      alarm_description   = "Turn latency p95 > 1200 ms for 5 minutes — SLO breach."
      metric_name         = "fg_voice_turn_latency_ms"
      comparison_operator = "GreaterThanThreshold"
      threshold           = 1200
      evaluation_periods  = 5
      period              = 60
      extended_statistic  = "p95"
      treat_missing_data  = "notBreaching"
    }

    # §16.2: fg_voice_csv_lag_seconds > 30 → page
    csv_lag = {
      alarm_name          = "${local.name}-csv-lag"
      alarm_description   = "CSV freshness SLO breached (> 30 s). Projector may be stuck."
      metric_name         = "fg_voice_csv_lag_seconds"
      comparison_operator = "GreaterThanThreshold"
      threshold           = 30
      evaluation_periods  = 2
      period              = 60
      statistic           = "Maximum"
      treat_missing_data  = "notBreaching"
    }

    # §16.2: fg_voice_concurrent_calls > 80% capacity → page
    # The alarm fires when tasks are saturated — before queue build-up.
    # Threshold = 0.8 * (max_tasks * target_calls_per_task)
    capacity_80_pct = {
      alarm_name          = "${local.name}-capacity-80pct"
      alarm_description   = "Voice agent is at > 80% call capacity. Consider surge mode."
      metric_name         = "fg_voice_concurrent_calls"
      comparison_operator = "GreaterThanThreshold"
      threshold           = var.voice_agent_max_tasks * var.target_calls_per_task * 0.8
      evaluation_periods  = 3
      period              = 60
      statistic           = "Maximum"
      treat_missing_data  = "notBreaching"
    }
  }

  # Alarms that WARN (publish to warnings topic only)
  warn_alarms = {
    # §16.2: tts_cache_hit_ratio < 0.70 → warn
    tts_cache_hit = {
      alarm_name          = "${local.name}-tts-cache-hit"
      alarm_description   = "TTS cache hit ratio < 70%. Audio bank may have drifted."
      metric_name         = "fg_voice_tts_cache_hit_ratio"
      comparison_operator = "LessThanThreshold"
      threshold           = 0.7
      evaluation_periods  = 5
      period              = 60
      statistic           = "Average"
      treat_missing_data  = "notBreaching"
    }

    # §16.2: asr_confidence p50 < 0.70 for 30 min → warn
    asr_confidence = {
      alarm_name          = "${local.name}-asr-confidence-low"
      alarm_description   = "ASR confidence p50 < 0.70 for 30 minutes. STT may be degraded."
      metric_name         = "fg_voice_asr_confidence"
      comparison_operator = "LessThanThreshold"
      threshold           = 0.7
      evaluation_periods  = 30
      period              = 60
      statistic           = "p50"
      treat_missing_data  = "notBreaching"
    }

    # §16.2: dtmf_fallback_ratio > 0.25 → investigate
    dtmf_fallback = {
      alarm_name          = "${local.name}-dtmf-fallback-high"
      alarm_description   = "DTMF fallback ratio > 25%. ASR performance degraded."
      metric_name         = "fg_voice_dtmf_fallback_ratio"
      comparison_operator = "GreaterThanThreshold"
      threshold           = 0.25
      evaluation_periods  = 5
      period              = 60
      statistic           = "Average"
      treat_missing_data  = "notBreaching"
    }

    # §16.2: abandoned ratio > 0.20 → investigate
    abandoned_ratio = {
      alarm_name          = "${local.name}-abandoned-ratio"
      alarm_description   = "Call abandoned rate > 20%. Investigate call flow."
      metric_name         = "fg_voice_call_outcome_abandoned_ratio"
      comparison_operator = "GreaterThanThreshold"
      threshold           = 0.2
      evaluation_periods  = 10
      period              = 60
      statistic           = "Average"
      treat_missing_data  = "notBreaching"
    }

    # §16.2: enrichment_dlq_depth > 0 → warn
    enrichment_dlq = {
      alarm_name          = "${local.name}-enrichment-dlq"
      alarm_description   = "Enrichment DLQ depth > 0. Check stuck rows."
      metric_name         = "fg_voice_enrichment_dlq_depth"
      comparison_operator = "GreaterThanThreshold"
      threshold           = 0
      evaluation_periods  = 1
      period              = 300
      statistic           = "Maximum"
      treat_missing_data  = "notBreaching"
    }
  }
}

resource "aws_cloudwatch_metric_alarm" "page" {
  for_each = local.page_alarms

  alarm_name          = each.value.alarm_name
  alarm_description   = each.value.alarm_description
  metric_name         = each.value.metric_name
  namespace           = "SentinelVoice"
  comparison_operator = each.value.comparison_operator
  threshold           = each.value.threshold
  evaluation_periods  = each.value.evaluation_periods
  period              = each.value.period
  statistic           = lookup(each.value, "statistic", null)
  extended_statistic  = lookup(each.value, "extended_statistic", null)
  treat_missing_data  = each.value.treat_missing_data

  alarm_actions = [aws_sns_topic.alerts.arn]
  ok_actions    = [aws_sns_topic.alerts.arn]

  dimensions = {
    Environment = var.environment
  }

  tags = { Severity = "page" }
}

resource "aws_cloudwatch_metric_alarm" "warn" {
  for_each = local.warn_alarms

  alarm_name          = each.value.alarm_name
  alarm_description   = each.value.alarm_description
  metric_name         = each.value.metric_name
  namespace           = "SentinelVoice"
  comparison_operator = each.value.comparison_operator
  threshold           = each.value.threshold
  evaluation_periods  = each.value.evaluation_periods
  period              = each.value.period
  statistic           = lookup(each.value, "statistic", null)
  treat_missing_data  = each.value.treat_missing_data

  alarm_actions = [aws_sns_topic.warnings.arn]

  dimensions = {
    Environment = var.environment
  }

  tags = { Severity = "warn" }
}

# ── Amazon Managed Grafana workspace ────────────────────────────────

resource "aws_grafana_workspace" "main" {
  name                     = "${local.name}-grafana"
  account_access_type      = "CURRENT_ACCOUNT"
  authentication_providers = ["AWS_SSO"]
  permission_type          = "SERVICE_MANAGED"
  data_sources             = ["CLOUDWATCH", "XRAY"]

  role_arn = aws_iam_role.grafana.arn

  tags = { Name = "${local.name}-grafana" }
}

resource "aws_iam_role" "grafana" {
  name = "${local.name}-grafana"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "grafana.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy_attachment" "grafana_cw" {
  role       = aws_iam_role.grafana.name
  policy_arn = "arn:aws:iam::aws:policy/CloudWatchReadOnlyAccess"
}

resource "aws_iam_role_policy_attachment" "grafana_xray" {
  role       = aws_iam_role.grafana.name
  policy_arn = "arn:aws:iam::aws:policy/AWSXRayReadOnlyAccess"
}

# ── Outputs ─────────────────────────────────────────────────────────

output "sns_topic_alerts_arn" {
  value       = aws_sns_topic.alerts.arn
  description = "Subscribe ops paging Lambda or PagerDuty endpoint here"
}
output "sns_topic_warnings_arn" {
  value = aws_sns_topic.warnings.arn
}
output "grafana_endpoint" {
  value = aws_grafana_workspace.main.endpoint
}
