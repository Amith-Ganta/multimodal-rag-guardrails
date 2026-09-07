# Observability + streaming stack: build plan

Status: **resumed 2026-09-03, cluster layer in progress.** The ArgoCD /
Kubernetes sync work that preceded this is finished, shipped and green, and is
not affected by anything in this file.

## Scope decision (2026-09-03)

The build was split. **Only the Terraform cluster layer is in scope now:**

| In scope now | Deferred |
|---|---|
| Patch 0, EBS CSI driver + gp3 StorageClass | Patch 1, `src/config.py`, `src/events.py`, `src/metrics.py` |
| Patch 4, Kafka, Elasticsearch, Fluent Bit, Prometheus, Grafana | Patch 2, `src/ingest_worker.py` and `app_streamlit.py` changes |
| | Patch 3, Helm wiring for the worker |

The deliverable for this session is Grafana reachable with real Kubernetes
metrics from Prometheus and real container logs from Fluent Bit through
Elasticsearch. Application-level custom metrics and the Kafka ingestion worker
come later, and the briefs for them are already written and reviewed.

## Storage decision (2026-09-03), and why it was a blocker

The cluster **could not bind a single PVC**. Measured:

```
kubectl version              -> v1.31.14-eks-bca9cf6
kubectl get storageclass     -> gp2   kubernetes.io/aws-ebs
kubectl get pods -n kube-system | grep -iE "ebs|csi"  -> nothing
aws eks list-addons          -> {"addons": []}
kubectl get pvc -A           -> No resources found
```

`kubernetes.io/aws-ebs` is the in-tree provisioner, removed from Kubernetes in
1.27. This cluster runs 1.31, so nothing serves that provisioner name. The gap
was invisible because no workload here has ever asked for storage.

Decision: add the `aws-ebs-csi-driver` EKS managed addon
(`v1.65.0-eksbuild.1`, the verified default for 1.31) with an IRSA role, plus a
`gp3` StorageClass marked default. Roughly $2/month for two 8Gi gp3 volumes.
This is patch 0 and it runs before patch 4.

## Bitnami images return 404 (found 2026-09-03)

Both pinned Bitnami charts default to images that no longer exist. Measured
against Docker Hub:

```
bitnami/kafka:4.0.0-debian-12-r10               -> HTTP 404
bitnamilegacy/kafka:4.0.0-debian-12-r10         -> HTTP 200
bitnami/elasticsearch:9.1.2-debian-12-r0        -> HTTP 404
bitnamilegacy/elasticsearch:9.1.2-debian-12-r0  -> HTTP 200
```

Bitnami moved its free catalogue to `bitnamilegacy`. Left unhandled this is a
pure silent failure: `terraform apply` succeeds, Helm succeeds, and both
StatefulSets sit in `ImagePullBackOff` with nothing in the Terraform output
saying so. Both charts therefore pin `image.registry` and `image.repository`
explicitly. `bitnamilegacy` carries no update guarantee; the production answer
is a Bitnami Secure Images subscription or mirroring into this project's own
ECR.

## Verified chart versions (2026-09-07, against live repo indexes)

| Component | Repository | Chart | App |
|---|---|---|---|
| Kafka | `https://charts.bitnami.com/bitnami` | `32.4.3` | 4.0.0 |
| Elasticsearch | `https://charts.bitnami.com/bitnami` | `22.1.6` | 9.1.2 |
| Fluent Bit | `https://fluent.github.io/helm-charts` | `0.58.1` | 5.1.1 |
| Prometheus | `https://prometheus-community.github.io/helm-charts` | `29.27.2` | v3.14.0 |
| Grafana | `https://grafana.github.io/helm-charts` | `10.5.15` | 12.3.1 |
| EBS CSI driver | EKS managed addon | `v1.65.0-eksbuild.1` | n/a |

Two operational notes. `charts.bitnami.com` now redirects to
`repo.broadcom.com/bitnami-files/`, and the index is 27MB, large enough to stall
a batched `helm repo update` (it killed a 3 minute Bash call with exit 143).
Query it with `curl` rather than `helm search`.

---

## What was agreed

Components to build: **Kafka, Elasticsearch, Fluent Bit, Prometheus, Grafana**.

**Kibana is explicitly excluded.** Grafana carries an Elasticsearch datasource
instead, so metrics and logs sit behind one pane of glass.

Decisions locked through two rounds of questions:

| Question | Decision |
|---|---|
| Capacity | Keep the current nodes. Do not drop Kafka or Elasticsearch. Drop Kibana. |
| Kafka's role | Async ingestion queue. Upload publishes to a topic, a consumer worker does the heavy PDF and CLIP work outside the UI pod. |
| Depth | Deployed and wired to real signals. Real custom metrics, a real dashboard, one real alert rule. Not just installed charts. |
| What feeds Elasticsearch | Fluent Bit DaemonSet tailing container logs. Classic EFK minus Kibana. |
| How Elasticsearch is viewed | Grafana with the Elasticsearch datasource. |

## Why this shape

The Kafka decision is not decorative. The UI pod was OOMKilled (exit 137) by a
13.5 MB image-heavy CAD PDF, which killed every other user's session on that
pod, not only the uploader's. A 25 MB upload cap and a 2560Mi limit went in as
immediate mitigations, but ingestion still runs inside the Streamlit process.
Moving it behind a queue is the actual fix, and the Grafana alert rule is meant
to fire on that same failure mode.

## Measured capacity

### Superseded reading (2026-09-05)

Kept for the record. This is the measurement the original budget was built on,
and it was wrong in two ways: it read CPU requests but memory *live usage*, and
it treated free memory as one pool.

```
NAME                           CPU    MEM live   CPU requests
ip-10-60-12-149.ec2.internal   40m    1036Mi     375m  (19%)
ip-10-60-20-84.ec2.internal    52m    1891Mi     1245m (64%)
ip-10-60-8-193.ec2.internal    32m    1342Mi     1150m (59%)
```

Roughly 5.6Gi free, was the conclusion. It does not hold.

### Current reading (2026-09-07, use this one)

The node set has moved: `ip-10-60-12-149` is gone, `ip-10-60-25-62` is new.
Each node allocatable `1930m` CPU and `3376684Ki` (~3.3Gi) memory, ~9.9Gi total.

Memory **requests** are what the scheduler packs on, not live usage:

```
NODE              MEM REQUESTS   MEM LIMITS    FREE (requests)
ip-10-60-20-84    2058Mi (62%)   1834Mi (55%)  ~1.25Gi
ip-10-60-25-62    1792Mi (54%)   4608Mi (139%) ~1.5Gi
ip-10-60-8-193    1850Mi (56%)   2218Mi (67%)  ~1.45Gi
```

About **4.2Gi free in total, but never more than ~1.5Gi on any single node.**

Two consequences the old budget missed:

1. **Fragmentation binds before the total does.** A 1536Mi Elasticsearch pod
   exceeds the free block on every node and would sit `Pending` forever, even
   though 4.2Gi "is available".
2. **`ip-10-60-25-62` is already at 139% of memory limits.** It carries an API
   replica (1Gi) and the sole UI replica (768Mi request, 2560Mi limit). Placing
   a large pod there puts it in contention with the UI during exactly the
   image-heavy ingest this build exists to move off that pod.

## Revised memory budget (2026-09-07)

Sized so no component exceeds the smallest free block (~1.25Gi).

| Component | Limit | Was | Note |
|---|---|---|---|
| Kafka | 768Mi | 1024Mi | KRaft, single broker, 384Mi heap, `log.retention.hours=6`. Queue depth here is one PDF at a time. |
| Elasticsearch | 1024Mi | 1536Mi | Single node, no replicas, 512Mi heap, short retention. Below the general-purpose floor, which holds only because this is append-mostly log ingest with tiny queries from one Grafana panel. |
| Prometheus | 512Mi | 768Mi | Retention 3h, scrape interval 30s. Three nodes and ~20 pods is very little series volume. |
| Grafana | 256Mi | 256Mi | Unchanged. |
| Ingestion worker | 1024Mi | 1024Mi | **Do not shrink.** This pod inherits the CLIP rasterization that OOMKilled the UI at 1536Mi. Cutting it rebuilds the original bug somewhere new. |
| Fluent Bit | ~100Mi per node | same | DaemonSet, ~300Mi across three nodes. |

Total about **3.9Gi against 4.2Gi free.**

## Node count is the open cost decision

The node group is `2-4 x t3.medium ON_DEMAND` at `desiredSize: 3`, so a fourth
node is available without editing the Terraform maximum.

- **Three nodes.** The revised budget fits, with roughly 200Mi of cluster-wide
  slack left. That is too thin for an API HPA scale-up (which needs 1Gi) and a
  lost node leaves pods `Pending`. Acceptable for a demo driven by hand.
- **Four nodes.** About 7.5Gi free. The budget fits with real headroom, the API
  HPA works again, and Elasticsearch can be placed away from the UI pod. Costs
  roughly $30/month more while the cluster is up.

Four is the recommendation. The sizing above is correct either way, so this
stays a one-line switch at deploy time and does not block writing the patches.

## Prompts already written

Copied out of the session scratchpad into the repo, so they survive a cleared
temp directory:

```
docs/observability-prompts/prompt_obs_01_config_events.md
docs/observability-prompts/prompt_obs_02_worker.md
docs/observability-prompts/prompt_obs_03_helm.md
docs/observability-prompts/prompt_obs_04_terraform.md
```

**All four briefs are now written.** The next action is to hand them to DeepSeek
one at a time, in order, reviewing each patch before applying it.

**Patch 1** covers `src/config.py` additions, a new `src/events.py` (Kafka
publisher) and a new `src/metrics.py` (Prometheus exporter). The governing
requirement is that both new modules are no-ops when the library is missing or
the feature is disabled, so the app runs identically with no broker present.
`KAFKA_ENABLED` defaults to False for exactly that reason.

**Patch 2** covers `src/ingest_worker.py` (the Kafka consumer), the
`app_streamlit.py` changes that publish instead of ingesting inline, a worker
Dockerfile, and requirements pins. The in-process ingestion path must survive
untouched as the fallback, because the same image runs on EC2 with no broker.

## Patches 3 and 4, now written

**Patch 3, Kubernetes and Helm.** A worker Deployment, a ClusterIP Service
exposing `/metrics` on port 9100, `prometheus.io/scrape` annotations on the API,
UI and worker pods, the new environment variables through `values.yaml`, and a
worker PDB. Two facts found while grounding the brief and worth keeping here:

- `configmap.yaml` is a generic `range` over `.Values.config`, so a new
  environment variable needs a `values.yaml` key and no template edit at all.
- Neither `api:` nor `ui:` currently carries a `metricsPort`, so that is a real
  addition rather than a reference to something already present.

Two deliberate calls recorded in the brief: the worker gets **no probes**,
because it is a Kafka consumer serving no HTTP application traffic and inventing
a health endpoint would be worse than omitting one; and the unshared index path
is **documented, not solved**, since the worker writes to a container filesystem
path the UI pod cannot read without an EFS-backed RWX volume.

**Patch 4, Terraform.** A namespace plus a pinned `helm_release` per component
under `terraform/eks/`, following only the second half of the `velero.tf`
pattern.

**Correction to the earlier note in this file: no IRSA is needed.** Kafka,
Elasticsearch, Fluent Bit, Prometheus and Grafana all stay inside the cluster and
call no AWS API, so the assume-role document, permissions document,
`aws_iam_role` and `aws_iam_role_policy` are skipped entirely. IAM roles nothing
ever assumes read as cargo cult to a reviewer.

**Also corrected: `kube-prometheus-stack` is out, the plain `prometheus` chart is
in.** The stack pulls in the Prometheus Operator, its CRDs, Alertmanager,
node-exporter and kube-state-metrics, several hundred Mi past this cluster's
headroom. The tradeoff accepted in exchange is hand-written scrape config instead
of `ServiceMonitor` CRDs.

One consequence worth flagging before the alert rule is written: with
kube-state-metrics disabled, `kube_pod_container_status_restarts_total` does not
exist, so the OOMKill alert has to key off the worker's own failure counter or
its `up` series instead.

## Then, and only then

Rewrite `README.md`. AI part first, DevOps part second, with **Mermaid
diagrams**. The current README is 732 lines and already follows that ordering
(`# Part 1: the AI system` at line 57, `# Part 2: the platform` at line 384), so
this is an extension rather than a restructure.

## Rules that still apply when this resumes

- **DeepSeek v4 Pro authors every patch.** Claude scopes, reviews and verifies.
  Documentation prose is the exception, Claude writes that directly.
- Review DeepSeek output before applying. It has previously hallucinated a URL,
  dropped a `namespace` argument and omitted an import.
- Do not push commits merely to trigger CI, and do not re-run whole workflows.
  Re-run only the specific failing job.
- No em dash characters in any `.tex` file.
- Never print API key or secret values into chat or tool output.

## Unrelated open item

Two API keys leaked into tool output in an earlier session and still need
rotating: the OpenAI key ending `1RUA` and the Groq key ending `Lgxtz`. After
rotating, update the `OPENAI_API_KEY` and `GROQ_API_KEY` repository secrets.
This is not blocking, the app works on the current keys.
