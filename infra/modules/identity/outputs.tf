output "execution_role_arns" {
  description = "Execution role of each task kind."
  value       = { for kind, role in aws_iam_role.execution : kind => role.arn }
}

output "task_role_arns" {
  description = "Task role of each task kind."
  value       = { for kind, role in aws_iam_role.task : kind => role.arn }
}

output "log_group_names" {
  description = "Log group of each task kind."
  value       = { for kind, group in aws_cloudwatch_log_group.this : kind => group.name }
}

output "container_secrets" {
  description = "For each task kind, the secrets its container receives: variable name to secret ARN."
  value = {
    for kind in local.kinds : kind => {
      for key, secret in local.secrets : secret.env => aws_secretsmanager_secret.this[key].arn
      if contains(secret.kinds, kind)
    }
  }
}

output "secret_names" {
  description = "Name of every secret whose value must be set by hand."
  value       = sort([for secret in aws_secretsmanager_secret.this : secret.name])
}

output "deploy_role_arn" {
  description = "Role the deploy workflow assumes."
  value       = aws_iam_role.deploy.arn
}
