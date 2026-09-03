# VPC + subnets + NAT + IGW per spec §14.1.
#
# Layout: 2 AZs, /20 subnets each. Public subnets host the ALB + NAT
# GWs; private subnets host every runtime task, RDS, and Redis. RAG
# snapshots live in S3, accessed via a VPC endpoint (no NAT egress on
# hot-path fetches).

locals {
  name = "fg-voice-${var.environment}"
  # /20 per AZ — 4094 usable, well-headroom for surge scaling.
  public_subnets  = ["10.30.0.0/20", "10.30.16.0/20"]
  private_subnets = ["10.30.32.0/20", "10.30.48.0/20"]
  # Data subnets (RDS + Redis) — /22 is plenty; kept separate so a
  # future security-group audit can trivially assert "nothing outside
  # the data subnets talks to RDS on 5432".
  data_subnets = ["10.30.64.0/22", "10.30.68.0/22"]
}

resource "aws_vpc" "main" {
  cidr_block           = var.vpc_cidr
  enable_dns_hostnames = true
  enable_dns_support   = true
  tags                 = { Name = "${local.name}-vpc" }
}

resource "aws_internet_gateway" "main" {
  vpc_id = aws_vpc.main.id
  tags   = { Name = "${local.name}-igw" }
}

resource "aws_subnet" "public" {
  count                   = length(var.azs)
  vpc_id                  = aws_vpc.main.id
  cidr_block              = local.public_subnets[count.index]
  availability_zone       = var.azs[count.index]
  map_public_ip_on_launch = true
  tags                    = { Name = "${local.name}-public-${var.azs[count.index]}" }
}

resource "aws_subnet" "private" {
  count             = length(var.azs)
  vpc_id            = aws_vpc.main.id
  cidr_block        = local.private_subnets[count.index]
  availability_zone = var.azs[count.index]
  tags              = { Name = "${local.name}-private-${var.azs[count.index]}" }
}

resource "aws_subnet" "data" {
  count             = length(var.azs)
  vpc_id            = aws_vpc.main.id
  cidr_block        = local.data_subnets[count.index]
  availability_zone = var.azs[count.index]
  tags              = { Name = "${local.name}-data-${var.azs[count.index]}" }
}

resource "aws_eip" "nat" {
  count      = length(var.azs)
  domain     = "vpc"
  depends_on = [aws_internet_gateway.main]
  tags       = { Name = "${local.name}-nat-eip-${var.azs[count.index]}" }
}

resource "aws_nat_gateway" "main" {
  count         = length(var.azs)
  allocation_id = aws_eip.nat[count.index].id
  subnet_id     = aws_subnet.public[count.index].id
  tags          = { Name = "${local.name}-nat-${var.azs[count.index]}" }
}

resource "aws_route_table" "public" {
  vpc_id = aws_vpc.main.id
  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.main.id
  }
  tags = { Name = "${local.name}-public-rt" }
}

resource "aws_route_table_association" "public" {
  count          = length(var.azs)
  subnet_id      = aws_subnet.public[count.index].id
  route_table_id = aws_route_table.public.id
}

resource "aws_route_table" "private" {
  count  = length(var.azs)
  vpc_id = aws_vpc.main.id
  route {
    cidr_block     = "0.0.0.0/0"
    nat_gateway_id = aws_nat_gateway.main[count.index].id
  }
  tags = { Name = "${local.name}-private-rt-${var.azs[count.index]}" }
}

resource "aws_route_table_association" "private" {
  count          = length(var.azs)
  subnet_id      = aws_subnet.private[count.index].id
  route_table_id = aws_route_table.private[count.index].id
}

resource "aws_route_table_association" "data" {
  count          = length(var.azs)
  subnet_id      = aws_subnet.data[count.index].id
  # Data subnets share the private route so scheduled migrations from
  # the ECS side can talk to RDS without opening internet egress.
  route_table_id = aws_route_table.private[count.index].id
}

# VPC endpoints — keep S3 (RAG snapshots, recordings), Secrets Manager,
# and ECR/CloudWatch traffic off the NAT (saves per-GB egress + tighter
# blast radius on a NAT outage).

resource "aws_vpc_endpoint" "s3" {
  vpc_id            = aws_vpc.main.id
  service_name      = "com.amazonaws.${var.region}.s3"
  vpc_endpoint_type = "Gateway"
  route_table_ids   = aws_route_table.private[*].id
  tags              = { Name = "${local.name}-s3-endpoint" }
}
