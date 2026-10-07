# One VPC across two zones. Only the load balancer has a public address. Tasks and data
# stores sit in private subnets and reach the internet (the image registry, the secret
# store, the log service, the providers) through a NAT gateway.

data "aws_availability_zones" "available" {
  # checkov:skip=CKV_AWS_394:The region is a variable, so no zone can be named here. Only the first two ordinary zones are used, and a zone AWS adds later sorts after them.
  state = "available"

  # Ordinary zones only, not Local Zones or Wavelength Zones.
  filter {
    name   = "opt-in-status"
    values = ["opt-in-not-required"]
  }
}

locals {
  zones = slice(data.aws_availability_zones.available.names, 0, 2)
}

resource "aws_vpc" "this" {
  # checkov:skip=CKV2_AWS_11:Flow logs are left out of a stack built to be created for a demonstration and destroyed; they cost per gigabyte and nothing here reads them.
  cidr_block           = var.vpc_cidr
  enable_dns_support   = true
  enable_dns_hostnames = true

  tags = { Name = var.name }
}

# The default security group is adopted and emptied, so that a resource created without
# naming a group gets one that admits and sends nothing.
resource "aws_default_security_group" "this" {
  vpc_id = aws_vpc.this.id

  tags = { Name = "${var.name}-default-unused" }
}

resource "aws_internet_gateway" "this" {
  vpc_id = aws_vpc.this.id

  tags = { Name = var.name }
}

resource "aws_subnet" "public" {
  count = 2

  vpc_id            = aws_vpc.this.id
  availability_zone = local.zones[count.index]
  cidr_block        = cidrsubnet(var.vpc_cidr, 4, count.index)
  # Nothing launched here gets a public address unless it asks for one; the load balancer
  # and the NAT gateway do.
  map_public_ip_on_launch = false

  tags = { Name = "${var.name}-public-${local.zones[count.index]}" }
}

resource "aws_subnet" "private" {
  count = 2

  vpc_id                  = aws_vpc.this.id
  availability_zone       = local.zones[count.index]
  cidr_block              = cidrsubnet(var.vpc_cidr, 4, count.index + 8)
  map_public_ip_on_launch = false

  tags = { Name = "${var.name}-private-${local.zones[count.index]}" }
}

resource "aws_route_table" "public" {
  vpc_id = aws_vpc.this.id

  tags = { Name = "${var.name}-public" }
}

resource "aws_route" "public_internet" {
  route_table_id         = aws_route_table.public.id
  destination_cidr_block = "0.0.0.0/0"
  gateway_id             = aws_internet_gateway.this.id
}

resource "aws_route_table_association" "public" {
  count = 2

  subnet_id      = aws_subnet.public[count.index].id
  route_table_id = aws_route_table.public.id
}

locals {
  nat_count = var.single_nat_gateway ? 1 : 2
}

resource "aws_eip" "nat" {
  count = local.nat_count

  domain = "vpc"

  tags = { Name = "${var.name}-nat-${count.index}" }
}

resource "aws_nat_gateway" "this" {
  count = local.nat_count

  allocation_id = aws_eip.nat[count.index].id
  subnet_id     = aws_subnet.public[count.index].id

  tags = { Name = "${var.name}-${count.index}" }

  depends_on = [aws_internet_gateway.this]
}

resource "aws_route_table" "private" {
  count = 2

  vpc_id = aws_vpc.this.id

  tags = { Name = "${var.name}-private-${local.zones[count.index]}" }
}

resource "aws_route" "private_nat" {
  count = 2

  route_table_id         = aws_route_table.private[count.index].id
  destination_cidr_block = "0.0.0.0/0"
  nat_gateway_id         = aws_nat_gateway.this[var.single_nat_gateway ? 0 : count.index].id
}

resource "aws_route_table_association" "private" {
  count = 2

  subnet_id      = aws_subnet.private[count.index].id
  route_table_id = aws_route_table.private[count.index].id
}

# Security groups. Every rule names the group on the other side, so the paths that exist
# are exactly: internet -> load balancer -> API; API, worker and migration -> database;
# API and worker -> cache; tasks -> internet over HTTPS.

resource "aws_security_group" "alb" {
  # checkov:skip=CKV2_AWS_5:Attached in another module (the load balancer, a service, the migration task run by the deploy workflow, or a data store), where this check does not look.
  name        = "${var.name}-alb"
  description = "Load balancer: HTTPS and the HTTP redirect in, the API port out"
  vpc_id      = aws_vpc.this.id

  tags = { Name = "${var.name}-alb" }
}

resource "aws_security_group" "api" {
  # checkov:skip=CKV2_AWS_5:Attached in another module (the load balancer, a service, the migration task run by the deploy workflow, or a data store), where this check does not look.
  name        = "${var.name}-api"
  description = "API tasks: reached only by the load balancer"
  vpc_id      = aws_vpc.this.id

  tags = { Name = "${var.name}-api" }
}

resource "aws_security_group" "worker" {
  # checkov:skip=CKV2_AWS_5:Attached in another module (the load balancer, a service, the migration task run by the deploy workflow, or a data store), where this check does not look.
  name        = "${var.name}-worker"
  description = "Worker tasks: nothing connects to them"
  vpc_id      = aws_vpc.this.id

  tags = { Name = "${var.name}-worker" }
}

resource "aws_security_group" "migrate" {
  # checkov:skip=CKV2_AWS_5:Attached in another module (the load balancer, a service, the migration task run by the deploy workflow, or a data store), where this check does not look.
  name        = "${var.name}-migrate"
  description = "One-off migration task: nothing connects to it"
  vpc_id      = aws_vpc.this.id

  tags = { Name = "${var.name}-migrate" }
}

resource "aws_security_group" "database" {
  # checkov:skip=CKV2_AWS_5:Attached in another module (the load balancer, a service, the migration task run by the deploy workflow, or a data store), where this check does not look.
  name        = "${var.name}-database"
  description = "PostgreSQL: reached only by the API, the worker and the migration task"
  vpc_id      = aws_vpc.this.id

  tags = { Name = "${var.name}-database" }
}

resource "aws_security_group" "cache" {
  # checkov:skip=CKV2_AWS_5:Attached in another module (the load balancer, a service, the migration task run by the deploy workflow, or a data store), where this check does not look.
  name        = "${var.name}-cache"
  description = "Redis: reached only by the API and the worker"
  vpc_id      = aws_vpc.this.id

  tags = { Name = "${var.name}-cache" }
}

resource "aws_vpc_security_group_ingress_rule" "alb_https" {
  for_each = toset(var.ingress_cidrs)

  security_group_id = aws_security_group.alb.id
  description       = "HTTPS from clients"
  cidr_ipv4         = each.value
  ip_protocol       = "tcp"
  from_port         = 443
  to_port           = 443
}

resource "aws_vpc_security_group_ingress_rule" "alb_http" {
  # checkov:skip=CKV_AWS_260:Port 80 is open only so that the listener can answer with a redirect to HTTPS; nothing is forwarded from it.
  for_each = toset(var.ingress_cidrs)

  security_group_id = aws_security_group.alb.id
  description       = "HTTP from clients, answered with a redirect to HTTPS"
  cidr_ipv4         = each.value
  ip_protocol       = "tcp"
  from_port         = 80
  to_port           = 80
}

resource "aws_vpc_security_group_egress_rule" "alb_to_api" {
  security_group_id            = aws_security_group.alb.id
  description                  = "Requests and health checks to the API"
  referenced_security_group_id = aws_security_group.api.id
  ip_protocol                  = "tcp"
  from_port                    = var.api_port
  to_port                      = var.api_port
}

resource "aws_vpc_security_group_ingress_rule" "api_from_alb" {
  security_group_id            = aws_security_group.api.id
  description                  = "Requests and health checks from the load balancer"
  referenced_security_group_id = aws_security_group.alb.id
  ip_protocol                  = "tcp"
  from_port                    = var.api_port
  to_port                      = var.api_port
}

locals {
  # The groups whose tasks start from an image and so need the registry, the secret store
  # and the log service; the API and the worker also call providers. All of it is HTTPS.
  task_groups = {
    api     = aws_security_group.api.id
    worker  = aws_security_group.worker.id
    migrate = aws_security_group.migrate.id
  }
  cache_clients = {
    api    = aws_security_group.api.id
    worker = aws_security_group.worker.id
  }
}

resource "aws_vpc_security_group_egress_rule" "task_https" {
  for_each = local.task_groups

  security_group_id = each.value
  description       = "HTTPS out through the NAT gateway: AWS services and providers"
  cidr_ipv4         = "0.0.0.0/0"
  ip_protocol       = "tcp"
  from_port         = 443
  to_port           = 443
}

resource "aws_vpc_security_group_egress_rule" "task_to_database" {
  for_each = local.task_groups

  security_group_id            = each.value
  description                  = "PostgreSQL"
  referenced_security_group_id = aws_security_group.database.id
  ip_protocol                  = "tcp"
  from_port                    = 5432
  to_port                      = 5432
}

resource "aws_vpc_security_group_ingress_rule" "database_from_task" {
  for_each = local.task_groups

  security_group_id            = aws_security_group.database.id
  description                  = "PostgreSQL from the ${each.key} tasks"
  referenced_security_group_id = each.value
  ip_protocol                  = "tcp"
  from_port                    = 5432
  to_port                      = 5432
}

resource "aws_vpc_security_group_egress_rule" "task_to_cache" {
  for_each = local.cache_clients

  security_group_id            = each.value
  description                  = "Redis"
  referenced_security_group_id = aws_security_group.cache.id
  ip_protocol                  = "tcp"
  from_port                    = 6379
  to_port                      = 6379
}

resource "aws_vpc_security_group_ingress_rule" "cache_from_task" {
  for_each = local.cache_clients

  security_group_id            = aws_security_group.cache.id
  description                  = "Redis from the ${each.key} tasks"
  referenced_security_group_id = each.value
  ip_protocol                  = "tcp"
  from_port                    = 6379
  to_port                      = 6379
}
