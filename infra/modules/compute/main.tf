# One image, three ways of running it: the API behind the load balancer, the worker, and a
# migration task that the deploy workflow starts once per release. The simulators are not
# deployed.

data "aws_region" "current" {}

# --- Image registry ------------------------------------------------------------------

resource "aws_ecr_repository" "this" {
  # checkov:skip=CKV_AWS_136:Encrypted with the key the registry owns. The image holds code that is already in the source repository and no secret.
  name                 = var.name
  image_tag_mutability = "IMMUTABLE"

  image_scanning_configuration {
    scan_on_push = true
  }
}

resource "aws_ecr_lifecycle_policy" "this" {
  repository = aws_ecr_repository.this.name

  policy = jsonencode({
    rules = [{
      rulePriority = 1
      description  = "Keep the last 20 images"
      selection = {
        tagStatus   = "any"
        countType   = "imageCountMoreThan"
        countNumber = 20
      }
      action = { type = "expire" }
    }]
  })
}

# --- Cluster and task definitions ----------------------------------------------------

resource "aws_ecs_cluster" "this" {
  # checkov:skip=CKV_AWS_65:Container Insights is billed per metric and is a variable, off by default; the application exports its own metrics.
  name = var.name

  setting {
    name  = "containerInsights"
    value = var.container_insights ? "enabled" : "disabled"
  }
}

locals {
  commands = {
    # The image's entrypoint is `corridor`. The API binds every address of the task's own
    # network interface, which only the load balancer's security group can reach.
    api     = ["serve", "--host", "0.0.0.0", "--port", tostring(var.api_port)]
    worker  = ["worker"]
    migrate = ["db", "migrate"]
  }

  container_definitions = {
    for kind, command in local.commands : kind => [{
      name      = kind
      image     = "${aws_ecr_repository.this.repository_url}:${var.image_tag}"
      essential = true
      command   = command

      portMappings = kind == "api" ? [{ containerPort = var.api_port, protocol = "tcp" }] : []

      environment = [
        for name in sort(keys(var.container_environment[kind])) :
        { name = name, value = var.container_environment[kind][name] }
      ]
      # Resolved by ECS when the task starts, with the kind's execution role. The values
      # are never in the task definition.
      secrets = [
        for name in sort(keys(var.container_secrets[kind])) :
        { name = name, valueFrom = var.container_secrets[kind][name] }
      ]

      # The code writes nothing to its own filesystem. Temporary files go to a volume that
      # lives as long as the task.
      readonlyRootFilesystem = true
      mountPoints            = [{ sourceVolume = "tmp", containerPath = "/tmp", readOnly = false }]
      linuxParameters        = { initProcessEnabled = true }
      # Time for requests in flight, or the outbox events a worker has claimed, to finish.
      stopTimeout = 60

      logConfiguration = {
        logDriver = "awslogs"
        options = {
          "awslogs-group"         = var.log_group_names[kind]
          "awslogs-region"        = data.aws_region.current.region
          "awslogs-stream-prefix" = kind
        }
      }
    }]
  }
}

resource "aws_ecs_task_definition" "this" {
  for_each = local.commands

  family                   = "${var.name}-${each.key}"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = var.task_cpu
  memory                   = var.task_memory_mib
  execution_role_arn       = var.execution_role_arns[each.key]
  task_role_arn            = var.task_role_arns[each.key]
  container_definitions    = jsonencode(local.container_definitions[each.key])

  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = "X86_64"
  }

  volume {
    name = "tmp"
  }
}

# --- Services ------------------------------------------------------------------------

resource "aws_ecs_service" "api" {
  name            = "${var.name}-api"
  cluster         = aws_ecs_cluster.this.id
  task_definition = aws_ecs_task_definition.this["api"].arn
  desired_count   = var.api_desired_count
  launch_type     = "FARGATE"

  # A new task must be healthy before an old one is stopped.
  deployment_minimum_healthy_percent = 100
  deployment_maximum_percent         = 200
  health_check_grace_period_seconds  = 60
  enable_execute_command             = false

  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }

  network_configuration {
    subnets          = var.private_subnet_ids
    security_groups  = [var.security_group_ids.api]
    assign_public_ip = false
  }

  load_balancer {
    target_group_arn = aws_lb_target_group.api.arn
    container_name   = "api"
    container_port   = var.api_port
  }

  # The deploy workflow registers a revision per release and points the service at it.
  # Terraform keeps the service, not which release it runs.
  lifecycle {
    ignore_changes = [task_definition]
  }

  depends_on = [aws_lb_listener.https]
}

resource "aws_ecs_service" "worker" {
  name            = "${var.name}-worker"
  cluster         = aws_ecs_cluster.this.id
  task_definition = aws_ecs_task_definition.this["worker"].arn
  desired_count   = var.worker_desired_count
  launch_type     = "FARGATE"

  # Workers claim work with SKIP LOCKED, so old and new may run side by side.
  deployment_minimum_healthy_percent = 100
  deployment_maximum_percent         = 200
  enable_execute_command             = false

  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }

  network_configuration {
    subnets          = var.private_subnet_ids
    security_groups  = [var.security_group_ids.worker]
    assign_public_ip = false
  }

  lifecycle {
    ignore_changes = [task_definition]
  }
}

# --- Load balancer -------------------------------------------------------------------

resource "aws_lb" "this" {
  # checkov:skip=CKV_AWS_91:Access logs need a bucket with its own policy and lifecycle; the API writes one access line per request to its own log.
  # checkov:skip=CKV2_AWS_28:No web application firewall. The API rate-limits by client address and validates every request; a firewall is a per-request cost to add when there is traffic to justify it.
  name               = var.name
  load_balancer_type = "application"
  internal           = false
  subnets            = var.public_subnet_ids
  security_groups    = [var.security_group_ids.alb]

  drop_invalid_header_fields = true
  enable_deletion_protection = var.alb_deletion_protection
  idle_timeout               = 60
}

resource "aws_lb_target_group" "api" {
  # checkov:skip=CKV_AWS_378:TLS ends at the load balancer. From there to the task is the VPC's private network, on a port only the load balancer's security group may reach.
  name_prefix = "api-"
  vpc_id      = var.vpc_id
  target_type = "ip"
  protocol    = "HTTP"
  port        = var.api_port

  deregistration_delay = 30

  health_check {
    path                = "/healthz"
    protocol            = "HTTP"
    matcher             = "200"
    interval            = 15
    timeout             = 5
    healthy_threshold   = 2
    unhealthy_threshold = 3
  }

  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_lb_listener" "https" {
  load_balancer_arn = aws_lb.this.arn
  port              = 443
  protocol          = "HTTPS"
  ssl_policy        = "ELBSecurityPolicy-TLS13-1-2-2021-06"
  certificate_arn   = var.certificate_arn

  # Anything that is not routed below gets this. /readyz, /metrics and the API's
  # documentation pages are served by the same process and must not be reachable from
  # outside, so the default is to answer for them here.
  default_action {
    type = "fixed-response"

    fixed_response {
      content_type = "application/json"
      status_code  = "404"
      message_body = jsonencode({ type = "about:blank", title = "Not Found", status = 404 })
    }
  }
}

resource "aws_lb_listener_rule" "api" {
  listener_arn = aws_lb_listener.https.arn
  priority     = 10

  action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.api.arn
  }

  condition {
    path_pattern {
      values = ["/v1/*", "/healthz"]
    }
  }
}

resource "aws_lb_listener" "http" {
  # checkov:skip=CKV_AWS_2:This listener forwards nothing; it answers every request with a redirect to HTTPS.
  load_balancer_arn = aws_lb.this.arn
  port              = 80
  protocol          = "HTTP"

  default_action {
    type = "redirect"

    redirect {
      protocol    = "HTTPS"
      port        = "443"
      status_code = "HTTP_301"
    }
  }
}
