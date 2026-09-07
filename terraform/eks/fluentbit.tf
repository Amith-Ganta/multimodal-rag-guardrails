# Fluent Bit, the log shipper.
#
# A DaemonSet, so one pod per node reads that node's container log files off the host
# filesystem and forwards them to Elasticsearch. This is the piece that makes logs
# outlive the container that wrote them: once a pod is gone, kubectl logs returns
# nothing, and the crash that killed it is unrecoverable.
#
# This chart comes from the Fluent project, not Bitnami, so its image lives on
# cr.fluentbit.io and the bitnamilegacy problem does not apply here.

resource "helm_release" "fluent_bit" {
  name       = "fluent-bit"
  repository = "https://fluent.github.io/helm-charts"
  chart      = "fluent-bit"
  version    = "0.58.1"
  namespace  = kubernetes_namespace.observability.metadata[0].name

  values = [yamlencode({
    # The chart leaves resources empty, which means no requests at all. An unbounded
    # DaemonSet on a cluster this tight is how a log spike starves the application
    # pods on the same node.
    resources = {
      requests = {
        memory = "64Mi"
        cpu    = "50m"
      }
      limits = {
        memory = "192Mi"
      }
    }

    config = {
      # The default inputs block ships two inputs, and both need correcting.
      #
      # First, the systemd input is dropped entirely. It reads the kubelet journal,
      # which on EKS is high-volume node chatter unrelated to this application, and it
      # would roughly double the log traffic into a single-node Elasticsearch.
      #
      # Second, and this is the silent one: the tail input must exclude Fluent Bit's
      # own log file. Without Exclude_Path, a single shipping error gets written to
      # Fluent Bit's log, which Fluent Bit then reads and tries to ship, which fails
      # and logs again. The loop is self-sustaining and saturates the output.
      #
      # multiline.parser is kept at "docker, cri" from the chart default. EKS runs
      # containerd, so cri is the parser that actually matches; docker is harmless
      # ahead of it and the chart tries them in order.
      inputs = <<-EOT
        [INPUT]
            Name tail
            Path /var/log/containers/*.log
            Exclude_Path /var/log/containers/fluent-bit*
            multiline.parser docker, cri
            Tag kube.*
            Mem_Buf_Limit 5MB
            Skip_Long_Lines On
      EOT

      # The kubernetes filter is what turns a log line into something searchable. Without
      # it every record is raw text with a filename attached, and there is no way to ask
      # which pod, namespace or container produced it.
      filters = <<-EOT
        [FILTER]
            Name kubernetes
            Match kube.*
            Merge_Log On
            Keep_Log Off
            K8S-Logging.Parser On
            K8S-Logging.Exclude On
      EOT

      # Two corrections against the chart default here, both of which fail quietly.
      #
      # Host: the chart defaults to "elasticsearch-master", which in this release is the
      # HEADLESS service, confirmed by rendering the chart rather than assuming. The
      # client-facing ClusterIP service is plain "elasticsearch". Shipping to the
      # headless name resolves to pod IPs directly and bypasses the service entirely.
      #
      # Suppress_Type_Name: Elasticsearch 8 removed mapping types. Fluent Bit still
      # sends a _type field unless this is On, and Elasticsearch 9 rejects every single
      # record with a 400. Fluent Bit logs the rejection and carries on, so the pods
      # look healthy, the DaemonSet is Running, and no log ever arrives.
      #
      # Retry_Limit False means retry forever rather than dropping. On a single-node
      # Elasticsearch that restarts occasionally, dropping would lose exactly the logs
      # from the incident worth investigating.
      #
      # Replace_Dots is the one that actually bit this cluster, and it is worth spelling
      # out because the symptom points nowhere near the cause. Kubernetes pods carry
      # labels like app.kubernetes.io/name, and Elasticsearch reads a dot as nesting.
      # So that label arrives as an object at kubernetes.labels.app with a child field,
      # while any pod carrying a plain "app" label sends a string at that same path.
      # One index cannot hold both shapes. Whichever arrives first wins the mapping and
      # every record from the other group is rejected for the rest of that day's index.
      #
      # What made this expensive to find: the rejection happens during document parsing,
      # so Elasticsearch's index_failed counter stays at 0, its own log records the real
      # reason only at INFO level, and Fluent Bit reports errors 0 with dropped_records 0
      # while retrying the same chunks forever. Every surface says healthy. On turns the
      # dots into underscores, so the two labels stop colliding.
      #
      # Trace_Error surfaces the response body Elasticsearch sends back on a failed bulk
      # write. Without it the output plugin reports a failed flush and nothing about why,
      # which is what turned this into a guessing game rather than a lookup.
      outputs = <<-EOT
        [OUTPUT]
            Name es
            Match kube.*
            Host elasticsearch
            Port 9200
            Logstash_Format On
            Logstash_Prefix kube
            Suppress_Type_Name On
            Replace_Dots On
            Trace_Error On
            Retry_Limit False
      EOT
    }
  })]

  # Elasticsearch must exist before logs are aimed at it. Fluent Bit would survive the
  # wait by retrying, but a failed first install is a confusing thing to debug.
  depends_on = [helm_release.elasticsearch]
}
