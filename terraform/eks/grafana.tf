# Grafana, the single pane of glass.
#
# Two datasources rather than two web UIs. Prometheus answers "how much and how
# fast", Elasticsearch answers "what exactly did it say when it broke". Keeping both
# behind one login is why Kibana was dropped: it would have cost around 500Mi to show
# the same Elasticsearch data a Grafana datasource already reaches.

# The admin password is generated, never written into this repository. Putting it in
# a variable with a default would mean committing it; putting it in a tfvars file
# means one more file to keep out of git. Generated here, it lands only in the state
# file, which already lives in the encrypted S3 backend.
#
# The tradeoff is honest: anyone with read access to remote state can read this
# password. A cluster carrying real users would source it from AWS Secrets Manager
# with External Secrets, so it never touches Terraform state at all.
resource "random_password" "grafana_admin" {
  length = 20
  # Grafana's own login form handles these fine, but they are excluded because this
  # password gets pasted into shells and kubectl commands, where a quote or a
  # backslash turns a working credential into a confusing error.
  override_special = "-_=+"
}

resource "kubernetes_secret" "grafana_admin" {
  metadata {
    name      = "grafana-admin"
    namespace = kubernetes_namespace.observability.metadata[0].name
  }

  # These key names are not free choices. The chart reads admin.userKey and
  # admin.passwordKey, which default to admin-user and admin-password, and the
  # values block below leaves those defaults in place.
  data = {
    admin-user     = "admin"
    admin-password = random_password.grafana_admin.result
  }

  type = "Opaque"
}

resource "helm_release" "grafana" {
  name       = "grafana"
  repository = "https://grafana.github.io/helm-charts"
  chart      = "grafana"
  version    = "10.5.15"
  namespace  = kubernetes_namespace.observability.metadata[0].name

  values = [yamlencode({
    # Pointing at the secret above rather than setting adminPassword inline. The
    # chart's adminPassword path would put the plaintext into the Helm release record
    # stored in the cluster, readable by anyone who can read secrets in this namespace.
    admin = {
      existingSecret = kubernetes_secret.grafana_admin.metadata[0].name
      userKey        = "admin-user"
      passwordKey    = "admin-password"
    }

    # persistence defaults to disabled, which means an emptyDir: every dashboard
    # edited in the UI disappears the moment the pod restarts. The datasources below
    # are provisioned from this file and would survive, but hand-built panels would
    # not, and losing those silently is the kind of thing nobody notices until they
    # go looking for a dashboard that used to be there.
    #
    # Note the key is storageClassName here, not the storageClass that Bitnami charts
    # use. Getting this wrong is silent: Helm ignores the unknown key and the PVC
    # falls through to the cluster default.
    persistence = {
      enabled          = true
      type             = "pvc"
      size             = "4Gi"
      storageClassName = kubernetes_storage_class.gp3.metadata[0].name
    }

    resources = {
      requests = {
        memory = "192Mi"
        cpu    = "100m"
      }
      limits = {
        memory = "512Mi"
      }
    }

    # ClusterIP, reached through kubectl port-forward. A LoadBalancer here would mean
    # a second public ELB and a Grafana login exposed to the internet, which is a
    # meaningful attack surface for a dashboard only one person uses.
    service = {
      type = "ClusterIP"
      port = 80
    }

    # Datasources provisioned as config rather than clicked in the UI. Anything added
    # by hand exists only in that pod's database and has to be recreated after every
    # reinstall.
    datasources = {
      "datasources.yaml" = {
        apiVersion = 1
        datasources = [
          {
            name = "Prometheus"
            type = "prometheus"
            # The chart names the server service <release>-server, so with a release
            # named "prometheus" this is prometheus-server, not prometheus.
            url       = "http://prometheus-server.observability.svc.cluster.local"
            access    = "proxy"
            isDefault = true
          },
          {
            name = "Elasticsearch"
            type = "elasticsearch"
            # Plain "elasticsearch" is the ClusterIP service. Not
            # elasticsearch-master, which is a ServiceAccount in this chart, and not
            # elasticsearch-master-hl, which is headless and would bypass the service
            # to hit pod IPs directly. Confirmed by rendering the chart.
            url    = "http://elasticsearch.observability.svc.cluster.local:9200"
            access = "proxy"
            # Must match Logstash_Prefix in the Fluent Bit output plus the daily date
            # suffix Logstash_Format produces. A mismatch here shows an empty
            # datasource that tests as healthy, because the connection works and only
            # the index pattern is wrong.
            database  = "kube-*"
            isDefault = false
            jsonData = {
              # The field Fluent Bit writes the record timestamp into. Without it
              # Grafana has no time axis and every query returns nothing.
              timeField = "@timestamp"
              # Elasticsearch 9. Naming the major version wrong makes Grafana send
              # query syntax the server rejects.
              esVersion = "8.0.0"
            }
          },
        ]
      }
    }
  })]

  # Both datasources must exist before Grafana provisions them. Grafana would start
  # anyway and show them as failing, which looks like a broken deployment rather than
  # an ordering problem.
  depends_on = [
    helm_release.prometheus,
    helm_release.elasticsearch,
    kubernetes_storage_class.gp3,
  ]

  timeout = 600
}
