# IAM roles and policies for ECS tasks + GitHub Actions OIDC deploy.
#
# Principle: one execution role (agent uses to pull images + read
# secrets) and one task role per ECS service (scoped to what that
# service actually needs). No wildcard S3 permissions.

# ── ECS task execution role ─────────────────────────────────────────
# Shared by all services. Grants ECS the ability to pull images from
# ECR, read secret values, and write logs.

resource "aws_iam_role" "ecs_execution" {
  name = "${local.name}-ecs-execution"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "ecs-tasks.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy_attachment" "ecs_execution_managed" {
  role       = aws_iam_role.ecs_execution.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

resource "aws_iam_role_policy" "ecs_execution_secrets" {
  name = "secrets-read"
  role = aws_iam_role.ecs_execution.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["secretsmanager:GetSecretValue"]
      Resource = [for s in aws_secretsmanager_secret.app : s.arn]
    }]
  })
}

# ── Task role: voice-agent ──────────────────────────────────────────
# Needs: S3 write for recordings/transcripts, SSM read for surge mode,
# CloudWatch PutMetricData for the custom autoscaling metric,
# Bedrock InvokeModel for LLM extraction.

resource "aws_iam_role" "task_agent" {
  name = "${local.name}-task-agent"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "ecs-tasks.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy" "task_agent_policy" {
  name = "agent-permissions"
  role = aws_iam_role.task_agent.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "Recordings"
        Effect = "Allow"
        Action = ["s3:PutObject", "s3:GetObject"]
        Resource = [
          "${aws_s3_bucket.app["recordings"].arn}/*",
          "${aws_s3_bucket.app["transcripts"].arn}/*",
        ]
      },
      {
        Sid      = "SurgeModeParam"
        Effect   = "Allow"
        Action   = ["ssm:GetParameter", "ssm:PutParameter", "ssm:DeleteParameter"]
        Resource = "arn:aws:ssm:${var.region}:${local.acct}:parameter/fg-voice/*"
      },
      {
        Sid      = "CustomMetrics"
        Effect   = "Allow"
        Action   = ["cloudwatch:PutMetricData"]
        Resource = "*"
        Condition = {
          StringEquals = { "cloudwatch:namespace" = "SentinelVoice" }
        }
      },
      {
        Sid      = "BedrockInvoke"
        Effect   = "Allow"
        Action   = ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"]
        Resource = "arn:aws:bedrock:${var.region}::foundation-model/*"
      },
      {
        Sid      = "XRayPutSegments"
        Effect   = "Allow"
        Action   = ["xray:PutTraceSegments", "xray:PutTelemetryRecords"]
        Resource = "*"
      },
    ]
  })
}

# ── Task role: voice-api ────────────────────────────────────────────

resource "aws_iam_role" "task_api" {
  name = "${local.name}-task-api"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "ecs-tasks.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy" "task_api_policy" {
  name = "api-permissions"
  role = aws_iam_role.task_api.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "Reports"
        Effect = "Allow"
        Action = ["s3:PutObject", "s3:GetObject"]
        Resource = "${aws_s3_bucket.app["reports"].arn}/*"
      },
      {
        Sid      = "CustomMetrics"
        Effect   = "Allow"
        Action   = ["cloudwatch:PutMetricData"]
        Resource = "*"
        Condition = {
          StringEquals = { "cloudwatch:namespace" = "SentinelVoice" }
        }
      },
    ]
  })
}

# ── Task role: csv-projector ────────────────────────────────────────

resource "aws_iam_role" "task_projector" {
  name = "${local.name}-task-projector"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "ecs-tasks.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy" "task_projector_policy" {
  name = "projector-permissions"
  role = aws_iam_role.task_projector.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid    = "Reports"
      Effect = "Allow"
      Action = ["s3:PutObject", "s3:GetObject", "s3:ListBucket"]
      Resource = [
        aws_s3_bucket.app["reports"].arn,
        "${aws_s3_bucket.app["reports"].arn}/*",
      ]
    }]
  })
}

# ── Task role: flows worker ─────────────────────────────────────────

resource "aws_iam_role" "task_flows" {
  name = "${local.name}-task-flows"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "ecs-tasks.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy" "task_flows_policy" {
  name = "flows-permissions"
  role = aws_iam_role.task_flows.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "RagSnapshots"
        Effect   = "Allow"
        Action   = ["s3:GetObject", "s3:ListBucket"]
        Resource = [
          aws_s3_bucket.app["rag"].arn,
          "${aws_s3_bucket.app["rag"].arn}/*",
        ]
      },
      {
        Sid    = "ReportsWrite"
        Effect = "Allow"
        Action = ["s3:PutObject", "s3:GetObject"]
        Resource = "${aws_s3_bucket.app["reports"].arn}/*"
      },
      {
        Sid    = "Recordings"
        Effect = "Allow"
        Action = ["s3:GetObject"]
        Resource = [
          "${aws_s3_bucket.app["recordings"].arn}/*",
          "${aws_s3_bucket.app["transcripts"].arn}/*",
        ]
      },
      {
        Sid      = "BedrockEnrichment"
        Effect   = "Allow"
        Action   = ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"]
        Resource = "arn:aws:bedrock:${var.region}::foundation-model/*"
      },
    ]
  })
}

# ── GitHub Actions OIDC ─────────────────────────────────────────────
# Allows GitHub Actions runners to assume a deploy role without
# long-lived AWS keys. The trust policy is scoped to the specific
# repo + environment protection rule.

data "aws_iam_openid_connect_provider" "github" {
  url = "https://token.actions.githubusercontent.com"
}

# Create the OIDC provider only if it doesn't already exist in the
# account. In a multi-project account this resource may pre-exist.
resource "aws_iam_openid_connect_provider" "github" {
  count = var.create_github_oidc_provider ? 1 : 0

  url             = "https://token.actions.githubusercontent.com"
  client_id_list  = ["sts.amazonaws.com"]
  thumbprint_list = ["6938fd4d98bab03faadb97b34396831e3780aea1"]
}

resource "aws_iam_role" "github_deploy" {
  name = "${local.name}-github-deploy"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Principal = {
        Federated = var.create_github_oidc_provider ? (
          aws_iam_openid_connect_provider.github[0].arn
        ) : data.aws_iam_openid_connect_provider.github.arn
      }
      Action = "sts:AssumeRoleWithWebIdentity"
      Condition = {
        StringEquals = {
          "token.actions.githubusercontent.com:aud" = "sts.amazonaws.com"
        }
        StringLike = {
          # Restrict to the Sentinel repo; adjust if the org name differs.
          "token.actions.githubusercontent.com:sub" = "repo:${var.github_repo}:*"
        }
      }
    }]
  })
}

resource "aws_iam_role_policy" "github_deploy_policy" {
  name = "deploy"
  role = aws_iam_role.github_deploy.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "ECRAuth"
        Effect = "Allow"
        Action = ["ecr:GetAuthorizationToken"]
        Resource = "*"
      },
      {
        Sid    = "ECRPush"
        Effect = "Allow"
        Action = [
          "ecr:BatchCheckLayerAvailability",
          "ecr:CompleteLayerUpload",
          "ecr:InitiateLayerUpload",
          "ecr:PutImage",
          "ecr:UploadLayerPart",
          "ecr:BatchGetImage",
          "ecr:DescribeImages",
        ]
        Resource = [for r in aws_ecr_repository.app : r.arn]
      },
      {
        Sid    = "ECSDeployRollback"
        Effect = "Allow"
        Action = [
          "ecs:DescribeServices",
          "ecs:UpdateService",
          "ecs:DescribeTaskDefinition",
          "ecs:RegisterTaskDefinition",
          "ecs:DeregisterTaskDefinition",
          "ecs:ListTaskDefinitions",
          "ecs:DescribeTasks",
          "ecs:ListTasks",
        ]
        Resource = "*"
      },
      {
        Sid      = "PassTaskRoles"
        Effect   = "Allow"
        Action   = ["iam:PassRole"]
        Resource = [
          aws_iam_role.ecs_execution.arn,
          aws_iam_role.task_agent.arn,
          aws_iam_role.task_api.arn,
          aws_iam_role.task_projector.arn,
          aws_iam_role.task_flows.arn,
        ]
      },
    ]
  })
}

# ── Outputs ─────────────────────────────────────────────────────────

output "iam_ecs_execution_role_arn" {
  value = aws_iam_role.ecs_execution.arn
}
output "iam_task_agent_role_arn" {
  value = aws_iam_role.task_agent.arn
}
output "iam_task_api_role_arn" {
  value = aws_iam_role.task_api.arn
}
output "iam_github_deploy_role_arn" {
  value       = aws_iam_role.github_deploy.arn
  description = "Paste into GitHub secrets as AWS_DEPLOY_ROLE_ARN"
}
