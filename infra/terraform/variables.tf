# Cross-module variables. Values come from `envs/<env>.tfvars`.

variable "region" {
  description = "AWS region — kept in ap-south-1 per §14.5"
  type        = string
  default     = "ap-south-1"
}

variable "environment" {
  description = "dev | staging | prod"
  type        = string
  validation {
    condition     = contains(["dev", "staging", "prod"], var.environment)
    error_message = "environment must be one of: dev, staging, prod"
  }
}

variable "domain_name" {
  description = "Public FQDN attached to the ALB (voice.sentinel.in in prod)"
  type        = string
}

variable "acm_certificate_arn" {
  description = "ACM cert covering domain_name — must be in same region as ALB"
  type        = string
}

variable "vpc_cidr" {
  description = "VPC CIDR — leaves headroom for two AZs of /20 subnets"
  type        = string
  default     = "10.30.0.0/16"
}

variable "azs" {
  description = "Availability zones — two per §14.1 topology"
  type        = list(string)
  default     = ["ap-south-1a", "ap-south-1b"]
}

variable "image_tag" {
  description = "ECR image tag (git SHA in CI). Overridden per deploy."
  type        = string
  default     = "latest"
}

# ── Per-env sizing knobs. Prod overrides; dev/staging use defaults. ──

variable "voice_agent_cpu" {
  description = "vCPU units for the fg-voice-agent task"
  type        = number
  default     = 2048 # 2 vCPU per §14.1
}

variable "voice_agent_memory" {
  description = "Memory MiB for the fg-voice-agent task"
  type        = number
  default     = 4096 # 4 GB per §14.1
}

variable "voice_agent_min_tasks" {
  type    = number
  default = 2 # §14.1: min 2 tasks
}

variable "voice_agent_max_tasks" {
  type    = number
  default = 20
}

variable "target_calls_per_task" {
  description = "Autoscaling target on fg_voice_concurrent_calls_per_task (§14.3)"
  type        = number
  default     = 8
}

variable "voice_api_cpu" {
  type    = number
  default = 512 # 0.5 vCPU per §14.1
}

variable "voice_api_memory" {
  type    = number
  default = 1024
}

variable "rds_instance_class" {
  description = "RDS instance class — db.t4g.large per §14.1 default"
  type        = string
  default     = "db.t4g.large"
}

variable "rds_allocated_storage_gb" {
  type    = number
  default = 100
}

variable "redis_node_type" {
  type    = string
  default = "cache.t4g.small"
}

variable "cyclone_season_min_tasks" {
  description = "Minimum tasks during cyclone season (Jun-Nov) pre-warm schedule"
  type        = number
  default     = 4
}

variable "create_github_oidc_provider" {
  description = "Set false if the OIDC provider already exists in this account"
  type        = bool
  default     = true
}

variable "github_repo" {
  description = "GitHub repo slug for OIDC trust (org/repo)"
  type        = string
  default     = "sentinel/Sentinel-Mobile-App"
}

variable "twilio_egress_cidrs" {
  description = "Twilio egress ranges for the WAF allowlist on /voice/*"
  type        = list(string)
  # Refreshed from https://www.twilio.com/docs/sip-trunking/ip-addresses
  # every quarter; the list should be regenerated when Twilio adds a
  # region or during an incident review.
  default = [
    "54.172.60.0/23",
    "54.244.51.0/24",
    "177.71.206.192/26",
    "54.171.127.192/26",
    "35.156.191.128/25",
    "54.65.63.192/26",
    "54.169.127.128/26",
    "54.252.254.64/26",
  ]
}
