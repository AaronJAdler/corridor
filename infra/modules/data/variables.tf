variable "name" {
  description = "Prefix for every resource name."
  type        = string
}

variable "subnet_ids" {
  description = "Private subnets for both stores."
  type        = list(string)
}

variable "database_security_group_id" {
  description = "Security group of the database."
  type        = string
}

variable "cache_security_group_id" {
  description = "Security group of the cache."
  type        = string
}

variable "db_instance_class" {
  description = "RDS instance class."
  type        = string
}

variable "db_allocated_storage_gb" {
  description = "Storage the database starts with."
  type        = number
}

variable "db_max_allocated_storage_gb" {
  description = "Storage the database may grow to on its own."
  type        = number
}

variable "db_multi_az" {
  description = "Keep a synchronous standby in the second zone."
  type        = bool
}

variable "db_backup_retention_days" {
  description = "Days of automated backups, which is also the point-in-time recovery window."
  type        = number
}

variable "db_deletion_protection" {
  description = "Refuse to delete the database until this is turned off."
  type        = bool
}

variable "db_skip_final_snapshot" {
  description = "Delete the database without a last snapshot."
  type        = bool
}

variable "db_name" {
  description = "Name of the database created in the instance."
  type        = string
}

variable "db_master_username" {
  description = "Administrative user of the instance. It creates the two application roles and is not used afterwards."
  type        = string
}

variable "cache_node_type" {
  description = "ElastiCache node type."
  type        = string
}

variable "cache_replicas" {
  description = "Read replicas of the cache. With at least one, a failed primary is replaced by a replica."
  type        = number
}

variable "redis_auth_token" {
  description = "Password the cache requires. Sent to AWS and never written to state or to a plan."
  type        = string
  sensitive   = true
  ephemeral   = true
  default     = null
}

variable "redis_auth_token_version" {
  description = "Raise by one to send redis_auth_token again, which rotates it."
  type        = number
}

variable "redis_auth_token_update_strategy" {
  description = "How a new token replaces the old: ROTATE accepts both, SET leaves only the new one."
  type        = string
}
