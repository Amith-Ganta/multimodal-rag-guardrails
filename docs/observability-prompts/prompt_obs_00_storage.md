# Patch 0 of 5: EBS CSI driver and a gp3 StorageClass

Run this BEFORE patch 4. Patch 4 creates PersistentVolumeClaims for Kafka,
Elasticsearch and Prometheus. Without this patch every one of them sits
`Pending` forever and the StatefulSets behind them never start.

## Why this patch exists

Measured on the live cluster, not assumed:

```
kubectl version              -> v1.31.14-eks-bca9cf6
kubectl get storageclass     -> gp2   kubernetes.io/aws-ebs   Delete   WaitForFirstConsumer
kubectl get pods -n kube-system | grep -iE "ebs|csi"  -> (nothing)
aws eks list-addons --cluster-name multimodal-rag-dev  -> {"addons": []}
kubectl get pvc -A           -> No resources found
```

The single StorageClass uses the **in-tree** provisioner
`kubernetes.io/aws-ebs`. In-tree cloud provider volume plugins were removed
from Kubernetes in 1.27 and this cluster runs 1.31, so nothing is listening on
that provisioner name. No PVC has ever been created here, which is why the gap
went unnoticed: the cluster looks healthy because nothing has exercised
storage.

The fix is the `aws-ebs-csi-driver` EKS managed addon plus a `gp3`
StorageClass marked default.

## Non-negotiable environment facts

Root module is `terraform/eks/`. Provider pins already set in `versions.tf`,
do not change them:

```hcl
required_version = ">= 1.10.0"
aws        ~> 5.0
kubernetes ~> 2.31
helm       ~> 2.14
tls        ~> 4.0
```

The `provider "helm"` block already exists and uses the **v2 nested
`kubernetes { }` block syntax**. Do not add another provider block, do not
convert it to v3 flat syntax, do not touch the S3 backend.

An OIDC provider for IRSA **already exists** and must be reused, not
recreated. It is at `terraform/eks/iam.tf` line 61:

```hcl
resource "aws_iam_openid_connect_provider" "eks" {
  url             = aws_eks_cluster.this.identity[0].oidc[0].issuer
  client_id_list  = ["sts.amazonaws.com"]
  thumbprint_list = [data.tls_certificate.eks.certificates[0].sha1_fingerprint]
}
```

Creating a second OIDC provider for the same issuer URL fails with
`EntityAlreadyExists`. Reference `aws_iam_openid_connect_provider.eks.arn`.

## The house IRSA pattern, copy it exactly

This is the working cluster-autoscaler role from `iam.tf` lines 73-100. Your
EBS CSI role must be the same shape with only the service account name
changed:

```hcl
data "aws_iam_policy_document" "cluster_autoscaler_assume" {
  statement {
    actions = ["sts:AssumeRoleWithWebIdentity"]
    effect  = "Allow"

    principals {
      type        = "Federated"
      identifiers = [aws_iam_openid_connect_provider.eks.arn]
    }

    condition {
      test     = "StringEquals"
      variable = "${replace(aws_iam_openid_connect_provider.eks.url, "https://", "")}:sub"
      values   = ["system:serviceaccount:kube-system:cluster-autoscaler"]
    }

    condition {
      test     = "StringEquals"
      variable = "${replace(aws_iam_openid_connect_provider.eks.url, "https://", "")}:aud"
      values   = ["sts.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "cluster_autoscaler" {
  name               = "${var.project_name}-cluster-autoscaler"
  assume_role_policy = data.aws_iam_policy_document.cluster_autoscaler_assume.json
}
```

Note the `sub` condition uses `StringEquals` against the exact
`system:serviceaccount:<namespace>:<serviceaccount>` string. For the EBS CSI
driver that value is **`system:serviceaccount:kube-system:ebs-csi-controller-sa`**.
Getting this string wrong produces a driver that starts cleanly, then fails
every single volume attach with an AccessDenied that only appears in the
controller's logs. Do not guess it, and do not use a wildcard.

## What to produce

### One new file: `terraform/eks/storage.tf`

Four resources, in this order.

**1. The IRSA role for the driver.**

An `aws_iam_policy_document` for assume-role following the pattern above with
the `ebs-csi-controller-sa` subject, an `aws_iam_role` named
`"${var.project_name}-ebs-csi-driver"`, and an
`aws_iam_role_policy_attachment` to the AWS managed policy
`arn:aws:iam::aws:policy/service-role/AmazonEBSCSIDriverPolicy`.

Use the AWS managed policy, do not hand-write the permission set. This is the
one case in this whole build where the managed policy is correct: the
permission list for EBS CSI is long, AWS updates it when the driver gains
features, and a hand-rolled copy silently rots.

**This is the only IRSA role in the entire observability build.** Kafka,
Elasticsearch, Fluent Bit, Prometheus and Grafana all stay inside the cluster
and call no AWS API, so they get no roles. The EBS CSI driver is different
because it genuinely calls the EC2 API to create, attach and detach volumes.

**2. The EKS managed addon.**

```hcl
resource "aws_eks_addon" "ebs_csi_driver" {
  cluster_name             = aws_eks_cluster.this.name
  addon_name               = "aws-ebs-csi-driver"
  addon_version            = "v1.65.0-eksbuild.1"
  service_account_role_arn = aws_iam_role.ebs_csi_driver.arn

  resolve_conflicts_on_create = "OVERWRITE"
  resolve_conflicts_on_update = "OVERWRITE"

  depends_on = [
    aws_eks_node_group.default,
    aws_iam_role_policy_attachment.ebs_csi_driver,
  ]
}
```

`v1.65.0-eksbuild.1` is **verified**, not recalled. It is the current default
version for Kubernetes 1.31, confirmed with:

```
aws eks describe-addon-versions --addon-name aws-ebs-csi-driver \
  --kubernetes-version 1.31 --region us-east-1
```

The `depends_on` the policy attachment matters. If the addon starts before its
role has permissions, the controller comes up unable to call EC2 and you get
the same silent attach failures as a wrong `sub` string.

**3. A gp3 StorageClass, marked default.**

```hcl
resource "kubernetes_storage_class" "gp3" {
  metadata {
    name = "gp3"
    annotations = {
      "storageclass.kubernetes.io/is-default-class" = "true"
    }
  }

  storage_provisioner    = "ebs.csi.aws.com"
  reclaim_policy         = "Delete"
  volume_binding_mode    = "WaitForFirstConsumer"
  allow_volume_expansion = true

  parameters = {
    type      = "gp3"
    encrypted = "true"
  }

  depends_on = [aws_eks_addon.ebs_csi_driver]
}
```

Four choices to comment, each with its reason:

- `WaitForFirstConsumer`, not `Immediate`. EBS volumes are bound to one
  availability zone. Binding immediately picks a zone before the scheduler has
  chosen a node, and roughly two times in three on a three-AZ cluster the pod
  is then unschedulable because its volume is in the wrong zone.
- `allow_volume_expansion = true`. The existing gp2 class has this false. A
  full Elasticsearch volume that cannot grow means deleting the PVC and losing
  the data.
- `encrypted = "true"`. Encryption at rest costs nothing on EBS and its
  absence is a finding in any security review.
- `reclaim_policy = "Delete"`. This is a dev cluster that gets torn down.
  Retain would leave orphaned volumes billing after `terraform destroy`. Say
  plainly in the comment that a production cluster would use Retain.

**4. Remove the default annotation from gp2.**

Two default StorageClasses is an error state: the API server picks
arbitrarily and PVCs land on whichever it happened to choose. Check first
whether `gp2` actually carries the default annotation. If it does, use
`kubernetes_annotations` to set
`"storageclass.kubernetes.io/is-default-class" = "false"` on it, with
`force = true`, since the annotation is managed by EKS rather than by
Terraform. If it does not carry the annotation, omit this resource entirely
and say so, rather than adding a resource that fights a field nothing set.

### Two outputs, appended to `terraform/eks/outputs.tf`

Match the style of the existing `velero_backup_status_command` output. One
naming the default StorageClass, one giving the command to verify the driver
is actually running:

```hcl
output "ebs_csi_driver_status_command" {
  value = "kubectl -n kube-system get pods -l app.kubernetes.io/name=aws-ebs-csi-driver"
}
```

Emit only the added blocks for `outputs.tf`, not the whole file.

## Hard constraints

- Do not modify `cluster.tf`, `versions.tf`, `variables.tf`, `velero.tf`,
  `argocd.tf`, `addons.tf` or `iam.tf`. Everything new goes in `storage.tf`
  except the two outputs.
- Do not create a second `aws_iam_openid_connect_provider`.
- Do not delete or replace the existing `gp2` StorageClass. StorageClasses are
  immutable in the fields that matter, and PVs already reference it. Only the
  default annotation changes.
- `terraform validate` and `terraform fmt` must both be clean.
- Comments explain WHY, not WHAT, matching the voice already in `velero.tf`
  and `addons.tf`.
- No em dash characters anywhere.
- Emit `storage.tf` as one complete file in a fenced block with its exact
  path.
