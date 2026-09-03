# Secrets Manager entries and ECR repositories.
#
# Each secret is a placeholder with an empty initial value — operators
# populate the values after first `terraform apply`. Rotation is
# enabled on the RDS-password secret only (Lambda-managed); all other
# secrets rotate manually via the Secrets Manager console or CLI.
#
# Secret ARNs are exposed as outputs so the ECS task definitions in
# ecs.tf can reference them without circular deps.

resource "random_password" "rds" {
  length           = 32
  special          = true
  override_special = "!#$%&*()-_=+[]{}<>:?"
}

# ── Application secrets ─────────────────────────────────────────────

locals {
  secret_defs = {
    rds_password       = { description = "RDS PostgreSQL master password" }
    twilio_auth_token  = { description = "Twilio Auth Token for webhook validation" }
    deepgram_api_key   = { description = "Deepgram Flux STT API key" }
    tts_api_key        = { description = "TTS provider (Cartesia/Deepgram) API key" }
    llm_fallback_key   = { description = "Anthropic direct API key (Bedrock failover)" }
    caller_hash_pepper = { description = "HMAC-SHA256 pepper — never rotate without re-hashing" }
    admin_api_key      = { description = "X-Admin-Api-Key for /api/v1/reports*" }
    krisp_license_key  = { description = "Krisp noise suppression license" }
    anthropic_api_key  = { description = "Anthropic Claude API key for enrichment extractor" }
  }
}

resource "aws_secretsmanager_secret" "app" {
  for_each    = local.secret_defs
  name        = "/fg-voice/${var.environment}/${each.key}"
  description = each.value.description
  recovery_window_in_days = var.environment == "prod" ? 14 : 0

  tags = { Secret = each.key }
}

# Seed the RDS password so tasks can read it immediately after apply.
# All other secrets are populated out-of-band (Terraform never touches
# third-party API keys in state).
resource "aws_secretsmanager_secret_version" "rds_password" {
  secret_id     = aws_secretsmanager_secret.app["rds_password"].id
  secret_string = random_password.rds.result

  lifecycle {
    # RDS rotation Lambda may update this — don't revert it on the next apply.
    ignore_changes = [secret_string]
  }
}

# ── ECR repositories ────────────────────────────────────────────────

locals {
  ecr_repos = ["agent", "api", "flows"]
}

resource "aws_ecr_repository" "app" {
  for_each             = toset(local.ecr_repos)
  name                 = "fg-voice-${each.key}"
  image_tag_mutability = "MUTABLE"

  image_scanning_configuration { scan_on_push = true }

  tags = { Image = each.key }
}

resource "aws_ecr_lifecycle_policy" "app" {
  for_each   = toset(local.ecr_repos)
  repository = aws_ecr_repository.app[each.key].name

  policy = jsonencode({
    rules = [{
      rulePriority = 1
      description  = "Keep last 20 images"
      selection = {
        tagStatus   = "any"
        countType   = "imageCountMoreThan"
        countNumber = 20
      }
      action = { type = "expire" }
    }]
  })
}

# ── Outputs ─────────────────────────────────────────────────────────

output "secret_arn_rds_password" {
  value = aws_secretsmanager_secret.app["rds_password"].arn
}
output "secret_arn_twilio_auth_token" {
  value = aws_secretsmanager_secret.app["twilio_auth_token"].arn
}
output "secret_arn_deepgram_api_key" {
  value = aws_secretsmanager_secret.app["deepgram_api_key"].arn
}
output "secret_arn_tts_api_key" {
  value = aws_secretsmanager_secret.app["tts_api_key"].arn
}
output "secret_arn_admin_api_key" {
  value = aws_secretsmanager_secret.app["admin_api_key"].arn
}
output "secret_arn_caller_hash_pepper" {
  value = aws_secretsmanager_secret.app["caller_hash_pepper"].arn
}

output "ecr_repository_urls" {
  value = { for k, v in aws_ecr_repository.app : k => v.repository_url }
}

output "rds_password_generated" {
  value     = random_password.rds.result
  sensitive = true
}
