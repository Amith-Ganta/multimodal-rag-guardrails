variable "aws_region" {
  description = "AWS region to deploy into. No default on purpose: this stack is meant to be replicable into a second AWS account, and a default region is the kind of thing that gets inherited by accident rather than chosen. Set it in the tfvars file for the target account."
  type        = string
}

variable "project_name" {
  description = "Short name used to prefix/tag every resource. Kept distinct from the banking_application_eks project's names so the two never collide."
  type        = string
  default     = "multimodal-rag"
}

variable "cluster_name" {
  description = "EKS cluster name. Must stay distinct from any other project's cluster (e.g. banking-dev)."
  type        = string
  default     = "multimodal-rag-dev"
}

variable "kubernetes_version" {
  description = "EKS control plane version."
  type        = string
  default     = "1.31"
}

variable "node_instance_types" {
  description = "Instance types for the managed node group. t3.medium chosen as the cheapest viable option per cost instruction; bump to t3.large if memory proves too tight for torch/transformers once verified."
  type        = list(string)
  default     = ["t3.medium"]
}

variable "node_group_desired_size" {
  description = "Desired node count at cluster creation."
  type        = number
  default     = 2
}

variable "node_group_min_size" {
  description = "Minimum node count (cluster-autoscaler floor)."
  type        = number
  default     = 2
}

variable "node_group_max_size" {
  description = "Maximum node count (cluster-autoscaler ceiling)."
  type        = number
  default     = 4
}

variable "github_repo" {
  description = "GitHub repo (owner/name) allowed to assume the CI IAM role via OIDC."
  type        = string
  default     = "Amith-Ganta/multimodal-rag-guardrails"
}

variable "velero_bucket_name" {
  description = "S3 bucket holding Velero backups. Leave empty and it is derived as <project_name>-velero-backups-<account id>, which is what makes this stack replicable: S3 names are globally unique, so a hardcoded one either collides in a second account or, worse, resolves to the first account's bucket and silently backs up into it. Set explicitly only when an existing bucket must be reused."
  type        = string
  default     = ""
}

variable "velero_bucket_force_destroy" {
  description = "Whether terraform destroy may delete the Velero bucket while it still holds backups. False everywhere that matters; true is only reasonable for a throwaway replication test."
  type        = bool
  default     = false
}

variable "eks_public_access_cidrs" {
  description = "CIDRs allowed to reach the EKS public API endpoint. Restrict to your own IP/CIDR when known; defaults open since the caller's outbound IP changes constantly (same posture as the EC2 stack's ssh_ingress_cidr)."
  type        = list(string)
  default     = ["0.0.0.0/0"]
}
