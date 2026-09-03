# WAF v2 on the ALB (§14.1 + §17.3).
#
# Two protection layers:
# 1. Rate limiting — 100 requests per 5-minute window per source IP
#    on all paths (blocks port-scan / abuse bots).
# 2. Twilio allowlist on /voice/* — only Twilio egress CIDRs can POST
#    to webhook endpoints. Any other source that somehow bypasses the
#    signature check still gets dropped here first.
#
# WAF is regional (ALB-facing), not CloudFront-facing.

resource "aws_wafv2_ip_set" "twilio_egress" {
  name               = "${local.name}-twilio-egress"
  scope              = "REGIONAL"
  ip_address_version = "IPV4"
  addresses          = var.twilio_egress_cidrs

  tags = { Name = "${local.name}-twilio-egress" }
}

resource "aws_wafv2_web_acl" "main" {
  name  = "${local.name}-waf"
  scope = "REGIONAL"

  default_action { allow {} }

  # Rule 1: block /voice/* from non-Twilio IPs.
  # Priority 10 — evaluated first, before the rate-limit rule.
  rule {
    name     = "twilio-webhook-allowlist"
    priority = 10

    action { block {} }

    statement {
      and_statement {
        statement {
          byte_match_statement {
            search_string         = "/voice/"
            field_to_match { uri_path {} }
            text_transformation { priority = 0; type = "LOWERCASE" }
            positional_constraint = "STARTS_WITH"
          }
        }
        statement {
          not_statement {
            statement {
              ip_set_reference_statement {
                arn = aws_wafv2_ip_set.twilio_egress.arn
              }
            }
          }
        }
      }
    }

    visibility_config {
      cloudwatch_metrics_enabled = true
      metric_name                = "${local.name}-twilio-allowlist"
      sampled_requests_enabled   = true
    }
  }

  # Rule 2: rate-limit all paths — 100 requests per 5 min per IP.
  # Priority 20 — runs after the allowlist rule.
  rule {
    name     = "rate-limit-global"
    priority = 20

    action { block {} }

    statement {
      rate_based_statement {
        limit              = 100
        aggregate_key_type = "IP"
      }
    }

    visibility_config {
      cloudwatch_metrics_enabled = true
      metric_name                = "${local.name}-rate-limit"
      sampled_requests_enabled   = true
    }
  }

  # Rule 3: AWS managed rules — core rule set for OWASP Top 10.
  # Priority 30. Kept in Count mode for the first week after deploy
  # so genuine traffic patterns can be audited before switching to Block.
  rule {
    name     = "aws-managed-core"
    priority = 30

    override_action { count {} }

    statement {
      managed_rule_group_statement {
        name        = "AWSManagedRulesCommonRuleSet"
        vendor_name = "AWS"
      }
    }

    visibility_config {
      cloudwatch_metrics_enabled = true
      metric_name                = "${local.name}-core-rules"
      sampled_requests_enabled   = true
    }
  }

  visibility_config {
    cloudwatch_metrics_enabled = true
    metric_name                = "${local.name}-waf"
    sampled_requests_enabled   = true
  }

  tags = { Name = "${local.name}-waf" }
}

resource "aws_wafv2_web_acl_association" "alb" {
  resource_arn = aws_lb.main.arn
  web_acl_arn  = aws_wafv2_web_acl.main.arn
}

output "waf_web_acl_arn" {
  value = aws_wafv2_web_acl.main.arn
}
