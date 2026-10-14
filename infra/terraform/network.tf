# A VPC with two public subnets (the NAT instance) and two private subnets (Lambda and RDS).
# Nothing in the private subnets is reachable from the internet. Outbound traffic (Anthropic,
# STS, CloudWatch Logs in users' accounts) leaves through the NAT instance.

resource "aws_vpc" "main" {
  cidr_block           = "10.20.0.0/16"
  enable_dns_support   = true
  enable_dns_hostnames = true
  tags                 = { Name = local.name }
}

resource "aws_internet_gateway" "main" {
  vpc_id = aws_vpc.main.id
  tags   = { Name = local.name }
}

resource "aws_subnet" "public" {
  count             = 2
  vpc_id            = aws_vpc.main.id
  cidr_block        = cidrsubnet(aws_vpc.main.cidr_block, 8, count.index)
  availability_zone = local.azs[count.index]
  tags              = { Name = "${local.name}-public-${count.index}" }
}

resource "aws_subnet" "private" {
  count             = 2
  vpc_id            = aws_vpc.main.id
  cidr_block        = cidrsubnet(aws_vpc.main.cidr_block, 8, count.index + 10)
  availability_zone = local.azs[count.index]
  tags              = { Name = "${local.name}-private-${count.index}" }
}

resource "aws_route_table" "public" {
  vpc_id = aws_vpc.main.id
  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.main.id
  }
  tags = { Name = "${local.name}-public" }
}

resource "aws_route_table_association" "public" {
  count          = 2
  subnet_id      = aws_subnet.public[count.index].id
  route_table_id = aws_route_table.public.id
}

# The private route table's default route is added by the fck-nat module.
resource "aws_route_table" "private" {
  vpc_id = aws_vpc.main.id
  tags   = { Name = "${local.name}-private" }
}

resource "aws_route_table_association" "private" {
  count          = 2
  subnet_id      = aws_subnet.private[count.index].id
  route_table_id = aws_route_table.private.id
}

# fck-nat: an open-source NAT image on a t4g.nano, about $3/month instead of a NAT Gateway's
# ~$33. ha_mode runs it in an auto scaling group of one, so a failed instance is replaced
# automatically (with a short outage while it starts).
module "nat" {
  source  = "RaJiska/fck-nat/aws"
  version = "1.6.1"

  name          = "${local.name}-nat"
  vpc_id        = aws_vpc.main.id
  subnet_id     = aws_subnet.public[0].id
  instance_type = var.nat_instance_type
  ha_mode       = true

  update_route_tables = true
  route_tables_ids    = { private = aws_route_table.private.id }
}

# The Lambda functions: outbound only.
resource "aws_security_group" "lambda" {
  name        = "${local.name}-lambda"
  description = "Log Inspector Lambda functions"
  vpc_id      = aws_vpc.main.id

  egress {
    description = "Outbound: Postgres, Anthropic, AWS APIs"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

# The database: Postgres from the Lambda functions only.
resource "aws_security_group" "database" {
  name        = "${local.name}-database"
  description = "Log Inspector Postgres"
  vpc_id      = aws_vpc.main.id

  ingress {
    description     = "Postgres from Lambda"
    from_port       = 5432
    to_port         = 5432
    protocol        = "tcp"
    security_groups = [aws_security_group.lambda.id]
  }
}
