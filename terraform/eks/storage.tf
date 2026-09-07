# Persistent storage for the cluster.
#
# Until this file existed, this cluster could not bind a single PersistentVolumeClaim.
# Its only StorageClass was the EKS-supplied "gp2", which uses the in-tree provisioner
# "kubernetes.io/aws-ebs". In-tree cloud volume plugins were removed from Kubernetes in
# 1.27 and this cluster runs 1.31, so nothing was listening on that provisioner name.
# The gap stayed invisible because no workload here had ever asked for storage: the
# first PVC would simply have sat Pending forever with no error anywhere else.
#
# Kafka, Elasticsearch and Prometheus all want real volumes, so the driver goes in first.

# The EBS CSI controller calls the EC2 API to create, attach and detach volumes, so it
# needs a real IAM identity. This is the ONLY IRSA role in the observability build:
# Kafka, Elasticsearch, Fluent Bit, Prometheus and Grafana all stay inside the cluster
# and call no AWS API, so giving them roles would be cargo cult.
data "aws_iam_policy_document" "ebs_csi_driver_assume" {
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

    # This exact string is what the addon's service account presents. Get it wrong and
    # the driver still starts cleanly, then fails every single volume attach with an
    # AccessDenied that appears only in the controller's own logs. No wildcards.
    condition {
      test     = "StringEquals"
      variable = "${replace(aws_iam_openid_connect_provider.eks.url, "https://", "")}:sub"
      values   = ["system:serviceaccount:kube-system:ebs-csi-controller-sa"]
    }
  }
}

resource "aws_iam_role" "ebs_csi_driver" {
  name               = "${var.project_name}-ebs-csi-driver"
  assume_role_policy = data.aws_iam_policy_document.ebs_csi_driver_assume.json
}

# The AWS managed policy is correct here, unlike elsewhere in this module where policies
# are written out explicitly. The EBS CSI permission set is long, AWS extends it when the
# driver gains features, and a hand-rolled copy silently rots into broken attaches.
resource "aws_iam_role_policy_attachment" "ebs_csi_driver" {
  role       = aws_iam_role.ebs_csi_driver.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonEBSCSIDriverPolicy"
}

# v1.65.0-eksbuild.1 is the current default version for Kubernetes 1.31, confirmed with
# aws eks describe-addon-versions rather than recalled. Pinning it means a cluster rebuild
# reproduces this exact driver instead of whatever is newest that day.
resource "aws_eks_addon" "ebs_csi_driver" {
  cluster_name             = aws_eks_cluster.this.name
  addon_name               = "aws-ebs-csi-driver"
  addon_version            = "v1.65.0-eksbuild.1"
  service_account_role_arn = aws_iam_role.ebs_csi_driver.arn

  resolve_conflicts_on_create = "OVERWRITE"
  resolve_conflicts_on_update = "OVERWRITE"

  # The dependency on the policy attachment is load-bearing, not tidiness. An addon that
  # starts before its role has permissions produces exactly the same silent attach
  # failures as a wrong subject string above.
  depends_on = [
    aws_eks_node_group.default,
    aws_iam_role_policy_attachment.ebs_csi_driver,
  ]
}

resource "kubernetes_storage_class" "gp3" {
  metadata {
    name = "gp3"
    annotations = {
      # gp2 was checked and carries no default-class annotation, so this becomes the sole
      # default with nothing to unset. Two defaults would be an error state: the API server
      # picks arbitrarily and PVCs land on whichever it happened to choose.
      "storageclass.kubernetes.io/is-default-class" = "true"
    }
  }

  storage_provisioner = "ebs.csi.aws.com"

  # Delete, not Retain, because this is a dev cluster that gets torn down. Retain would
  # leave orphaned volumes billing after terraform destroy. A production cluster holding
  # data anyone cares about would use Retain and accept the cleanup burden.
  reclaim_policy = "Delete"

  # EBS volumes live in exactly one availability zone. Immediate binding would pick a zone
  # before the scheduler picks a node, so on a multi-AZ cluster the pod would frequently
  # be unschedulable because its volume landed somewhere else.
  volume_binding_mode = "WaitForFirstConsumer"

  # The existing gp2 class has this false. A full Elasticsearch volume that cannot grow
  # means deleting the PVC and losing the data, which is a bad way to learn the setting.
  allow_volume_expansion = true

  parameters = {
    type = "gp3"
    # Encryption at rest is free on EBS and its absence is a finding in any review.
    encrypted = "true"
  }

  depends_on = [aws_eks_addon.ebs_csi_driver]
}
