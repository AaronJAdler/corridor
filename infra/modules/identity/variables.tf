variable "name" {
  description = "Prefix for every resource name. The compute module derives the same names from it."
  type        = string
}

variable "log_retention_days" {
  description = "Days the task logs are kept."
  type        = number
}

variable "providers_configured" {
  description = "Which providers the deployment calls. A provider's secrets exist only when it is configured."
  type = object({
    bank_rail = bool
    custody   = bool
    fx_rates  = bool
  })
}

variable "github_repository" {
  description = "The one repository whose deploy workflow may assume the deploy role, as owner/name."
  type        = string
}

variable "github_environment" {
  description = "The GitHub environment a job must run in to assume the deploy role."
  type        = string
}

variable "create_github_oidc_provider" {
  description = "Create the account's GitHub OIDC provider. An account has at most one; turn this off if it exists already."
  type        = bool
}
