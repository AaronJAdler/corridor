output "vpc_id" {
  description = "The VPC."
  value       = aws_vpc.this.id
}

output "public_subnet_ids" {
  description = "Subnets of the load balancer."
  value       = aws_subnet.public[*].id
}

output "public_subnet_cidrs" {
  description = "Address ranges the load balancer's requests come from."
  value       = aws_subnet.public[*].cidr_block
}

output "private_subnet_ids" {
  description = "Subnets of the tasks and the data stores."
  value       = aws_subnet.private[*].id
}

output "security_group_ids" {
  description = "Security group of each component, by name."
  value = {
    alb      = aws_security_group.alb.id
    api      = aws_security_group.api.id
    worker   = aws_security_group.worker.id
    migrate  = aws_security_group.migrate.id
    database = aws_security_group.database.id
    cache    = aws_security_group.cache.id
  }
}
