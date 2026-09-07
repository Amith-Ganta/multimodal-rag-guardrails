output "cluster_name" {
  value = aws_eks_cluster.this.name
}

output "cluster_endpoint" {
  value = aws_eks_cluster.this.endpoint
}

output "cluster_certificate_authority_data" {
  value = aws_eks_cluster.this.certificate_authority[0].data
}

output "region" {
  value = var.aws_region
}

output "github_actions_role_arn" {
  description = "Put this in the GitHub Actions workflow's role-to-assume input. Not a secret."
  value       = aws_iam_role.github_actions.arn
}

output "ecr_ui_repository_url" {
  value = aws_ecr_repository.ui.repository_url
}

output "ecr_api_repository_url" {
  value = aws_ecr_repository.api.repository_url
}

output "vpc_id" {
  value = aws_vpc.this.id
}

output "public_subnet_ids" {
  value = aws_subnet.public[*].id
}

output "default_storage_class" {
  value = kubernetes_storage_class.gp3.metadata[0].name
}

output "ebs_csi_driver_status_command" {
  value = "kubectl -n kube-system get pods -l app.kubernetes.io/name=aws-ebs-csi-driver"
}
