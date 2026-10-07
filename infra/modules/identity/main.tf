# Who may do what. Three kinds of principal:
#
# - An execution role per task kind. ECS uses it to start the task: pull the image, read
#   the secrets the task definition names, write the log stream. Because this is the role
#   that reads secrets, there is one per kind and not one for all: the API's cannot read
#   the owner connection string, and the migration's can read nothing else.
# - A task role per task kind. It is what the running code would call AWS as. The code
#   calls no AWS API, so these have no permissions; they exist so that granting one later
#   is a change to one service.
# - The deploy role, assumed by one repository's workflow in one environment.

data "aws_caller_identity" "current" {}
data "aws_partition" "current" {}
data "aws_region" "current" {}

locals {
  account   = data.aws_caller_identity.current.account_id
  partition = data.aws_partition.current.partition
  region    = data.aws_region.current.region

  kinds = toset(["api", "worker", "migrate"])

  # Every secret, the variable it becomes in a container, and the task kinds that get it.
  # The values are set by hand after the first apply; see the README.
  all_secrets = {
    database-url = {
      env         = "CORRIDOR_DATABASE_URL"
      description = "Connection string of the application role"
      kinds       = ["api", "worker"]
      enabled     = true
    }
    database-owner-url = {
      env         = "CORRIDOR_DATABASE_OWNER_URL"
      description = "Connection string of the owner role, used only to migrate"
      kinds       = ["migrate"]
      enabled     = true
    }
    redis-url = {
      env         = "CORRIDOR_REDIS_URL"
      description = "Connection string of the cache, with its auth token"
      kinds       = ["api", "worker"]
      enabled     = true
    }
    jwt-signing-key = {
      env         = "CORRIDOR_JWT_SIGNING_KEY"
      description = "ES256 private key that signs access tokens, as PEM"
      kinds       = ["api"]
      enabled     = true
    }
    api-key-hash-key = {
      env         = "CORRIDOR_API_KEY_HASH_KEY"
      description = "Key that agent API keys are hashed under"
      kinds       = ["api"]
      enabled     = true
    }
    fx-cache-mac-key = {
      env         = "CORRIDOR_FX_CACHE_MAC_KEY"
      description = "Key that authenticates exchange rates cached in Redis"
      kinds       = ["api"]
      enabled     = true
    }
    bank-rail-api-key = {
      env         = "CORRIDOR_BANK_RAIL_API_KEY"
      description = "Key presented to the bank-rail provider"
      kinds       = ["api", "worker"]
      enabled     = var.providers_configured.bank_rail
    }
    bank-rail-webhook-secrets = {
      env         = "CORRIDOR_BANK_RAIL_WEBHOOK_SECRETS"
      description = "Secrets that verify the bank-rail provider's webhooks, as a JSON list"
      kinds       = ["api"]
      enabled     = var.providers_configured.bank_rail
    }
    custody-api-key = {
      env         = "CORRIDOR_CUSTODY_API_KEY"
      description = "Key presented to the custody provider"
      kinds       = ["api", "worker"]
      enabled     = var.providers_configured.custody
    }
    custody-webhook-secrets = {
      env         = "CORRIDOR_CUSTODY_WEBHOOK_SECRETS"
      description = "Secrets that verify the custody provider's webhooks, as a JSON list"
      kinds       = ["api"]
      enabled     = var.providers_configured.custody
    }
    fx-rates-api-key = {
      env         = "CORRIDOR_FX_RATES_API_KEY"
      description = "Key presented to the exchange-rate source"
      kinds       = ["api"]
      enabled     = var.providers_configured.fx_rates
    }
  }
  secrets = { for key, secret in local.all_secrets : key => secret if secret.enabled }

  secret_arns_by_kind = {
    for kind in local.kinds : kind => [
      for key, secret in local.secrets : aws_secretsmanager_secret.this[key].arn
      if contains(secret.kinds, kind)
    ]
  }

  # Names the compute module gives its resources, rebuilt here so that the roles can be
  # scoped to them without the two modules depending on each other in a circle.
  ecr_repository_arn = "arn:${local.partition}:ecr:${local.region}:${local.account}:repository/${var.name}"
  cluster_arn        = "arn:${local.partition}:ecs:${local.region}:${local.account}:cluster/${var.name}"
  service_arns = [
    for kind in ["api", "worker"] :
    "arn:${local.partition}:ecs:${local.region}:${local.account}:service/${var.name}/${var.name}-${kind}"
  ]
  github_oidc_host = "token.actions.githubusercontent.com"
  github_oidc_provider_arn = (
    var.create_github_oidc_provider
    ? aws_iam_openid_connect_provider.github[0].arn
    : "arn:${local.partition}:iam::${local.account}:oidc-provider/${local.github_oidc_host}"
  )
}

# --- Secrets -------------------------------------------------------------------------

# Each secret is created with no value at all. There is deliberately no
# aws_secretsmanager_secret_version here: a value given to Terraform would be written to
# its state. A task whose secret has no value yet does not start.
resource "aws_secretsmanager_secret" "this" {
  # checkov:skip=CKV_AWS_149:Encrypted with the account's Secrets Manager key. A key of its own adds a monthly charge and a key policy to keep, and protects against nothing this stack's roles do not already bound.
  # checkov:skip=CKV2_AWS_57:These are connection strings and application keys with no rotation function to call; each is replaced by hand as the README describes.
  for_each = local.secrets

  name        = "${var.name}/${each.key}"
  description = each.value.description

  recovery_window_in_days = 7
}

# --- Logs ----------------------------------------------------------------------------

resource "aws_cloudwatch_log_group" "this" {
  # checkov:skip=CKV_AWS_158:Encrypted with the key the log service owns. The application redacts secrets and account numbers before it logs.
  # checkov:skip=CKV_AWS_338:Retention is a variable; a year of logs is not wanted for a stack that lives for a demonstration.
  for_each = local.kinds

  name              = "/ecs/${var.name}/${each.key}"
  retention_in_days = var.log_retention_days
}

# --- Task roles ----------------------------------------------------------------------

data "aws_iam_policy_document" "assume_by_tasks" {
  statement {
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["ecs-tasks.amazonaws.com"]
    }

    # Only tasks of this account and region, so another account's ECS cannot be made to
    # present these roles.
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [local.account]
    }

    condition {
      test     = "ArnLike"
      variable = "aws:SourceArn"
      values   = ["arn:${local.partition}:ecs:${local.region}:${local.account}:*"]
    }
  }
}

resource "aws_iam_role" "execution" {
  for_each = local.kinds

  name               = "${var.name}-${each.key}-execution"
  description        = "Starts ${each.key} tasks: pulls the image, reads their secrets, writes their log"
  assume_role_policy = data.aws_iam_policy_document.assume_by_tasks.json
}

data "aws_iam_policy_document" "execution" {
  for_each = local.kinds

  statement {
    sid = "RegistryLogin"
    # This action has no resource to name; the token it returns is useful only together
    # with the pull permissions below.
    actions   = ["ecr:GetAuthorizationToken"]
    resources = ["*"]
  }

  statement {
    sid = "PullTheImage"
    actions = [
      "ecr:BatchCheckLayerAvailability",
      "ecr:BatchGetImage",
      "ecr:GetDownloadUrlForLayer",
    ]
    resources = [local.ecr_repository_arn]
  }

  statement {
    sid = "WriteOwnLog"
    actions = [
      "logs:CreateLogStream",
      "logs:PutLogEvents",
    ]
    resources = ["${aws_cloudwatch_log_group.this[each.key].arn}:*"]
  }

  statement {
    sid       = "ReadOwnSecrets"
    actions   = ["secretsmanager:GetSecretValue"]
    resources = local.secret_arns_by_kind[each.key]
  }
}

resource "aws_iam_role_policy" "execution" {
  for_each = local.kinds

  name   = "start-tasks"
  role   = aws_iam_role.execution[each.key].id
  policy = data.aws_iam_policy_document.execution[each.key].json
}

resource "aws_iam_role" "task" {
  for_each = local.kinds

  name               = "${var.name}-${each.key}-task"
  description        = "What ${each.key} code calls AWS as. It calls nothing, so nothing is attached"
  assume_role_policy = data.aws_iam_policy_document.assume_by_tasks.json
}

# --- Deploy role ---------------------------------------------------------------------

resource "aws_iam_openid_connect_provider" "github" {
  count = var.create_github_oidc_provider ? 1 : 0

  url            = "https://${local.github_oidc_host}"
  client_id_list = ["sts.amazonaws.com"]
}

data "aws_iam_policy_document" "assume_by_github" {
  statement {
    actions = ["sts:AssumeRoleWithWebIdentity"]

    principals {
      type        = "Federated"
      identifiers = [local.github_oidc_provider_arn]
    }

    condition {
      test     = "StringEquals"
      variable = "${local.github_oidc_host}:aud"
      values   = ["sts.amazonaws.com"]
    }

    # An exact match, no wildcard: one repository, and only a job that runs in the named
    # environment, which is where the required reviewers are set.
    condition {
      test     = "StringEquals"
      variable = "${local.github_oidc_host}:sub"
      values   = ["repo:${var.github_repository}:environment:${var.github_environment}"]
    }
  }
}

resource "aws_iam_role" "deploy" {
  name                 = "${var.name}-deploy"
  description          = "Assumed by the deploy workflow of ${var.github_repository} in its ${var.github_environment} environment"
  assume_role_policy   = data.aws_iam_policy_document.assume_by_github.json
  max_session_duration = 3600
}

data "aws_iam_policy_document" "deploy" {
  statement {
    sid       = "RegistryLogin"
    actions   = ["ecr:GetAuthorizationToken"]
    resources = ["*"]
  }

  statement {
    sid = "PushTheImage"
    actions = [
      "ecr:BatchCheckLayerAvailability",
      "ecr:BatchGetImage",
      "ecr:CompleteLayerUpload",
      "ecr:GetDownloadUrlForLayer",
      "ecr:InitiateLayerUpload",
      "ecr:PutImage",
      "ecr:UploadLayerPart",
    ]
    resources = [local.ecr_repository_arn]
  }

  statement {
    sid = "CopyTaskDefinitions"
    # Neither action can be limited to a resource. What a registered definition can do is
    # bounded by the roles this role may pass, below.
    actions = [
      "ecs:DescribeTaskDefinition",
      "ecs:RegisterTaskDefinition",
    ]
    resources = ["*"]
  }

  statement {
    sid       = "RunTheMigration"
    actions   = ["ecs:RunTask"]
    resources = ["arn:${local.partition}:ecs:${local.region}:${local.account}:task-definition/${var.name}-migrate:*"]

    condition {
      test     = "ArnEquals"
      variable = "ecs:cluster"
      values   = [local.cluster_arn]
    }
  }

  statement {
    sid       = "WatchTheMigration"
    actions   = ["ecs:DescribeTasks"]
    resources = ["arn:${local.partition}:ecs:${local.region}:${local.account}:task/${var.name}/*"]
  }

  statement {
    sid = "UpdateTheServices"
    actions = [
      "ecs:DescribeServices",
      "ecs:UpdateService",
    ]
    resources = local.service_arns
  }

  statement {
    sid       = "HandRolesToTasks"
    actions   = ["iam:PassRole"]
    resources = concat([for role in aws_iam_role.execution : role.arn], [for role in aws_iam_role.task : role.arn])

    condition {
      test     = "StringEquals"
      variable = "iam:PassedToService"
      values   = ["ecs-tasks.amazonaws.com"]
    }
  }
}

resource "aws_iam_role_policy" "deploy" {
  name   = "deploy"
  role   = aws_iam_role.deploy.id
  policy = data.aws_iam_policy_document.deploy.json
}
