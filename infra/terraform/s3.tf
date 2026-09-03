# S3 buckets (§14.1): recordings, transcripts, reports, rag snapshots.
# ALB access logs get their own bucket (in alb.tf).
#
# Naming includes the account ID so buckets survive across account forks
# without global-namespace collisions.

data "aws_caller_identity" "current" {}
data "aws_elb_service_account" "main" {}

locals {
  acct = data.aws_caller_identity.current.account_id
}

# ── Helper module to avoid repeating the same six blocks ────────────

locals {
  private_buckets = {
    recordings  = "fg-voice-recordings-${local.acct}-${var.environment}"
    transcripts = "fg-voice-transcripts-${local.acct}-${var.environment}"
    reports     = "fg-reports-${local.acct}-${var.environment}"
    rag         = "fg-voice-rag-${local.acct}-${var.environment}"
  }
}

resource "aws_s3_bucket" "app" {
  for_each      = local.private_buckets
  bucket        = each.value
  force_destroy = var.environment != "prod"
  tags          = { Name = each.value, Purpose = each.key }
}

resource "aws_s3_bucket_public_access_block" "app" {
  for_each                = local.private_buckets
  bucket                  = aws_s3_bucket.app[each.key].id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_versioning" "app" {
  for_each = local.private_buckets
  bucket   = aws_s3_bucket.app[each.key].id
  versioning_configuration { status = "Enabled" }
}

# SSE-S3 default (KMS can be overlaid per-bucket below if needed)
resource "aws_s3_bucket_server_side_encryption_configuration" "app" {
  for_each = local.private_buckets
  bucket   = aws_s3_bucket.app[each.key].id
  rule {
    apply_server_side_encryption_by_default { sse_algorithm = "aws:kms" }
    bucket_key_enabled = true
  }
}

# ── Recordings lifecycle: Glacier at 90d, delete at 365d (§17.1) ───

resource "aws_s3_bucket_lifecycle_configuration" "recordings" {
  bucket = aws_s3_bucket.app["recordings"].id

  rule {
    id     = "glacier-and-expire"
    status = "Enabled"
    transition {
      days          = 90
      storage_class = "GLACIER"
    }
    expiration { days = 365 }
  }
}

# ── Reports: no expiry — the CSV is the ops record ──────────────────

resource "aws_s3_bucket_lifecycle_configuration" "rag" {
  bucket = aws_s3_bucket.app["rag"].id

  # Keep only the three most recent snapshot versions to avoid unbounded
  # growth; snapshots are immutable+versioned so rolling back is trivial.
  rule {
    id     = "noncurrent-version-expire"
    status = "Enabled"
    noncurrent_version_expiration { noncurrent_days = 30 }
  }
}

# ── ALB access logs bucket ──────────────────────────────────────────

resource "aws_s3_bucket" "alb_logs" {
  bucket        = "${local.name}-alb-logs-${local.acct}"
  force_destroy = var.environment != "prod"
  tags          = { Name = "${local.name}-alb-logs" }
}

resource "aws_s3_bucket_public_access_block" "alb_logs" {
  bucket                  = aws_s3_bucket.alb_logs.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_policy" "alb_logs" {
  bucket = aws_s3_bucket.alb_logs.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid    = "ALBAccessLogs"
      Effect = "Allow"
      Principal = {
        AWS = "arn:aws:iam::${data.aws_elb_service_account.main.id}:root"
      }
      Action   = "s3:PutObject"
      Resource = "${aws_s3_bucket.alb_logs.arn}/alb/AWSLogs/${local.acct}/*"
    }]
  })
}

# ── Outputs ─────────────────────────────────────────────────────────

output "s3_bucket_recordings" {
  value = aws_s3_bucket.app["recordings"].bucket
}
output "s3_bucket_transcripts" {
  value = aws_s3_bucket.app["transcripts"].bucket
}
output "s3_bucket_reports" {
  value = aws_s3_bucket.app["reports"].bucket
}
output "s3_bucket_rag" {
  value = aws_s3_bucket.app["rag"].bucket
}
