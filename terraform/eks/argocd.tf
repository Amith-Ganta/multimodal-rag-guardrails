# ArgoCD namespace; creation is gated on the managed node group like the other addons.
resource "kubernetes_namespace" "argocd" {
  metadata {
    name = "argocd"
  }

  depends_on = [aws_eks_node_group.default]
}

# ArgoCD install sized for the current 2 x t3.medium cluster.
#
# Component requests are explicitly packed so this fits on the two existing nodes:
#   node 1: application-controller (StatefulSet) + server (Deployment)
#   node 2: repo-server (Deployment) + redis (Deployment)
# Kubernetes schedules per node, not against the cluster total, so each pod has to
# fit inside one node's remaining headroom rather than the sum of both.
#
# Dex and notifications are disabled because this is a single-repo, single-Application
# GitOps setup with no SSO and no Slack/email target. The applicationset-controller has
# no enable/install toggle in chart 10.x and is always rendered, so its cost is shed by
# scaling it to zero replicas instead.
resource "helm_release" "argocd" {
  name       = "argocd"
  repository = "https://argoproj.github.io/argo-helm"
  chart      = "argo-cd"
  namespace  = kubernetes_namespace.argocd.metadata[0].name
  version    = "10.8.1"

  depends_on = [
    kubernetes_namespace.argocd,
    aws_eks_node_group.default,
  ]

  # The Application is rendered by the Helm release via extraObjects, so terraform plan
  # does not need to read the Application CRD from a cluster that may not exist yet.
  # extraObjects takes a list of nested manifests, which a helm provider v2 set{} block
  # cannot express, hence values + yamlencode.
  values = [
    yamlencode({
      extraObjects = [
        {
          apiVersion = "argoproj.io/v1alpha1"
          kind       = "Application"
          metadata = {
            name      = "multimodal-rag"
            namespace = "argocd"
          }
          spec = {
            project = "default"
            source = {
              repoURL        = "https://github.com/Amith-Ganta/multimodal-rag-guardrails"
              path           = "helm/multimodal-rag"
              targetRevision = "main"
            }
            destination = {
              server    = "https://kubernetes.default.svc"
              namespace = "default"
            }
            syncPolicy = {
              automated = {
                prune    = true
                selfHeal = true
              }
            }
          }
        }
      ]
    })
  ]

  # ApplicationSet has no enabled/install toggle in chart 10.x; scale to zero instead.
  set {
    name  = "applicationSet.replicas"
    value = "0"
  }

  set {
    name  = "dex.enabled"
    value = "false"
  }

  set {
    name  = "notifications.enabled"
    value = "false"
  }

  # Keep ClusterIP: no second internet-facing ELB. Access via port-forward:
  # kubectl -n argocd port-forward svc/argocd-server 8080:443
  set {
    name  = "server.service.type"
    value = "ClusterIP"
  }

  # argo-cd application-controller: core sync controller.
  set {
    name  = "controller.resources.requests.cpu"
    value = "100m"
  }
  set {
    name  = "controller.resources.requests.memory"
    value = "256Mi"
  }
  set {
    name  = "controller.resources.limits.cpu"
    value = "500m"
  }
  set {
    name  = "controller.resources.limits.memory"
    value = "512Mi"
  }

  # argo-cd server: UI and API.
  set {
    name  = "server.resources.requests.cpu"
    value = "50m"
  }
  set {
    name  = "server.resources.requests.memory"
    value = "128Mi"
  }
  set {
    name  = "server.resources.limits.cpu"
    value = "300m"
  }
  set {
    name  = "server.resources.limits.memory"
    value = "256Mi"
  }

  # argo-cd repo-server: renders Helm manifests from git.
  set {
    name  = "repoServer.resources.requests.cpu"
    value = "50m"
  }
  set {
    name  = "repoServer.resources.requests.memory"
    value = "128Mi"
  }
  set {
    name  = "repoServer.resources.limits.cpu"
    value = "300m"
  }
  set {
    name  = "repoServer.resources.limits.memory"
    value = "256Mi"
  }

  # redis: ArgoCD internal state cache.
  set {
    name  = "redis.resources.requests.cpu"
    value = "20m"
  }
  set {
    name  = "redis.resources.requests.memory"
    value = "64Mi"
  }
  set {
    name  = "redis.resources.limits.cpu"
    value = "100m"
  }
  set {
    name  = "redis.resources.limits.memory"
    value = "128Mi"
  }
}

output "argocd_admin_password_command" {
  description = "Command to retrieve the initial ArgoCD admin password after the server is reachable via port-forward."
  value       = "kubectl -n argocd get secret argocd-initial-admin-secret -o jsonpath='{.data.password}' | base64 -d; echo"
}
