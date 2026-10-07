# Nothing here is secret. The connection strings are not outputs, because they contain
# passwords that Terraform never sees; the README says how to compose them from these.

output "alb_dns_name" {
  description = "Host name of the load balancer. Point the API's DNS name at it."
  value       = module.compute.alb_dns_name
}

output "alb_zone_id" {
  description = "Hosted zone of the load balancer, for a Route 53 alias record."
  value       = module.compute.alb_zone_id
}

output "database_address" {
  description = "Host name of the database, for the two database connection strings."
  value       = module.data.database_address
}

output "database_port" {
  description = "Port of the database."
  value       = module.data.database_port
}

output "database_name" {
  description = "Name of the database in the instance."
  value       = module.data.database_name
}

output "database_master_secret_arn" {
  description = "The secret RDS keeps the administrative password in. Needed once, to create the two roles."
  value       = module.data.database_master_secret_arn
}

output "cache_address" {
  description = "Host name of the cache, for the Redis connection string."
  value       = module.data.cache_address
}

output "cache_port" {
  description = "Port of the cache."
  value       = module.data.cache_port
}

output "secret_names" {
  description = "Secrets whose values must be set by hand before the tasks can start."
  value       = module.identity.secret_names
}

output "github_variables" {
  description = "Repository (or environment) variables the deploy workflow reads, by name."
  value = {
    AWS_REGION                    = var.region
    AWS_DEPLOY_ROLE_ARN           = module.identity.deploy_role_arn
    ECR_REPOSITORY                = module.compute.ecr_repository_name
    ECS_CLUSTER                   = module.compute.cluster_name
    ECS_API_SERVICE               = module.compute.api_service_name
    ECS_WORKER_SERVICE            = module.compute.worker_service_name
    ECS_MIGRATE_TASK_FAMILY       = module.compute.migrate_task_family
    ECS_PRIVATE_SUBNET_IDS        = join(",", module.network.private_subnet_ids)
    ECS_MIGRATE_SECURITY_GROUP_ID = module.network.security_group_ids.migrate
  }
}
