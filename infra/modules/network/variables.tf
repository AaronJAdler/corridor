variable "name" {
  description = "Prefix for every resource name."
  type        = string
}

variable "vpc_cidr" {
  description = "Address range of the VPC. Four /20 subnets are cut from it."
  type        = string
}

variable "single_nat_gateway" {
  description = "One NAT gateway for both zones (cheaper) instead of one per zone (survives the loss of a zone)."
  type        = bool
}

variable "ingress_cidrs" {
  description = "Address ranges that may reach the load balancer."
  type        = list(string)
}

variable "api_port" {
  description = "Port the API container listens on."
  type        = number
}
