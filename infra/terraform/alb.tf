# Application Load Balancer — WebSocket + HTTP/HTTPS ingress (§14.2).
#
# Key constraint: idle_timeout MUST be 900 s. The ALB default (60 s)
# silently kills any call longer than 1 minute. A Twilio Media Stream
# is one long-lived WebSocket per call; truncating it mid-report is a
# data-loss bug, not a timeout.

resource "aws_lb" "main" {
  name               = "${local.name}-alb"
  internal           = false
  load_balancer_type = "application"
  security_groups    = [aws_security_group.alb.id]
  subnets            = aws_subnet.public[*].id

  # CRITICAL: must be ≥ 300 s (ECS stopTimeout) + call-end processing.
  # 900 s leaves headroom for the slowest valid call (max_call_duration 300 s +
  # post-call drain) while still terminating genuinely stuck connections.
  idle_timeout = 900

  enable_deletion_protection = var.environment == "prod"

  access_logs {
    bucket  = aws_s3_bucket.alb_logs.id
    prefix  = "alb"
    enabled = true
  }

  tags = { Name = "${local.name}-alb" }
}

# ── Target groups ──────────────────────────────────────────────────

# /ws/media → voice-agent tasks (WebSocket Media Streams)
resource "aws_lb_target_group" "voice_agent" {
  name        = "${local.name}-agent"
  port        = 8080
  protocol    = "HTTP"
  vpc_id      = aws_vpc.main.id
  target_type = "ip"

  health_check {
    enabled             = true
    path                = "/healthz"
    interval            = 15
    timeout             = 5
    healthy_threshold   = 2
    unhealthy_threshold = 3
    matcher             = "200"
  }

  # Match ECS stopTimeout so ALB stops routing to a draining task only
  # after in-flight WebSocket calls have had 300 s to complete.
  deregistration_delay = 300

  tags = { Name = "${local.name}-agent" }
}

# /voice/* + /api/v1/* → webhook + API tasks
resource "aws_lb_target_group" "voice_api" {
  name        = "${local.name}-api"
  port        = 8080
  protocol    = "HTTP"
  vpc_id      = aws_vpc.main.id
  target_type = "ip"

  health_check {
    enabled             = true
    path                = "/healthz"
    interval            = 15
    timeout             = 5
    healthy_threshold   = 2
    unhealthy_threshold = 3
    matcher             = "200"
  }

  deregistration_delay = 30

  tags = { Name = "${local.name}-api" }
}

# ── Listeners ─────────────────────────────────────────────────────

# HTTP → HTTPS redirect (callers should never hit plain HTTP, but
# Twilio's fallback TwiML fetch might land on 80 if misconfigured)
resource "aws_lb_listener" "http" {
  load_balancer_arn = aws_lb.main.arn
  port              = 80
  protocol          = "HTTP"

  default_action {
    type = "redirect"
    redirect {
      port        = "443"
      protocol    = "HTTPS"
      status_code = "HTTP_301"
    }
  }
}

# HTTPS — TLS 1.2+ via a security policy that also supports TLS 1.3.
# Default action routes to the API (covers /api/v1/* and /healthz).
resource "aws_lb_listener" "https" {
  load_balancer_arn = aws_lb.main.arn
  port              = 443
  protocol          = "HTTPS"
  ssl_policy        = "ELBSecurityPolicy-TLS13-1-2-2021-06"
  certificate_arn   = var.acm_certificate_arn

  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.voice_api.arn
  }
}

# WebSocket path → voice-agent (priority 10 = highest so it matches
# before the API catch-all at priority 100)
resource "aws_lb_listener_rule" "ws_media" {
  listener_arn = aws_lb_listener.https.arn
  priority     = 10

  action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.voice_agent.arn
  }

  condition {
    path_pattern { values = ["/ws/media*"] }
  }
}

# /voice/* → API service (Twilio webhooks).
# Lower priority than ws_media so /ws/media is never accidentally
# caught by this rule.
resource "aws_lb_listener_rule" "voice_webhooks" {
  listener_arn = aws_lb_listener.https.arn
  priority     = 20

  action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.voice_api.arn
  }

  condition {
    path_pattern { values = ["/voice/*"] }
  }
}

# ── Outputs ─────────────────────────────────────────────────────────

output "alb_dns_name" {
  value       = aws_lb.main.dns_name
  description = "Point voice.sentinel.in CNAME here (or use Route53 alias)"
}

output "alb_zone_id" {
  value = aws_lb.main.zone_id
}

output "alb_arn" {
  value = aws_lb.main.arn
}

output "tg_voice_agent_arn" {
  value = aws_lb_target_group.voice_agent.arn
}

output "tg_voice_api_arn" {
  value = aws_lb_target_group.voice_api.arn
}
