# Provider + Terraform version pins.
# Bumped deliberately — floor versions are what the modules were
# authored against, not the newest available. Upgrading is a P7-follow-up
# concern (test plan / breaking-change review).

terraform {
  required_version = ">= 1.9.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.60"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.6"
    }
  }
}

provider "aws" {
  region = var.region
  default_tags {
    tags = {
      Project     = "sentinel-voice"
      Environment = var.environment
      ManagedBy   = "terraform"
    }
  }
}
