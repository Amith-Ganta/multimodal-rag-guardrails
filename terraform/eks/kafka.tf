# Kafka, the ingestion queue.
#
# The reason this exists: PDF ingestion used to run inline in the Streamlit pod.
# A 13.5 MB CAD manual rasterized every page and ran CLIP in the UI process, the
# container hit its memory limit, and Kubernetes killed it with exit 137. That did
# not fail one request, it took down every other user's session on that pod.
# Publishing a job here instead moves the heavy work to a worker that can die
# without anyone noticing.
#
# The consumer side of this is patch 2 and is deliberately not deployed yet. Kafka
# standing up with no consumer is the correct intermediate state: the topic exists,
# the broker is healthy, and nothing is published to it.

resource "kubernetes_namespace" "observability" {
  metadata {
    name = "observability"
  }
}

resource "helm_release" "kafka" {
  name = "kafka"
  # OCI rather than the https://charts.bitnami.com/bitnami index. The classic index
  # still serves this chart, but the Helm provider fails to resolve it and reports
  # "invalid_reference: invalid tag", which is a misleading error because both the
  # chart and the version pin are correct. Pulling from the OCI registry avoids that
  # resolution path and is Bitnami's current distribution route in any case.
  chart     = "oci://registry-1.docker.io/bitnamicharts/kafka"
  version   = "32.4.3"
  namespace = kubernetes_namespace.observability.metadata[0].name

  # Bitnami moved its free images to the "bitnamilegacy" org in 2025. The tag this
  # chart version defaults to, bitnami/kafka:4.0.0-debian-12-r10, now returns 404
  # from Docker Hub. That is the worst kind of failure: terraform apply succeeds,
  # helm reports the release deployed, and the pods sit in ImagePullBackOff. The
  # same tags under bitnamilegacy return 200, verified against the registry before
  # writing this.
  #
  # bitnamilegacy carries no update guarantee and receives no security patches. The
  # real answer for a production cluster is a Bitnami Secure Images subscription or
  # mirroring these images into the ECR registry this project already owns. Pinning
  # legacy here is a deliberate, time-boxed choice for a dev cluster.
  #
  # Only the two images this chart actually pulls are overridden. The chart also
  # defines os-shell and kubectl init containers, but both ship disabled, so
  # redirecting them would be noise that implies a dependency that does not exist.
  values = [yamlencode({
    image = {
      registry   = "docker.io"
      repository = "bitnamilegacy/kafka"
      tag        = "4.0.0-debian-12-r10"
    }

    # KRaft, not ZooKeeper. Kafka 4.x removed ZooKeeper entirely, and a single
    # combined controller-plus-broker node saves the roughly 700Mi a separate broker
    # StatefulSet would cost on a cluster with about 1.25Gi free on its tightest node.
    kraft = {
      enabled = true
    }

    controller = {
      replicaCount = 1

      # One replica means no replication, so a node failure loses queued jobs. That is
      # acceptable here because a lost ingest job is a re-upload, not lost data: the
      # source PDF still exists on the user's machine. Three replicas would need
      # roughly 2.1Gi and this cluster does not have it.
      resources = {
        requests = {
          memory = "512Mi"
          cpu    = "100m"
        }
        limits = {
          memory = "1Gi"
        }
      }

      persistence = {
        enabled      = true
        size         = "8Gi"
        storageClass = kubernetes_storage_class.gp3.metadata[0].name
      }
    }

    # The chart defaults every listener to SASL_PLAINTEXT. Dropping the client listener
    # to PLAINTEXT is deliberate: the broker is a ClusterIP service reachable only from
    # inside the cluster, and SASL here would mean managing credentials for a queue
    # nothing outside the cluster can reach. A deployment carrying real user documents
    # would keep SASL and add TLS.
    listeners = {
      client = {
        protocol = "PLAINTEXT"
      }
    }

    # The broker exports JMX metrics; the exporter turns them into something Prometheus
    # can scrape. Without this the queue is invisible, and consumer lag, the one number
    # that says whether ingestion is keeping up, cannot be measured at all.
    metrics = {
      jmx = {
        enabled = true
        image = {
          registry   = "docker.io"
          repository = "bitnamilegacy/jmx-exporter"
          tag        = "1.4.0-debian-12-r0"
        }
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
    }
  })]

  # Storage must exist before the StatefulSet asks for a volume. Without this the
  # first apply on a fresh cluster races and the PVC lands before the class does.
  depends_on = [kubernetes_storage_class.gp3]

  # Kafka forms a quorum on startup and the chart's readiness probe is genuinely slow
  # on a t3.medium. The default 5 minutes is not enough and produces a spurious
  # failed apply on a release that would have come up fine.
  timeout = 900
}
