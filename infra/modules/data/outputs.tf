output "database_address" {
  description = "Host name of the database."
  value       = aws_db_instance.this.address
}

output "database_port" {
  description = "Port of the database."
  value       = aws_db_instance.this.port
}

output "database_name" {
  description = "Name of the database in the instance."
  value       = aws_db_instance.this.db_name
}

output "database_master_secret_arn" {
  description = "The secret RDS keeps the administrative password in."
  value       = aws_db_instance.this.master_user_secret[0].secret_arn
}

output "cache_address" {
  description = "Host name of the cache's primary endpoint."
  value       = aws_elasticache_replication_group.this.primary_endpoint_address
}

output "cache_port" {
  description = "Port of the cache."
  value       = aws_elasticache_replication_group.this.port
}
