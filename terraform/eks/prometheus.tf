# Prometheus, the metrics store.
#
# This is the plain prometheus chart, not kube-prometheus-stack. The stack version
# bundles Grafana, an operator, and a full set of CRDs, and would have cost roughly
# three times the memory on a cluster that does not have it. The tradeoff is that
# scrape targets are configured here as plain YAML rather than as ServiceMonitor
# objects, which is more verbose but has no operator to keep alive.

resource "helm_release" "prometheus" {
  name       = "prometheus"
  repository = "https://prometheus-community.github.io/helm-charts"
  chart      = "prometheus"
  version    = "29.27.2"
  namespace  = kubernetes_namespace.observability.metadata[0].name

  values = [yamlencode({
    # This chart pulls in four subcharts and every one of them defaults to enabled.
    # Left alone they would add an Alertmanager, a pushgateway, a node-exporter
    # DaemonSet and kube-state-metrics without being asked. Each is switched on or off
    # on its own merits rather than accepting the bundle.
    alertmanager = {
      # Off. Alertmanager routes alerts to email, Slack or PagerDuty, and none of those
      # are wired up here. Running it would mean a pod whose entire job is to deliver
      # notifications to nowhere.
      enabled = false
    }

    "prometheus-pushgateway" = {
      # Off. The pushgateway exists for batch jobs that exit before they can be scraped.
      # Every workload here is a long-running pod that Prometheus scrapes directly.
      enabled = false
    }

    "kube-state-metrics" = {
      # On, and this one earns its memory. It is the only source of pod-level state:
      # kube_pod_container_status_restarts_total is what turns "the app feels flaky"
      # into "that container has restarted 14 times", and nothing else exports it.
      enabled = true
      resources = {
        requests = {
          memory = "64Mi"
          cpu    = "25m"
        }
        limits = {
          memory = "192Mi"
        }
      }
    }

    "prometheus-node-exporter" = {
      # On. A DaemonSet, so it costs once per node, and it is the only source of node
      # memory and disk pressure. Those are the numbers that explain an eviction, and
      # this cluster evicts pods for memory often enough to need them.
      enabled = true
      resources = {
        requests = {
          memory = "32Mi"
          cpu    = "25m"
        }
        limits = {
          memory = "96Mi"
        }
      }
    }

    server = {
      # 15 days at this cluster's scrape volume fits comfortably in 8Gi and covers the
      # question actually asked of metrics here: what changed since last week.
      retention = "15d"

      persistentVolume = {
        enabled      = true
        size         = "8Gi"
        storageClass = kubernetes_storage_class.gp3.metadata[0].name
      }

      # The chart ships resources empty. Prometheus holds its active series in memory,
      # so an unbounded server on a shared node is the classic cause of an OOMKill that
      # takes the application pod down with it.
      resources = {
        requests = {
          memory = "512Mi"
          cpu    = "100m"
        }
        limits = {
          memory = "1Gi"
        }
      }

      # ClusterIP. Prometheus has no authentication of its own, and its query API can
      # read every metric in the cluster. Grafana reaches it from inside; a human
      # reaches it through kubectl port-forward.
      service = {
        type = "ClusterIP"
      }
    }
  })]

  depends_on = [kubernetes_storage_class.gp3]

  timeout = 900
}
