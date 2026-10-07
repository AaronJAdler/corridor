variable "name" {
  description = "Prefix for every resource name. The identity module derives the same names from it."
  type        = string
}

variable "vpc_id" {
  description = "The VPC."
  type        = string
}

variable "public_subnet_ids" {
  description = "Subnets of the load balancer."
  type        = list(string)
}

variable "private_subnet_ids" {
  description = "Subnets of the tasks."
  type        = list(string)
}

variable "security_group_ids" {
  description = "Security groups by component: alb, api, worker."
  type = object({
    alb    = string
    api    = string
    worker = string
  })
}

variable "certificate_arn" {
  description = "ACM certificate the HTTPS listener presents."
  type        = string
}

variable "alb_deletion_protection" {
  description = "Refuse to delete the load balancer until this is turned off."
  type        = bool
}

variable "execution_role_arns" {
  description = "Execution role of each task kind: api, worker, migrate."
  type        = map(string)
}

variable "task_role_arns" {
  description = "Task role of each task kind: api, worker, migrate."
  type        = map(string)
}

variable "log_group_names" {
  description = "Log group of each task kind: api, worker, migrate."
  type        = map(string)
}

variable "container_secrets" {
  description = "For each task kind, variable name to secret ARN."
  type        = map(map(string))
}

variable "container_environment" {
  description = "For each task kind, variable name to plain value."
  type        = map(map(string))
}

variable "image_tag" {
  description = "Tag in the first revision of each task definition. The deploy workflow registers the revisions that run."
  type        = string
}

variable "api_port" {
  description = "Port the API container listens on."
  type        = number
}

variable "api_desired_count" {
  description = "API tasks to run."
  type        = number
}

variable "worker_desired_count" {
  description = "Worker tasks to run."
  type        = number
}

variable "task_cpu" {
  description = "CPU units of each task (1024 is one vCPU)."
  type        = number
}

variable "task_memory_mib" {
  description = "Memory of each task."
  type        = number
}

variable "container_insights" {
  description = "Collect per-task metrics with Container Insights."
  type        = bool
}
