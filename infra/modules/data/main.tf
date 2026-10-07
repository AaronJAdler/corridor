# The two stores. Both are in private subnets, encrypted at rest, and refuse a connection
# that is not encrypted. Neither has a credential in this configuration: the database's
# administrative password is generated and kept by RDS, and the cache's is passed through
# a write-only argument.

resource "aws_db_subnet_group" "this" {
  name       = var.name
  subnet_ids = var.subnet_ids
}

resource "aws_db_parameter_group" "this" {
  name_prefix = "${var.name}-postgres16-"
  family      = "postgres16"
  description = "PostgreSQL 16 for ${var.name}: TLS required"

  # A client that does not negotiate TLS is refused, whatever its connection string says.
  parameter {
    name  = "rds.force_ssl"
    value = "1"
  }

  parameter {
    name  = "log_min_duration_statement"
    value = "1000"
  }

  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_db_instance" "this" {
  # checkov:skip=CKV_AWS_354:Performance Insights data is encrypted with the account's RDS key; it holds statement shapes, not bound values.
  # checkov:skip=CKV_AWS_157:A standby in a second zone is a choice of cost against availability, made with db_multi_az.
  # checkov:skip=CKV_AWS_118:Enhanced monitoring needs a further role and is billed by log volume; Performance Insights and the PostgreSQL log are on.
  # checkov:skip=CKV_AWS_161:The application signs in as PostgreSQL roles with passwords held in Secrets Manager; it has no support for IAM tokens.
  # checkov:skip=CKV2_AWS_30:Statements slower than a second are logged through the parameter group; logging every statement would write personal data to the log.
  identifier = var.name

  engine                      = "postgres"
  engine_version              = "16"
  auto_minor_version_upgrade  = true
  allow_major_version_upgrade = false
  instance_class              = var.db_instance_class

  storage_type          = "gp3"
  allocated_storage     = var.db_allocated_storage_gb
  max_allocated_storage = var.db_max_allocated_storage_gb
  storage_encrypted     = true

  db_name  = var.db_name
  username = var.db_master_username
  # RDS generates the password, stores it in a secret of its own and rotates it. It is in
  # neither this configuration nor its state.
  manage_master_user_password = true

  multi_az               = var.db_multi_az
  db_subnet_group_name   = aws_db_subnet_group.this.name
  vpc_security_group_ids = [var.database_security_group_id]
  publicly_accessible    = false
  parameter_group_name   = aws_db_parameter_group.this.name
  ca_cert_identifier     = "rds-ca-rsa2048-g1"

  backup_retention_period   = var.db_backup_retention_days
  backup_window             = "07:00-08:00"
  maintenance_window        = "sun:08:30-sun:09:30"
  copy_tags_to_snapshot     = true
  deletion_protection       = var.db_deletion_protection
  skip_final_snapshot       = var.db_skip_final_snapshot
  final_snapshot_identifier = var.db_skip_final_snapshot ? null : "${var.name}-final"

  performance_insights_enabled    = true
  enabled_cloudwatch_logs_exports = ["postgresql", "upgrade"]
}

resource "aws_elasticache_subnet_group" "this" {
  name       = var.name
  subnet_ids = var.subnet_ids
}

# The engine's defaults, in a group of this stack's own: the default group cannot be
# edited, so changing a parameter later would otherwise mean replacing the group in use.
resource "aws_elasticache_parameter_group" "this" {
  name        = "${var.name}-redis7"
  family      = "redis7"
  description = "Redis 7 for ${var.name}"
}

resource "aws_elasticache_replication_group" "this" {
  # checkov:skip=CKV_AWS_31:The auth token is set, through the write-only argument auth_token_wo, which this check does not read. Transit encryption is required.
  # checkov:skip=CKV2_AWS_50:Replicas and failover are a choice of cost against availability, made with cache_replicas. Every use of the cache has a defined behaviour for when it is down.
  # checkov:skip=CKV_AWS_191:Encrypted at rest with the key the service owns. The cache holds rate-limit counters, public exchange rates and revocation hints, nothing that warrants a key of its own.
  replication_group_id = var.name
  description          = "Rate limits, rate cache and revocation hints for ${var.name}"

  engine               = "redis"
  engine_version       = "7.1"
  parameter_group_name = aws_elasticache_parameter_group.this.name
  node_type            = var.cache_node_type
  port                 = 6379

  num_cache_clusters         = 1 + var.cache_replicas
  automatic_failover_enabled = var.cache_replicas > 0
  multi_az_enabled           = var.cache_replicas > 0

  subnet_group_name  = aws_elasticache_subnet_group.this.name
  security_group_ids = [var.cache_security_group_id]

  at_rest_encryption_enabled = true
  transit_encryption_enabled = true
  transit_encryption_mode    = "required"
  # Write-only: the provider sends the token to AWS and keeps no copy. It is sent again
  # only when the version changes.
  auth_token_wo              = var.redis_auth_token
  auth_token_wo_version      = var.redis_auth_token_version
  auth_token_update_strategy = var.redis_auth_token_update_strategy

  # Nothing in the cache needs to survive it, so there are no snapshots to pay for.
  snapshot_retention_limit   = 0
  auto_minor_version_upgrade = true
  maintenance_window         = "sun:09:30-sun:10:30"
  apply_immediately          = true
}
