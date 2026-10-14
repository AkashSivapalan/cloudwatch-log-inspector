terraform {
  required_version = ">= 1.9"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
  }
  # State is kept locally (infra/terraform/terraform.tfstate, git-ignored). It holds resource
  # IDs, not the database password (RDS manages that in Secrets Manager) or the Anthropic key.
}

provider "aws" {
  region  = var.region
  profile = var.aws_profile

  default_tags {
    tags = {
      Project   = "log-inspector"
      ManagedBy = "terraform"
    }
  }
}

data "aws_caller_identity" "current" {}

data "aws_availability_zones" "available" {
  state = "available"
}

locals {
  name = "log-inspector"
  azs  = slice(data.aws_availability_zones.available.names, 0, 2)
}
