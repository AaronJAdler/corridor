# The whole stack: a network, the two data stores, identities and secrets, and the tasks
# behind a load balancer. Nothing here is applied automatically; see README.md.

locals {
  api_port = 8000

  providers_configured = {
    bank_rail = var.provider_urls.bank_rail != null
    custody   = var.provider_urls.custody != null
    fx_rates  = var.provider_urls.fx_rates != null
  }

  # Everything the application's production check requires, set explicitly so that a
  # change of a default in the code cannot make a deployment that refuses to start, or
  # worse, one that starts with a development setting.
  common_environment = {
    CORRIDOR_ENVIRONMENT               = "production"
    CORRIDOR_LOG_LEVEL                 = var.log_level
    CORRIDOR_LOG_FORMAT                = "json"
    CORRIDOR_DATABASE_APP_ROLE         = "corridor_app"
    CORRIDOR_RATE_LIMIT_ENABLED        = "true"
    CORRIDOR_WEBHOOK_TOLERANCE_SECONDS = tostring(var.webhook_tolerance_seconds)
  }

  api_environment = merge(
    local.common_environment,
    {
      # X-Forwarded-For is believed only from the load balancer, which lives in the public
      # subnets. Rate limiting by client address depends on this being exactly that.
      CORRIDOR_FORWARDED_ALLOW_IPS = join(",", module.network.public_subnet_cidrs)
    },
    local.providers_configured.bank_rail ? { CORRIDOR_BANK_RAIL_URL = var.provider_urls.bank_rail } : {},
    local.providers_configured.custody ? { CORRIDOR_CUSTODY_URL = var.provider_urls.custody } : {},
    local.providers_configured.fx_rates ? { CORRIDOR_FX_RATES_URL = var.provider_urls.fx_rates } : {},
  )

  # The worker calls the bank rail and the custodian. It takes no requests and fetches no
  # rates.
  worker_environment = merge(
    local.common_environment,
    local.providers_configured.bank_rail ? { CORRIDOR_BANK_RAIL_URL = var.provider_urls.bank_rail } : {},
    local.providers_configured.custody ? { CORRIDOR_CUSTODY_URL = var.provider_urls.custody } : {},
  )

  # A migration reads the owner connection (a secret) and the name of the role it grants
  # to, and nothing else.
  migrate_environment = {
    CORRIDOR_DATABASE_APP_ROLE = "corridor_app"
  }
}

module "network" {
  source = "./modules/network"

  name               = var.name
  vpc_cidr           = var.vpc_cidr
  single_nat_gateway = var.single_nat_gateway
  ingress_cidrs      = var.ingress_cidrs
  api_port           = local.api_port
}

module "data" {
  source = "./modules/data"

  name                       = var.name
  subnet_ids                 = module.network.private_subnet_ids
  database_security_group_id = module.network.security_group_ids.database
  cache_security_group_id    = module.network.security_group_ids.cache

  db_instance_class           = var.db_instance_class
  db_allocated_storage_gb     = var.db_allocated_storage_gb
  db_max_allocated_storage_gb = var.db_max_allocated_storage_gb
  db_multi_az                 = var.db_multi_az
  db_backup_retention_days    = var.db_backup_retention_days
  db_deletion_protection      = var.db_deletion_protection
  db_skip_final_snapshot      = var.db_skip_final_snapshot
  db_name                     = "corridor"
  db_master_username          = "corridor_admin"

  cache_node_type                  = var.cache_node_type
  cache_replicas                   = var.cache_replicas
  redis_auth_token                 = var.redis_auth_token
  redis_auth_token_version         = var.redis_auth_token_version
  redis_auth_token_update_strategy = var.redis_auth_token_update_strategy
}

module "identity" {
  source = "./modules/identity"

  name                        = var.name
  log_retention_days          = var.log_retention_days
  providers_configured        = local.providers_configured
  github_repository           = var.github_repository
  github_environment          = var.github_environment
  create_github_oidc_provider = var.create_github_oidc_provider
}

module "compute" {
  source = "./modules/compute"

  name               = var.name
  vpc_id             = module.network.vpc_id
  public_subnet_ids  = module.network.public_subnet_ids
  private_subnet_ids = module.network.private_subnet_ids
  security_group_ids = {
    alb    = module.network.security_group_ids.alb
    api    = module.network.security_group_ids.api
    worker = module.network.security_group_ids.worker
  }

  certificate_arn         = var.certificate_arn
  alb_deletion_protection = var.alb_deletion_protection

  execution_role_arns = module.identity.execution_role_arns
  task_role_arns      = module.identity.task_role_arns
  log_group_names     = module.identity.log_group_names
  container_secrets   = module.identity.container_secrets
  container_environment = {
    api     = local.api_environment
    worker  = local.worker_environment
    migrate = local.migrate_environment
  }

  image_tag            = var.image_tag
  api_port             = local.api_port
  api_desired_count    = var.api_desired_count
  worker_desired_count = var.worker_desired_count
  task_cpu             = var.task_cpu
  task_memory_mib      = var.task_memory_mib
  container_insights   = var.container_insights
}
