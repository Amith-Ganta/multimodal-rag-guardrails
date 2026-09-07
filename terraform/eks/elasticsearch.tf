# Elasticsearch, the log store.
#
# This is where Fluent Bit ships container logs so they can be searched across pods
# and across restarts. Without it, debugging a crashed pod means kubectl logs against
# a container that no longer exists, which returns nothing at all.
#
# Kibana is deliberately not deployed. The user dropped it explicitly, and Grafana
# already reads Elasticsearch through a datasource, so a second web UI would cost
# roughly 500Mi to show the same data twice.

resource "helm_release" "elasticsearch" {
  name = "elasticsearch"
  # OCI rather than the https://charts.bitnami.com/bitnami index, for the same reason
  # as Kafka: the Helm provider fails to resolve that index and reports
  # "invalid_reference: invalid tag" even though the chart and the version pin are both
  # correct. Verified by pulling this exact chart and version from the OCI registry by
  # hand before changing it here.
  chart     = "oci://registry-1.docker.io/bitnamicharts/elasticsearch"
  version   = "22.1.6"
  namespace = kubernetes_namespace.observability.metadata[0].name

  # Same bitnamilegacy situation as Kafka: the chart's default bitnami/* tags now 404.
  # Three images matter here, and the third one is the trap. sysctlImage ships
  # enabled: true, so it runs on every Elasticsearch pod as an init container. Miss it
  # and the StatefulSet never reaches Running, because the init container cannot pull.
  # volumePermissions ships disabled and is therefore left alone.
  #
  # The os-shell tag is pinned to r50, the chart's own default, not the newer r51.
  # Both exist under bitnamilegacy; matching the chart default keeps this override a
  # pure registry redirect rather than a silent version bump.
  values = [yamlencode({
    image = {
      registry   = "docker.io"
      repository = "bitnamilegacy/elasticsearch"
      tag        = "9.1.2-debian-12-r0"
    }

    sysctlImage = {
      registry   = "docker.io"
      repository = "bitnamilegacy/os-shell"
      tag        = "12-debian-12-r50"
    }

    # The chart's default topology is four separate node roles at two replicas each:
    # eight pods, with the data nodes alone asking for a 1024m heap apiece. That is
    # several times this cluster's free memory. Collapsing to a single node that holds
    # every role is the only shape that fits.
    #
    # masterOnly = false is what makes this legal. Left at its default of true, the
    # master pods refuse to store data and Elasticsearch would have nowhere to put
    # anything, so the dedicated data nodes below could not be scaled to zero.
    master = {
      masterOnly   = false
      replicaCount = 1

      # resourcesPreset is ignored once resources is set, which is what the chart's own
      # documentation recommends. 1Gi is enough for the log volume this cluster produces.
      resources = {
        requests = {
          memory = "768Mi"
          cpu    = "150m"
        }
        limits = {
          memory = "1536Mi"
        }
      }

      # Heap is set well below the limit on purpose. Elasticsearch needs substantial
      # off-heap memory for Lucene segments and the JVM itself, so a heap sized at the
      # container limit produces an OOMKill rather than a garbage collection.
      heapSize = "768m"

      persistence = {
        enabled      = true
        size         = "8Gi"
        storageClass = kubernetes_storage_class.gp3.metadata[0].name
      }
    }

    # Every dedicated role scaled to zero. With masterOnly false above, the single
    # master pod covers data, coordinating and ingest duties by itself.
    data = {
      replicaCount = 0
    }

    coordinating = {
      replicaCount = 0
    }

    ingest = {
      replicaCount = 0
    }

    # security.enabled already defaults to false, so this is stating the decision rather
    # than changing behaviour. Elasticsearch here is a ClusterIP service holding this
    # cluster's own container logs, reachable only from Fluent Bit and Grafana inside
    # the cluster. Anything holding customer data would enable X-Pack and TLS, which
    # this chart also requires before password auth will work at all.
    security = {
      enabled = false
    }

    # Off, and this is a capacity decision rather than a judgement that the metrics are
    # worthless. A t3.medium caps at 17 pods per node, which is an ENI address limit and
    # has nothing to do with how much memory is free. One node hit that ceiling and the
    # Fluent Bit DaemonSet could not schedule on it. A DaemonSet cannot relocate, so
    # that node would ship no container logs at all, while this exporter is a Deployment
    # that happened to take the last slot.
    #
    # Between shard and JVM statistics for a single-node Elasticsearch and log shipping
    # from an entire node, the logs win: a missing exporter leaves a gap in a dashboard,
    # a missing shipper leaves a node whose crashes cannot be investigated afterwards.
    # Re-enable this once the node group has room, either on larger instances or with
    # more nodes.
    metrics = {
      enabled = false
    }
  })]

  depends_on = [kubernetes_storage_class.gp3]

  # Elasticsearch bootstraps a cluster, waits on its own health endpoint, and does it
  # on a burstable instance. The default 5 minutes fails an install that would have
  # succeeded at 8.
  timeout = 900
}
