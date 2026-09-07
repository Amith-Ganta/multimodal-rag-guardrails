# Variable values for the original dev account (651103158261).
#
#   terraform plan  -var-file=envs/dev.tfvars
#   terraform apply -var-file=envs/dev.tfvars
#
# This file is committed on purpose. It holds no secrets: a region and a set of
# sizing choices. Every account-specific name (the Velero bucket, IAM role
# ARNs, ECR URLs) is derived at apply time from the caller's account id rather
# than written down here, which is what lets a second account reuse this file
# almost unchanged.
#
# Note this is NOT terraform.tfvars, which is gitignored and auto-loaded. The
# named file must be passed with -var-file every time, which is deliberate:
# an apply that silently picks up whichever tfvars happens to be on disk is
# exactly how a plan ends up aimed at the wrong account.

aws_region = "us-east-1"

# Left at their variable defaults, restated here so the deployed shape of the
# dev cluster is readable in one place rather than spread across variables.tf.
project_name = "multimodal-rag"
cluster_name = "multimodal-rag-dev"
github_repo  = "Amith-Ganta/multimodal-rag-guardrails"

kubernetes_version      = "1.31"
node_instance_types     = ["t3.medium"]
node_group_desired_size = 2
node_group_min_size     = 2
node_group_max_size     = 4
