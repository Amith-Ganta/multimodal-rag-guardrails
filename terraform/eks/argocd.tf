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

# The Argo CD Application CR must be applied only after the argo-cd Helm
# release has installed the Application CRD. Helm validates every manifest
# in a release against the API server before applying any of them, so the
# CR and CRD cannot live in the same release. The argocd-apps chart only
# renders CRs and is deployed after argo-cd.
resource "helm_release" "argocd_apps" {
  name       = "argocd-apps"
  repository = "https://argoproj.github.io/argo-helm"
  chart      = "argocd-apps"
  version    = "2.0.5"
  namespace  = kubernetes_namespace.argocd.metadata[0].name

  depends_on = [helm_release.argocd]

  values = [
    yamlencode({
      applications = {
        "multimodal-rag" = {
          namespace = "argocd"
          project   = "default"
          # Derived from var.github_repo rather than written out, because a
          # literal URL here is a silent failure in a fork or a second account:
          # ArgoCD comes up healthy and syncs from the original owner's repo,
          # so the replica runs someone else's manifests and nobody notices
          # until a change lands that was never pushed to it.
          source = {
            repoURL        = "https://github.com/${var.github_repo}"
            path           = "helm/multimodal-rag"
            targetRevision = "main"
          }
          destination = {
            server    = "https://kubernetes.default.svc"
            namespace = "default"
          }
          # CI owns the image tag until the GitOps cutover, so ArgoCD must never
          # diff or apply that field. Manual-sync alone did not prevent this: a
          # human pressed SYNC in the ArgoCD UI, which rendered the chart from
          # git (where image.*.repository is empty) and rewrote both live
          # Deployments to a bare ":latest". Ignoring the field makes that
          # button safe to press.
          ignoreDifferences = [
            {
              group        = "apps"
              kind         = "Deployment"
              name         = "multimodal-rag-api"
              jsonPointers = ["/spec/template/spec/containers/0/image"]
            },
            {
              group        = "apps"
              kind         = "Deployment"
              name         = "multimodal-rag-ui"
              jsonPointers = ["/spec/template/spec/containers/0/image"]
            },
            # the same empty-git-values sync also blanked both secret keys,
            # measured as OPENAI_len=0 GROQ_len=0 inside a pod, so ignore the
            # whole data object to also cover any future keys CI may add.
            {
              group        = ""
              kind         = "Secret"
              name         = "multimodal-rag-secrets"
              namespace    = "default"
              jsonPointers = ["/data"]
            }
          ]
          # Manual sync only. selfHeal = false was not enough: an automated
          # policy still runs one initial sync, and git does not yet carry the
          # image tag (CI injects it with --set at deploy time), so that sync
          # rendered the chart's empty image.*.repository as a bare ":latest"
          # and created InvalidImageName pods next to CI's healthy ones.
          # ArgoCD still observes and reports drift, it just never applies.
          # Revisit at the GitOps cutover, when CI writes the tag into git.
          syncPolicy = {
            syncOptions = ["CreateNamespace=false"]
          }
        }
      }
    })
  ]
}

output "argocd_admin_password_command" {
  description = "Command to retrieve the initial ArgoCD admin password after the server is reachable via port-forward."
  value       = "kubectl -n argocd get secret argocd-initial-admin-secret -o jsonpath='{.data.password}' | base64 -d; echo"
}
