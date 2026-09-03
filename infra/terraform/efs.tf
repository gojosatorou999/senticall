# EFS for the CSV projector — single-writer atomic CSV path per §12.3.
#
# One file system shared across all private AZs. Throughput is
# "elastic" so an enrichment burst that rewrites the full CSV every
# 15 minutes doesn't starve concurrent NFS clients. Encryption in
# transit is mandatory (TLS-mount enforced by the ECS task's volume
# definition).

resource "aws_efs_file_system" "csv" {
  encrypted        = true
  throughput_mode  = "elastic"
  performance_mode = "generalPurpose"

  tags = { Name = "${local.name}-csv-efs" }
}

# Mount target in each private subnet so every ECS AZ can reach EFS
# without crossing the NAT.
resource "aws_efs_mount_target" "csv" {
  count           = length(var.azs)
  file_system_id  = aws_efs_file_system.csv.id
  subnet_id       = aws_subnet.private[count.index].id
  security_groups = [aws_security_group.efs.id]
}

# Access point scopes the CSV projector to /reports inside the FS.
# posix_user enforces a predictable UID/GID so `fcntl.flock` in the
# Python projector and the `aws_efs_file_system` encryption land on
# the same path regardless of how the task image was built.
resource "aws_efs_access_point" "reports" {
  file_system_id = aws_efs_file_system.csv.id

  posix_user {
    uid = 1000
    gid = 1000
  }

  root_directory {
    path = "/reports"
    creation_info {
      owner_uid   = 1000
      owner_gid   = 1000
      permissions = "755"
    }
  }

  tags = { Name = "${local.name}-reports-ap" }
}

output "efs_id" {
  value       = aws_efs_file_system.csv.id
  description = "EFS file-system ID — used in the ECS task volume definition"
}

output "efs_access_point_id" {
  value       = aws_efs_access_point.reports.id
  description = "EFS access-point ID for the /reports path"
}
