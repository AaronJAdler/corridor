output "ecr_repository_name" {
  description = "Name of the image repository."
  value       = aws_ecr_repository.this.name
}

output "ecr_repository_url" {
  description = "Address images are pushed to."
  value       = aws_ecr_repository.this.repository_url
}

output "cluster_name" {
  description = "Name of the ECS cluster."
  value       = aws_ecs_cluster.this.name
}

output "api_service_name" {
  description = "Name of the API service."
  value       = aws_ecs_service.api.name
}

output "worker_service_name" {
  description = "Name of the worker service."
  value       = aws_ecs_service.worker.name
}

output "migrate_task_family" {
  description = "Family of the migration task definition."
  value       = aws_ecs_task_definition.this["migrate"].family
}

output "alb_dns_name" {
  description = "Host name of the load balancer, for the DNS record of the API's name."
  value       = aws_lb.this.dns_name
}

output "alb_zone_id" {
  description = "Hosted zone of the load balancer, for a Route 53 alias record."
  value       = aws_lb.this.zone_id
}
