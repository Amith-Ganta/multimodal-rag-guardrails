# Cluster add-ons: cluster-autoscaler (node scale-out/in), metrics-server
# (required by HPA), VPA (right-sizing), Goldilocks (VPA recommendation UI).

resource "kubernetes_service_account" "cluster_autoscaler" {
  metadata {
    name      = "cluster-autoscaler"
    namespace = "kube-system"
    annotations = {
      "eks.amazonaws.com/role-arn" = aws_iam_role.cluster_autoscaler.arn
    }
    labels = {
      "k8s-addon" = "cluster-autoscaler.addons.k8s.io"
      "k8s-app"   = "cluster-autoscaler"
    }
  }

  depends_on = [aws_eks_node_group.default]
}

resource "helm_release" "cluster_autoscaler" {
  name       = "cluster-autoscaler"
  repository = "https://kubernetes.github.io/autoscaler"
  chart      = "cluster-autoscaler"
  namespace  = "kube-system"
  version    = "9.37.0"

  set {
    name  = "autoDiscovery.clusterName"
    value = aws_eks_cluster.this.name
  }

  set {
    name  = "awsRegion"
    value = var.aws_region
  }

  set {
    name  = "rbac.serviceAccount.create"
    value = "false"
  }

  set {
    name  = "rbac.serviceAccount.name"
    value = kubernetes_service_account.cluster_autoscaler.metadata[0].name
  }

  set {
    name  = "extraArgs.balance-similar-node-groups"
    value = "true"
  }

  set {
    name  = "extraArgs.skip-nodes-with-system-pods"
    value = "false"
  }

  depends_on = [
    kubernetes_service_account.cluster_autoscaler,
    aws_eks_node_group.default,
  ]
}

resource "helm_release" "metrics_server" {
  name       = "metrics-server"
  repository = "https://kubernetes-sigs.github.io/metrics-server/"
  chart      = "metrics-server"
  namespace  = "kube-system"
  version    = "3.12.1"

  depends_on = [aws_eks_node_group.default]
}

resource "helm_release" "vpa" {
  name       = "vpa"
  repository = "https://charts.fairwinds.com/stable"
  chart      = "vpa"
  namespace  = "kube-system"
  version    = "4.5.0"

  set {
    name  = "recommender.enabled"
    value = "true"
  }

  set {
    name  = "updater.enabled"
    value = "true"
  }

  set {
    name  = "admissionController.enabled"
    value = "true"
  }

  depends_on = [aws_eks_node_group.default]
}

resource "kubernetes_namespace" "goldilocks" {
  metadata {
    name = "goldilocks"
  }

  depends_on = [aws_eks_node_group.default]
}

resource "helm_release" "goldilocks" {
  name       = "goldilocks"
  repository = "https://charts.fairwinds.com/stable"
  chart      = "goldilocks"
  namespace  = kubernetes_namespace.goldilocks.metadata[0].name
  version    = "11.1.0"

  set {
    name  = "dashboard.replicaCount"
    value = "1"
  }

  depends_on = [helm_release.vpa]
}

resource "kubernetes_labels" "goldilocks_target_default" {
  api_version = "v1"
  kind        = "Namespace"
  metadata {
    name = "default"
  }
  labels = {
    "goldilocks.fairwinds.com/enabled" = "true"
  }

  depends_on = [aws_eks_node_group.default]
}
