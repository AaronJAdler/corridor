terraform {
  # Write-only arguments and ephemeral variables, which keep the cache's auth token out of
  # state, need 1.11.
  required_version = ">= 1.11"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
  }

  # Remote state. Until this is uncommented the state is a local file, which is enough to
  # try the stack and is not something to share. To use it: create the bucket by hand
  # (versioned, encrypted, public access blocked), uncomment, fill in, and run
  # `terraform init -migrate-state`. The lock is a file in the same bucket; no table is
  # needed. See the README.
  #
  # backend "s3" {
  #   bucket       = "NAME-OF-YOUR-STATE-BUCKET"
  #   key          = "corridor/production/terraform.tfstate"
  #   region       = "us-east-1"
  #   encrypt      = true
  #   use_lockfile = true
  # }
}

provider "aws" {
  region = var.region

  default_tags {
    tags = {
      Project     = var.name
      Environment = "production"
      ManagedBy   = "terraform"
    }
  }
}
