# Security groups. One SG per role; ingress is always by SG reference
# (never CIDR) so an audit can trace exactly which service reaches
# which port, and adding a new AZ doesn't require SG edits.

resource "aws_security_group" "alb" {
  name        = "${local.name}-alb"
  description = "Public ALB — 443 in from internet, WSS support"
  vpc_id      = aws_vpc.main.id

  ingress {
    description = "HTTPS from anywhere (WAF handles rate limits)"
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

resource "aws_security_group" "voice_task" {
  name        = "${local.name}-voice-task"
  description = "fg-voice-agent + fg-voice-api tasks"
  vpc_id      = aws_vpc.main.id

  ingress {
    description     = "ALB → task on 8080"
    from_port       = 8080
    to_port         = 8080
    protocol        = "tcp"
    security_groups = [aws_security_group.alb.id]
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

resource "aws_security_group" "rds" {
  name        = "${local.name}-rds"
  description = "RDS PostgreSQL — only from voice tasks"
  vpc_id      = aws_vpc.main.id

  ingress {
    description     = "voice tasks → PostgreSQL"
    from_port       = 5432
    to_port         = 5432
    protocol        = "tcp"
    security_groups = [aws_security_group.voice_task.id]
  }
}

resource "aws_security_group" "redis" {
  name        = "${local.name}-redis"
  description = "ElastiCache Redis — only from voice tasks"
  vpc_id      = aws_vpc.main.id

  ingress {
    description     = "voice tasks → Redis"
    from_port       = 6379
    to_port         = 6379
    protocol        = "tcp"
    security_groups = [aws_security_group.voice_task.id]
  }
}

resource "aws_security_group" "efs" {
  name        = "${local.name}-efs"
  description = "EFS mount targets — only from voice tasks (CSV projector)"
  vpc_id      = aws_vpc.main.id

  ingress {
    description     = "voice tasks → NFS"
    from_port       = 2049
    to_port         = 2049
    protocol        = "tcp"
    security_groups = [aws_security_group.voice_task.id]
  }
}
