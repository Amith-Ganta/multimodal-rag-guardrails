# Patch 4: Terraform for Kafka, Elasticsearch, Fluent Bit, Prometheus and Grafana

You are adding to a live Terraform root module that currently manages a running
EKS cluster serving real traffic. Do not refactor anything that exists. Add new
files only, plus the two small additions listed at the end.

**Depends on patch 0** (`prompt_obs_00_storage.md`), which adds the EBS CSI
driver and a default `gp3` StorageClass. Every PVC in this patch requires it.
The cluster's only StorageClass today is `gp2` on the in-tree
`kubernetes.io/aws-ebs` provisioner, which was removed from Kubernetes before
the 1.31 this cluster runs, so without patch 0 every PVC below sits `Pending`
forever and Kafka, Elasticsearch and Prometheus never start. Where this brief
says "the default StorageClass", it means the `gp3` class from patch 0.

## Verified versions: use these exact strings

Every version below was confirmed against the live chart repository index on
2026-09-07. They are measurements, not recollections. Use them verbatim and do
not substitute "latest" or a version you remember differently.

| Component | Repository | Chart version | App version |
|---|---|---|---|
| Kafka | `https://charts.bitnami.com/bitnami` | `32.4.3` | 4.0.0 |
| Elasticsearch | `https://charts.bitnami.com/bitnami` | `22.1.6` | 9.1.2 |
| Fluent Bit | `https://fluent.github.io/helm-charts` | `0.58.1` | 5.1.1 |
| Prometheus | `https://prometheus-community.github.io/helm-charts` | `29.27.2` | v3.14.0 |
| Grafana | `https://grafana.github.io/helm-charts` | `10.5.15` | 12.3.1 |

`charts.bitnami.com` now redirects to `repo.broadcom.com/bitnami-files/`. The
classic URL still resolves and both Bitnami charts were confirmed to pull, so
write the classic URL.

## Blocker you must handle: Bitnami images return 404

This one is not optional and it fails silently at the wrong layer. Both pinned
Bitnami charts default to images that **no longer exist** in the free `bitnami`
Docker Hub namespace. Measured directly against the registry:

```
bitnami/kafka:4.0.0-debian-12-r10               -> HTTP 404
bitnamilegacy/kafka:4.0.0-debian-12-r10         -> HTTP 200
bitnami/elasticsearch:9.1.2-debian-12-r0        -> HTTP 404
bitnamilegacy/elasticsearch:9.1.2-debian-12-r0  -> HTTP 200
```

Bitnami moved its free catalogue to the `bitnamilegacy` namespace. Deployed
unmodified, `terraform apply` reports success, Helm reports success, and both
StatefulSets sit in `ImagePullBackOff` indefinitely. Nothing in the Terraform
output says anything is wrong.

So for **both** Kafka and Elasticsearch, override the image explicitly in the
chart values:

```hcl
image = {
  registry   = "docker.io"
  repository = "bitnamilegacy/kafka"       # or bitnamilegacy/elasticsearch
  tag        = "4.0.0-debian-12-r10"       # or 9.1.2-debian-12-r0
}
```

Comment why in each file: the chart's default `bitnami/...` image is 404 since
Bitnami's catalogue move, and `bitnamilegacy` is the free replacement. Note
plainly that `bitnamilegacy` carries no update guarantee, and that the
production answer is either a Bitnami Secure Images subscription or mirroring
the image into this project's own ECR.

The Kafka chart may also pull a `kubectl` sidecar image from the same namespace.
If the chart exposes a separate image key for it, point that at
`bitnamilegacy` too rather than leaving one 404 behind.

## Non-negotiable environment facts

Root module is `terraform/eks/`. Existing files include `versions.tf`,
`cluster.tf`, `velero.tf`, `argocd.tf`, `variables.tf`, `outputs.tf`.

Provider versions, from `versions.tf`. These are pinned and you must write
syntax valid for them:

```hcl
required_version = ">= 1.10.0"
aws        ~> 5.0
kubernetes ~> 2.31
helm       ~> 2.14
tls        ~> 4.0
```

**The Helm provider is v2, not v3.** In v2 a `helm_release` is configured by the
provider block, and the provider block uses the nested syntax:

```hcl
provider "helm" {
  kubernetes {
    host                   = ...
    cluster_ca_certificate = ...
    token                  = ...
  }
}
```

That block already exists. Do not add another one, do not convert it to the v3
flat `kubernetes = { ... }` form, and do not emit any v3-only argument.

Backend is S3, bucket `multimodal-rag-tfstate-651103158261`, key
`dev/eks.tfstate`, with `use_lockfile = true`. Do not touch the backend block.

Existing variables you may reference: `aws_region`, `project_name`,
`cluster_name`, `kubernetes_version`, `node_instance_types`,
`node_group_desired_size`, `node_group_min_size`, `node_group_max_size`,
`github_repo`, `eks_public_access_cidrs`.

## The house pattern, from `velero.tf`

Every addon in this module follows the same shape. Read it and match it:

1. `resource "kubernetes_namespace"` with `depends_on = [aws_eks_node_group.default]`
2. `resource "helm_release"` with an explicitly pinned `version`, namespace
   referenced as `kubernetes_namespace.<x>.metadata[0].name`, values supplied as
   a single `yamlencode({ ... })` inside the `values` list, an explicit
   `resources` block, and a `depends_on`.

Velero additionally has an IRSA half: an assume-role policy document using
`sts:AssumeRoleWithWebIdentity` against `aws_iam_openid_connect_provider.eks.arn`
with `StringEquals` on `:aud` and `:sub`, a permissions document, an
`aws_iam_role` and an `aws_iam_role_policy`.

**Skip the IRSA half entirely for every component in this patch.** Kafka,
Elasticsearch, Fluent Bit, Prometheus and Grafana all stay inside the cluster and
call no AWS API. Creating IAM roles they never assume is noise that a reviewer
will read as cargo cult. If you believe one of them genuinely needs AWS
credentials, say so in a comment and explain why rather than adding the role
silently.

## The capacity constraint that governs every value below

This cluster is three `t3.medium` nodes, each roughly 3.3Gi allocatable. Measured
memory **requests** already committed:

```
ip-10-60-20-84    2058Mi (62%)  -> ~1.25Gi free
ip-10-60-25-62    1792Mi (54%)  -> ~1.5Gi free   (also 139% of memory LIMITS)
ip-10-60-8-193    1850Mi (56%)  -> ~1.45Gi free
```

About 4.2Gi free in total, but **never more than ~1.5Gi on any single node.**
Pods schedule onto one node, not onto the total. A 1536Mi pod therefore fits
nowhere and sits `Pending` forever regardless of the aggregate.

So: **no component in this patch may exceed a 1024Mi memory limit**, and the
totals must come in under about 3.9Gi. Every limit below is derived from that,
not chosen for comfort. Do not raise any of them "to be safe". If you think a
value is too low, write a comment saying so and leave the value as specified.

## Component 1: Kafka

File `terraform/eks/kafka.tf`. Namespace `kafka`.

Use the Bitnami chart, repository `https://charts.bitnami.com/bitnami`, chart
`kafka`. Pin an exact `version` and state in a comment that you are pinning it.

Configuration:
- **KRaft mode, no ZooKeeper.** ZooKeeper would add a second StatefulSet this
  cluster cannot afford.
- `controller.replicaCount = 1`. Single broker. This is a demo cluster; a
  three-broker quorum needs memory that does not exist here. Comment that the
  single broker is a deliberate capacity tradeoff and names the durability
  consequence: no replication, so a broker loss loses unconsumed messages.
- Memory limit `768Mi`, request `512Mi`. CPU request `250m`, limit `1`.
- Heap `-Xmx384m -Xms384m` via the chart's JVM options value. The heap must be
  roughly half the limit, or the JVM plus off-heap overhead exceeds the cgroup
  and the pod is OOMKilled.
- `log.retention.hours=6` and a small `log.segment.bytes`. Nothing here needs
  long retention; the queue holds one PDF ingest request at a time.
- `message.max.bytes` sized for a 25MB payload plus overhead. The application
  caps uploads at `MAX_UPLOAD_MB=25`, and the default 1MB broker limit would
  reject every real document. Set the matching `replica.fetch.max.bytes`.
- Persistence: request a small PVC (8Gi) using the cluster's default gp2/gp3
  StorageClass. Do not disable persistence, and do not use `emptyDir`.
- No authentication. This is cluster-internal only and never exposed. Comment
  that plaintext inter-broker listeners are acceptable **only** because the
  service is ClusterIP with no ingress, and name what would need to change to
  expose it (SASL plus TLS).
- Service must be ClusterIP. Never LoadBalancer or NodePort.

The application reaches this at `kafka.kafka.svc.cluster.local:9092`. If the
chart's Service name differs from `kafka`, say so explicitly in a comment,
because patch 3 hardcodes that DNS name in `values.yaml` and the two must agree.

## Component 2: Elasticsearch

File `terraform/eks/elasticsearch.tf`. Namespace `logging`.

Chart: the Bitnami `elasticsearch` chart. Pin an exact version.

- Single node. Set the chart to a single-node topology (master, data and
  coordinating collapsed into one pod), replicas 0.
- Memory limit `1024Mi`, request `768Mi`. CPU request `250m`, limit `1`.
- Heap `-Xms512m -Xmx512m`. Same halving rule as Kafka.
- `discovery.type=single-node` so it does not wait for a quorum that will never
  form.
- Persistence: an 8Gi PVC on the default StorageClass.
- No security/TLS/auth. Cluster-internal, and Grafana reaches it over ClusterIP.
  Comment the same caveat as Kafka: acceptable because there is no ingress.
- Service ClusterIP.

Comment that 1024Mi is below the usual Elasticsearch floor and why it holds here:
this is append-mostly log ingest from a three-node cluster, queried by a single
Grafana panel, with short retention. Note that raising index count, shard count
or retention will break this sizing.

## Component 3: Fluent Bit

File `terraform/eks/fluentbit.tf`. Namespace `logging` (reuse the namespace
resource from `elasticsearch.tf`; do not declare it twice, that is a duplicate
resource error).

Chart `fluent-bit` from `https://fluent.fluent.io/` or the official
`https://fluent.github.io/helm-charts` repository. Use whichever you can name
confidently. Pin an exact version.

- DaemonSet. One pod per node, so ~100Mi limit each, ~300Mi across three nodes.
  Memory request `64Mi`, limit `128Mi`. CPU request `50m`, limit `200m`.
- Input: tail `/var/log/containers/*.log` with the `cri` parser. EKS uses
  containerd, so the `docker` parser will fail to parse every line. Getting this
  wrong produces a running DaemonSet that ships nothing, which looks like
  success.
- Filter: the `kubernetes` filter, so records carry namespace, pod and container
  name. Without it the logs land in Elasticsearch as undifferentiated strings
  and the Grafana panel cannot filter by workload.
- Output: the `es` plugin, host
  `elasticsearch.logging.svc.cluster.local`, port 9200, index prefix
  `k8s-logs`, `Logstash_Format On` with `Logstash_Prefix k8s-logs` so indices
  roll daily. `Suppress_Type_Name On` is required against Elasticsearch 8, which
  removed mapping types; without it every write fails.
- Tolerations so it schedules on every node.
- Exclude Fluent Bit's own logs from the tail path, or a shipping error produces
  a log line that is itself shipped, which loops.

## Component 4: Prometheus

File `terraform/eks/prometheus.tf`. Namespace `monitoring`.

Use the `prometheus` chart from `https://prometheus-community.github.io/helm-charts`,
**not** `kube-prometheus-stack`. Pin an exact version.

State the reason in a comment: `kube-prometheus-stack` brings the Prometheus
Operator, CRDs, Alertmanager, node-exporter and kube-state-metrics, which is
several hundred Mi more than this cluster's remaining headroom. The plain chart
is the capacity-driven choice here, and the tradeoff is that scrape config is
written by hand rather than through `ServiceMonitor` CRDs.

- `server` only. Disable Alertmanager, pushgateway, and kube-state-metrics.
  Keep node-exporter **only** if it fits within the totals; if it does not, turn
  it off and comment that node-level metrics are consequently unavailable.
- Memory limit `512Mi`, request `384Mi`. CPU request `100m`, limit `500m`.
- Retention `3h`, scrape interval `30s`. Comment that this is deliberately short
  because the cluster has ~20 pods and a demo lifetime, and that the tradeoff is
  no historical analysis beyond the last three hours.
- Persistence: an 8Gi PVC, or `emptyDir` given the 3h retention. Pick one and
  comment the choice.
- Scrape config: the standard `kubernetes-pods` job using
  `kubernetes_sd_configs` with role `pod`, keeping only pods annotated
  `prometheus.io/scrape: "true"` and honouring `prometheus.io/port` and
  `prometheus.io/path`. Patch 3 puts exactly those annotations on the API, UI
  and worker pods, so this job is what makes the custom application metrics
  visible. Getting the relabel rules wrong here means Prometheus runs healthily
  and scrapes nothing.
- Service ClusterIP.

## Component 5: Grafana

File `terraform/eks/grafana.tf`. Namespace `monitoring` (reuse the namespace
from `prometheus.tf`).

Chart `grafana` from `https://grafana.github.io/helm-charts`. Pin an exact
version.

- Memory limit `256Mi`, request `128Mi`. CPU request `50m`, limit `200m`.
- Two datasources provisioned via `datasources.datasources.yaml`:
  - Prometheus, `http://prometheus-server.monitoring.svc.cluster.local`, set as
    default.
  - Elasticsearch, `http://elasticsearch.logging.svc.cluster.local:9200`, index
    `k8s-logs-*`, time field `@timestamp`, and the ES version field the current
    chart schema expects. This datasource is why Kibana was dropped: one pane of
    glass for both metrics and logs.
- Admin password: generate it with `random_password` and store it in a
  `kubernetes_secret`, referencing that secret from the chart's
  `admin.existingSecret`. **Do not hardcode a password, do not use `admin`, and
  do not put a literal into the values.** Mark the Terraform output for it
  `sensitive = true`.
- Service ClusterIP. Access is via `kubectl port-forward`. Do not create a
  LoadBalancer: this cluster already exposes two ELBs and an unauthenticated or
  weakly authenticated Grafana on a public address is a real exposure, not a
  theoretical one.
- Persistence disabled is acceptable given the dashboard is provisioned from
  config rather than created in the UI. Comment that this means UI-created
  dashboards do not survive a restart.

### Dashboard

Provision one dashboard as JSON via the chart's `dashboardConfigMaps` or
`dashboards` value. Put the JSON in `terraform/eks/dashboards/multimodal-rag.json`
and load it with `file()`.

Panels, all of which must query metrics that patch 1 and patch 2 actually
create. Do not invent metric names; use exactly these, which are what the
briefs specify:

- Ingest request rate and ingest duration percentiles (p50, p95) from the
  worker's histogram.
- Ingest failures, as a counter rate.
- Query latency percentiles from the API/UI query-path histogram.
- Kafka consumer lag if the chart exposes it; if it does not, omit the panel
  rather than querying a metric that does not exist.
- One logs panel using the Elasticsearch datasource, filtered to the
  `multimodal-rag` workloads.

If you cannot confirm a metric name from the patch 1 and patch 2 briefs, leave
the panel out and note it in a comment. A dashboard with dead panels is worse
than a smaller working one, because it reads as untested.

### Alert rule

One rule, as a Grafana provisioned alerting rule (not an Alertmanager rule,
since Alertmanager is disabled). It must fire on the failure mode that motivated
this entire build: **the ingestion worker pod being OOMKilled or restarting.**

Express it against a metric that exists. `kube_pod_container_status_restarts_total`
comes from kube-state-metrics, which is disabled above, so **do not use it**
unless you re-enable that component and account for its memory in the totals.
The honest alternatives are the worker's own ingest-failure counter, or its
`up` series going to 0. Pick one, and comment plainly which failure modes it
catches and which it misses. Do not write a rule that only looks correct.

## Two small additions to existing files

1. `terraform/eks/outputs.tf`: add outputs for the Grafana port-forward command
   and the Grafana admin password (`sensitive = true`), in the style of the
   existing `velero_backup_status_command` output.
2. Nothing else. Do not modify `cluster.tf`, `versions.tf`, `variables.tf`,
   `velero.tf` or `argocd.tf`.

## Node count: do not try to solve this in Terraform

`cluster.tf` carries `ignore_changes = [scaling_config[0].desired_size]` on
`aws_eks_node_group.default`. Changing `node_group_desired_size` therefore has no
effect on the running cluster. That is intentional, so the cluster autoscaler can
move the count without Terraform reverting it.

So do not add a fourth node by editing that variable, and do not remove the
`ignore_changes` block to make it work. If you want to note that a fourth node
would help, put it in a comment naming
`aws eks update-nodegroup-config --scaling-config desiredSize=4` as the route.
The budget above fits three nodes, so this is headroom, not a prerequisite.

## Hard constraints

- Every `helm_release` must pin an exact `version`. An unpinned chart makes the
  next `terraform apply` a surprise upgrade.
- **Do not invent chart repository URLs or chart versions.** If you are not
  certain a repository URL or a version string is real, write it with a clearly
  marked `# VERIFY:` comment rather than presenting a guess as fact. This has
  been a failure mode before.
- Do not declare the same `kubernetes_namespace` resource in two files.
- No Service may be `type: LoadBalancer`.
- Every component needs an explicit `resources` block. A pod with no memory
  request lands in the BestEffort QoS class and is the first thing evicted under
  pressure, which on this cluster is a matter of when, not if.
- Terraform must remain `terraform validate` clean and `terraform fmt` clean.
- Emit whole new files. For `outputs.tf`, emit only the added block.
