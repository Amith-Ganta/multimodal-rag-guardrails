# The backup bucket used to be created by hand and only referenced here by a
# hardcoded name. That is the single worst kind of gap for a stack meant to be
# replicated: terraform apply succeeds in a fresh account, every resource
# reports created, Velero installs and schedules its nightly job, and the
# backups fail against a bucket that does not exist. Nothing in the apply output
# says so. Declaring it here means the target either exists or the apply fails.
#
# The name carries the account id because S3 names are global. Without the
# suffix a second account cannot create the bucket at all, and the IAM policy
# below would grant the replica's Velero write access to this account's backups.
locals {
  velero_bucket_name = coalesce(
    var.velero_bucket_name,
    "${var.project_name}-velero-backups-${data.aws_caller_identity.current.account_id}",
  )
}

resource "aws_s3_bucket" "velero" {
  bucket = local.velero_bucket_name

  # Refuse to delete a bucket that still holds backups unless explicitly told
  # otherwise. A destroy that silently takes the backups with it defeats the
  # point of having them.
  force_destroy = var.velero_bucket_force_destroy

  tags = {
    Project = var.project_name
  }
}

# Velero writes one object per backup and never rewrites one, so versioning is
# not about recovering overwrites here. It is about surviving a delete: an
# accidental or malicious removal of a backup object leaves a delete marker
# rather than losing the data.
resource "aws_s3_bucket_versioning" "velero" {
  bucket = aws_s3_bucket.velero.id

  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "velero" {
  bucket = aws_s3_bucket.velero.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }

    # Stated rather than left to the default. S3 now enables bucket keys on new
    # buckets by default, so omitting this makes Terraform propose turning it
    # off on any bucket that already has it, which is a downgrade dressed up as
    # a no-op diff. It costs nothing under AES256 and matters if this ever moves
    # to SSE-KMS, where it collapses per-object KMS calls into one per key.
    bucket_key_enabled = true
  }
}

# Cluster backups contain every Secret in every namespace. Public access here
# would be a full credential disclosure, so all four switches go on rather than
# relying on the account-level default, which a second account may not share.
resource "aws_s3_bucket_public_access_block" "velero" {
  bucket = aws_s3_bucket.velero.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# Velero's own ttl (168h) expires the backup records, but the objects behind an
# expired backup are removed by Velero, not by S3. Versioning above means each
# removal leaves a noncurrent version that would otherwise accumulate forever.
resource "aws_s3_bucket_lifecycle_configuration" "velero" {
  bucket = aws_s3_bucket.velero.id

  rule {
    id     = "expire-noncurrent-backups"
    status = "Enabled"

    filter {}

    noncurrent_version_expiration {
      noncurrent_days = 30
    }

    abort_incomplete_multipart_upload {
      days_after_initiation = 7
    }
  }

  depends_on = [aws_s3_bucket_versioning.velero]
}

data "aws_iam_policy_document" "velero_assume" {
  statement {
    actions = ["sts:AssumeRoleWithWebIdentity"]
    effect  = "Allow"

    principals {
      type        = "Federated"
      identifiers = [aws_iam_openid_connect_provider.eks.arn]
    }

    condition {
      test     = "StringEquals"
      variable = "${replace(aws_iam_openid_connect_provider.eks.url, "https://", "")}:aud"
      values   = ["sts.amazonaws.com"]
    }

    condition {
      test     = "StringEquals"
      variable = "${replace(aws_iam_openid_connect_provider.eks.url, "https://", "")}:sub"
      values   = ["system:serviceaccount:velero:velero"]
    }
  }
}

data "aws_iam_policy_document" "velero" {
  statement {
    effect = "Allow"
    actions = [
      "s3:GetObject",
      "s3:DeleteObject",
      "s3:PutObject",
      "s3:AbortMultipartUpload",
      "s3:ListMultipartUploadParts",
    ]
    resources = [
      "${aws_s3_bucket.velero.arn}/*",
    ]
  }

  statement {
    effect = "Allow"
    actions = [
      "s3:ListBucket",
      "s3:GetBucketLocation",
      "s3:ListBucketMultipartUploads",
    ]
    resources = [
      aws_s3_bucket.velero.arn,
    ]
  }

  statement {
    effect = "Allow"
    actions = [
      "ec2:DescribeVolumes",
      "ec2:DescribeSnapshots",
      "ec2:CreateTags",
      "ec2:DeleteTags",
      "ec2:CreateSnapshot",
      "ec2:DeleteSnapshot",
    ]
    resources = ["*"]
  }
}

resource "aws_iam_role" "velero" {
  name               = "${var.cluster_name}-velero"
  assume_role_policy = data.aws_iam_policy_document.velero_assume.json

  tags = {
    Project = var.project_name
  }
}

resource "aws_iam_role_policy" "velero" {
  name   = "velero-backup"
  role   = aws_iam_role.velero.id
  policy = data.aws_iam_policy_document.velero.json
}

resource "kubernetes_namespace" "velero" {
  metadata {
    name = "velero"
  }

  depends_on = [aws_eks_node_group.default]
}

resource "helm_release" "velero" {
  name       = "velero"
  repository = "https://vmware-tanzu.github.io/helm-charts"
  chart      = "velero"
  namespace  = kubernetes_namespace.velero.metadata[0].name
  version    = "12.1.0"

  values = [
    yamlencode({
      credentials = {
        useSecret = false
      }

      configuration = {
        backupStorageLocation = [
          {
            name     = "default"
            provider = "aws"
            bucket   = aws_s3_bucket.velero.id
            default  = true
            config = {
              region = var.aws_region
            }
          }
        ]

        volumeSnapshotLocation = [
          {
            name     = "default"
            provider = "aws"
            default  = true
            config = {
              region = var.aws_region
            }
          }
        ]
      }

      schedules = {
        daily-backup = {
          disabled                   = false
          schedule                   = "0 1 * * *"
          useOwnerReferencesInBackup = false
          paused                     = false
          template = {
            ttl                = "168h0m0s"
            includedNamespaces = ["*"]
            excludedNamespaces = ["kube-system", "velero"]
          }
        }
      }

      serviceAccount = {
        server = {
          create = true
          name   = "velero"
          annotations = {
            "eks.amazonaws.com/role-arn" = aws_iam_role.velero.arn
          }
        }
      }

      initContainers = [
        {
          name            = "velero-plugin-for-aws"
          image           = "velero/velero-plugin-for-aws:v1.13.1"
          imagePullPolicy = "IfNotPresent"
          volumeMounts = [
            {
              mountPath = "/target"
              name      = "plugins"
            }
          ]
        }
      ]

      resources = {
        requests = {
          cpu    = "100m"
          memory = "256Mi"
        }
        limits = {
          cpu    = "500m"
          memory = "512Mi"
        }
      }
    })
  ]

  # The bucket must not merely exist before Velero starts writing to it, it must
  # already be locked down. Creating the bucket and hardening it are separate
  # resources, so without this the first nightly backup can land in a window
  # where public access is not yet blocked.
  depends_on = [
    aws_iam_role_policy.velero,
    aws_s3_bucket_public_access_block.velero,
    aws_s3_bucket_server_side_encryption_configuration.velero,
  ]
}

output "velero_backup_status_command" {
  description = "One-liner to check Velero backup status"
  value       = "kubectl -n velero get backups"
}
