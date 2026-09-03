# ECS autoscaling for the voice-agent service (§14.3).
#
# Scale on the CUSTOM metric `fg_voice_concurrent_calls_per_task`,
# NOT CPU. CPU is a lagging indicator for voice workloads — it scales
# up after callers already heard stutter. The agent emits this metric
# via CloudWatch PutMetricData (obs/metrics.py) on a 10-second period.
#
# The CSV-projector and API services are sized manually (desired_count
# is fixed in ecs.tf). Only the agent service needs autoscaling because
# it's the only service where load is proportional to active calls.

resource "aws_appautoscaling_target" "voice_agent" {
  max_capacity       = var.voice_agent_max_tasks
  min_capacity       = var.voice_agent_min_tasks
  resource_id        = "service/${aws_ecs_cluster.main.name}/${aws_ecs_service.agent.name}"
  scalable_dimension = "ecs:service:DesiredCount"
  service_namespace  = "ecs"
}

# Target-tracking policy: hold concurrent_calls_per_task at the
# configured target (default 8). AWS supplies the scale-in/out
# alarms automatically for target tracking.
resource "aws_appautoscaling_policy" "voice_agent_calls_per_task" {
  name               = "${local.name}-calls-per-task"
  policy_type        = "TargetTrackingScaling"
  resource_id        = aws_appautoscaling_target.voice_agent.resource_id
  scalable_dimension = aws_appautoscaling_target.voice_agent.scalable_dimension
  service_namespace  = aws_appautoscaling_target.voice_agent.service_namespace

  target_tracking_scaling_policy_configuration {
    target_value = var.target_calls_per_task

    # Aggressive scale-out cooldown (30 s) — call volume during a cyclone
    # can spike 50-100× within minutes, and even 30 s is slow. Pair with
    # surge_mode.py for instantaneous floor bumps during weather events.
    scale_in_cooldown  = 300   # conservative — never drop a task with live calls
    scale_out_cooldown = 30

    customized_metric_specification {
      metric_name = "fg_voice_concurrent_calls_per_task"
      namespace   = "SentinelVoice"
      statistic   = "Average"
      unit        = "Count"

      dimensions {
        name  = "ClusterName"
        value = aws_ecs_cluster.main.name
      }
    }
  }
}

# Scheduled action: pre-warm to surge floor during IMD cyclone season
# (June–November). Combined with surge_mode.py for ad-hoc overrides.
resource "aws_appautoscaling_scheduled_action" "cyclone_season_morning" {
  name               = "${local.name}-cyclone-season-prewarm"
  resource_id        = aws_appautoscaling_target.voice_agent.resource_id
  scalable_dimension = aws_appautoscaling_target.voice_agent.scalable_dimension
  service_namespace  = aws_appautoscaling_target.voice_agent.service_namespace

  # 04:00 UTC = 09:30 IST — before the peak reporting window
  schedule = "cron(0 4 * 6-11 ? *)"

  scalable_target_action {
    min_capacity = var.cyclone_season_min_tasks
    max_capacity = var.voice_agent_max_tasks
  }
}

resource "aws_appautoscaling_scheduled_action" "cyclone_season_overnight" {
  name               = "${local.name}-cyclone-season-scale-down"
  resource_id        = aws_appautoscaling_target.voice_agent.resource_id
  scalable_dimension = aws_appautoscaling_target.voice_agent.scalable_dimension
  service_namespace  = aws_appautoscaling_target.voice_agent.service_namespace

  # 23:00 UTC = 04:30 IST — quietest window
  schedule = "cron(0 23 * 6-11 ? *)"

  scalable_target_action {
    min_capacity = var.voice_agent_min_tasks
    max_capacity = var.voice_agent_max_tasks
  }
}
