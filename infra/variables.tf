# --- Required ------------------------------------------------------------------------

variable "region" {
  description = "AWS region of the whole stack."
  type        = string

  validation {
    condition     = can(regex("^[a-z]{2}(-[a-z]+)+-[0-9]$", var.region))
    error_message = "Give a region code such as us-east-1."
  }
}

variable "certificate_arn" {
  description = "ARN of an issued ACM certificate, in the stack's region, for the name clients will use."
  type        = string

  validation {
    condition     = can(regex("^arn:aws[a-z-]*:acm:[a-z0-9-]+:[0-9]{12}:certificate/[0-9a-f-]+$", var.certificate_arn))
    error_message = "Give the ARN of an ACM certificate."
  }
}

variable "github_repository" {
  description = "The repository whose deploy workflow may assume the deploy role, as owner/name."
  type        = string

  validation {
    # No wildcard can get into the role's trust condition through this value.
    condition     = can(regex("^[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9._-]+$", var.github_repository))
    error_message = "Give exactly one repository as owner/name, with no wildcard."
  }
}

# --- Optional ------------------------------------------------------------------------

variable "name" {
  description = "Prefix for every resource name."
  type        = string
  default     = "corridor"

  validation {
    # Also the cache's id, which allows 40 characters, and part of role names.
    condition     = can(regex("^[a-z][a-z0-9-]{1,22}[a-z0-9]$", var.name)) && !strcontains(var.name, "--")
    error_message = "Use 3 to 24 lower-case letters, digits and single hyphens, starting with a letter."
  }
}

variable "github_environment" {
  description = "The GitHub environment the deploy job runs in. The workflow names the same one."
  type        = string
  default     = "production"

  validation {
    condition     = can(regex("^[A-Za-z0-9_-]+$", var.github_environment))
    error_message = "Give one environment name, with no wildcard."
  }
}

variable "create_github_oidc_provider" {
  description = "Create the account's GitHub OIDC provider. An account has at most one; set false if it exists already."
  type        = bool
  default     = true
}

variable "vpc_cidr" {
  description = "Address range of the VPC."
  type        = string
  default     = "10.40.0.0/16"

  validation {
    condition     = can(cidrhost(var.vpc_cidr, 0)) && try(tonumber(split("/", var.vpc_cidr)[1]) <= 20, false)
    error_message = "Give an IPv4 range of /20 or larger, such as 10.40.0.0/16."
  }
}

variable "single_nat_gateway" {
  description = "One NAT gateway for both zones (cheaper) instead of one per zone (survives the loss of a zone)."
  type        = bool
  default     = true
}

variable "ingress_cidrs" {
  description = "Address ranges that may reach the load balancer."
  type        = list(string)
  default     = ["0.0.0.0/0"]

  validation {
    condition     = length(var.ingress_cidrs) > 0 && alltrue([for cidr in var.ingress_cidrs : can(cidrhost(cidr, 0))])
    error_message = "Give at least one IPv4 range."
  }
}

variable "db_instance_class" {
  description = "RDS instance class."
  type        = string
  default     = "db.t4g.micro"

  validation {
    condition     = startswith(var.db_instance_class, "db.")
    error_message = "Give an RDS instance class such as db.t4g.micro."
  }
}

variable "db_allocated_storage_gb" {
  description = "Storage the database starts with."
  type        = number
  default     = 20

  validation {
    condition     = var.db_allocated_storage_gb >= 20
    error_message = "gp3 storage starts at 20 GB."
  }
}

variable "db_max_allocated_storage_gb" {
  description = "Storage the database may grow to on its own."
  type        = number
  default     = 100

  validation {
    condition     = var.db_max_allocated_storage_gb >= 20
    error_message = "Give at least 20 GB."
  }
}

variable "db_multi_az" {
  description = "Keep a synchronous standby in the second zone. Roughly doubles the database's cost."
  type        = bool
  default     = false
}

variable "db_backup_retention_days" {
  description = "Days of automated backups, which is also the point-in-time recovery window."
  type        = number
  default     = 7

  validation {
    # Zero turns backups off, which this stack does not offer.
    condition     = var.db_backup_retention_days >= 1 && var.db_backup_retention_days <= 35
    error_message = "Give between 1 and 35 days."
  }
}

variable "db_deletion_protection" {
  description = "Refuse to delete the database until this is turned off. Part of the destroy procedure."
  type        = bool
  default     = true
}

variable "db_skip_final_snapshot" {
  description = "Delete the database without a last snapshot. Part of the destroy procedure."
  type        = bool
  default     = false
}

variable "cache_node_type" {
  description = "ElastiCache node type."
  type        = string
  default     = "cache.t4g.micro"

  validation {
    condition     = startswith(var.cache_node_type, "cache.")
    error_message = "Give an ElastiCache node type such as cache.t4g.micro."
  }
}

variable "cache_replicas" {
  description = "Read replicas of the cache. With at least one, a failed primary is replaced by a replica."
  type        = number
  default     = 0

  validation {
    condition     = var.cache_replicas >= 0 && var.cache_replicas <= 5 && floor(var.cache_replicas) == var.cache_replicas
    error_message = "Give a whole number from 0 to 5."
  }
}

variable "redis_auth_token" {
  description = "Password the cache requires. Give it when the cache is created and when rotating; it is never written to state. Set it with the TF_VAR_redis_auth_token environment variable, not in a file."
  type        = string
  sensitive   = true
  ephemeral   = true
  default     = null

  validation {
    condition     = var.redis_auth_token == null || can(regex("^[A-Za-z0-9!&#$^<>-]{32,128}$", var.redis_auth_token))
    error_message = "The token must be 32 to 128 characters from letters, digits and !&#$^<>-."
  }
}

variable "redis_auth_token_version" {
  description = "Raise by one to send redis_auth_token again, which rotates it."
  type        = number
  default     = 1
}

variable "redis_auth_token_update_strategy" {
  description = "How a new token replaces the old: ROTATE accepts both until SET leaves only the new one."
  type        = string
  default     = "ROTATE"

  validation {
    # DELETE would turn authentication off.
    condition     = contains(["ROTATE", "SET"], var.redis_auth_token_update_strategy)
    error_message = "Use ROTATE or SET."
  }
}

variable "api_desired_count" {
  description = "API tasks to run. Use 0 for the first apply, before the secrets have values and an image exists."
  type        = number
  default     = 2

  validation {
    condition     = var.api_desired_count >= 0 && floor(var.api_desired_count) == var.api_desired_count
    error_message = "Give a whole number, 0 or more."
  }
}

variable "worker_desired_count" {
  description = "Worker tasks to run. Use 0 for the first apply."
  type        = number
  default     = 1

  validation {
    condition     = var.worker_desired_count >= 0 && floor(var.worker_desired_count) == var.worker_desired_count
    error_message = "Give a whole number, 0 or more."
  }
}

variable "task_cpu" {
  description = "CPU units of each task (1024 is one vCPU)."
  type        = number
  default     = 256

  validation {
    condition     = contains([256, 512, 1024, 2048, 4096], var.task_cpu)
    error_message = "Fargate accepts 256, 512, 1024, 2048 or 4096 here."
  }
}

variable "task_memory_mib" {
  description = "Memory of each task. Must be a size Fargate offers for task_cpu."
  type        = number
  default     = 512

  validation {
    condition     = var.task_memory_mib >= 512 && var.task_memory_mib <= 30720
    error_message = "Give between 512 and 30720 MiB."
  }
}

variable "image_tag" {
  description = "Tag in the first revision of each task definition. The deploy workflow registers the revisions that actually run, so this only has to be a valid tag."
  type        = string
  default     = "bootstrap"

  validation {
    condition     = can(regex("^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$", var.image_tag))
    error_message = "Give a valid image tag."
  }
}

variable "container_insights" {
  description = "Collect per-task metrics with Container Insights, which is billed per metric."
  type        = bool
  default     = false
}

variable "alb_deletion_protection" {
  description = "Refuse to delete the load balancer until this is turned off. Part of the destroy procedure."
  type        = bool
  default     = true
}

variable "log_retention_days" {
  description = "Days the task logs are kept."
  type        = number
  default     = 30

  validation {
    condition     = contains([1, 3, 5, 7, 14, 30, 60, 90, 120, 150, 180, 365, 400, 545, 731], var.log_retention_days)
    error_message = "CloudWatch accepts only certain retention periods; 30, 90 and 365 are among them."
  }
}

variable "log_level" {
  description = "CORRIDOR_LOG_LEVEL. DEBUG is refused: the application will not start with it in production."
  type        = string
  default     = "INFO"

  validation {
    condition     = contains(["INFO", "WARNING", "ERROR"], var.log_level)
    error_message = "Use INFO, WARNING or ERROR."
  }
}

variable "webhook_tolerance_seconds" {
  description = "CORRIDOR_WEBHOOK_TOLERANCE_SECONDS. The application refuses more than 600 in production."
  type        = number
  default     = 300

  validation {
    condition     = var.webhook_tolerance_seconds >= 1 && var.webhook_tolerance_seconds <= 600
    error_message = "Give between 1 and 600 seconds."
  }
}

variable "provider_urls" {
  description = "Base URL of each provider the deployment calls, or null for one it does not. A provider that is set gets its secrets created, and their values must be set before the tasks start. The simulators are not deployed, so all are null by default."
  type = object({
    bank_rail = optional(string)
    custody   = optional(string)
    fx_rates  = optional(string)
  })
  default = {}

  validation {
    # The application refuses a provider address that is not HTTPS in production.
    condition = alltrue([
      for url in values(var.provider_urls) : url == null || can(regex("^https://[^\\s/]+", url))
    ])
    error_message = "Each provider URL must start with https://."
  }
}
