# Observability + streaming stack: paused build plan

Status: **scoped, not built.** Paused on 2026-09-07 at the user's request to
conserve token budget. The ArgoCD / Kubernetes sync work that preceded this is
finished, shipped and green, and is not affected by anything in this file.

Resume by reading this file, then the two DeepSeek briefs listed under
"Prompts already written".

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

## Measured capacity (taken 2026-09-05, re-verify before building)

Three nodes, not two. Each allocatable `1930m` CPU and `3376684Ki` (~3.3Gi)
memory, so roughly 9.9Gi in total.

```
NAME                           CPU    MEM live   CPU requests
ip-10-60-12-149.ec2.internal   40m    1036Mi     375m  (19%)
ip-10-60-20-84.ec2.internal    52m    1891Mi     1245m (64%)
ip-10-60-8-193.ec2.internal    32m    1342Mi     1150m (59%)
```

Roughly **5.6Gi free**.

## Memory budget that fits that headroom

| Component | Limit | Note |
|---|---|---|
| Kafka | 1024Mi | KRaft mode, single broker, no ZooKeeper. 512Mi heap. |
| Elasticsearch | 1536Mi | Single node, no replicas. 1Gi heap, the practical floor. |
| Prometheus | 768Mi | Short 6h retention to keep it there. |
| Grafana | 256Mi | |
| Ingestion worker | 1024Mi | The Kafka consumer that takes over PDF and CLIP work. |
| Fluent Bit | ~100Mi per node | DaemonSet, so roughly 300Mi across three nodes. |

Total about 4.9Gi against 5.6Gi free. Tight but workable. Re-measure before
applying, since the cluster may have moved.

## Prompts already written

Both live in the session scratchpad. Copy them somewhere durable if that
directory is cleared:

```
C:\Users\HP\AppData\Local\Temp\claude\C--Users-HP-Desktop-NLP\7850bfc2-6ed0-4faa-a19b-a018e5152681\scratchpad\prompt_obs_01_config_events.md
C:\Users\HP\AppData\Local\Temp\claude\C--Users-HP-Desktop-NLP\7850bfc2-6ed0-4faa-a19b-a018e5152681\scratchpad\prompt_obs_02_worker.md
```

**Patch 1** covers `src/config.py` additions, a new `src/events.py` (Kafka
publisher) and a new `src/metrics.py` (Prometheus exporter). The governing
requirement is that both new modules are no-ops when the library is missing or
the feature is disabled, so the app runs identically with no broker present.
`KAFKA_ENABLED` defaults to False for exactly that reason.

**Patch 2** covers `src/ingest_worker.py` (the Kafka consumer), the
`app_streamlit.py` changes that publish instead of ingesting inline, a worker
Dockerfile, and requirements pins. The in-process ingestion path must survive
untouched as the fallback, because the same image runs on EC2 with no broker.

## Still to be written

**Patch 3, Kubernetes and Helm.** A worker Deployment, a Service exposing
`/metrics` on port 9100, `prometheus.io/scrape` annotations on the API, UI and
worker pods, and the new environment variables plumbed through
`helm/multimodal-rag/values.yaml` and the ConfigMap. Existing templates:
`api-deployment.yaml`, `api-service.yaml`, `configmap.yaml`, `hpa.yaml`,
`pdb.yaml`, `secret.yaml`, `ui-deployment.yaml`, `ui-service.yaml`.

**Patch 4, Terraform.** Helm releases for Kafka, Elasticsearch, Fluent Bit,
kube-prometheus-stack and Grafana under `terraform/eks/`. Follow the house
pattern in `velero.tf`: an assume-role policy document using
`sts:AssumeRoleWithWebIdentity` against
`aws_iam_openid_connect_provider.eks.arn`, with `StringEquals` conditions on
`:aud` and `:sub`, then a permissions document, then the `helm_release`. Also
the Grafana dashboard JSON and the alert rule.

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
