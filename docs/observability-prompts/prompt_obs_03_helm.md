# Patch 3: Kubernetes and Helm wiring for the ingestion worker and metrics

You are editing an existing, working, production-deployed Helm chart. It is live
on EKS right now serving real traffic. Do not restructure it. Add only what is
listed below, in the style already present in the files.

## Repository facts you must not contradict

Chart lives at `helm/multimodal-rag/`. Existing templates, all of which already
work and are deployed:

```
api-deployment.yaml  api-service.yaml  configmap.yaml  hpa.yaml
pdb.yaml             secret.yaml       ui-deployment.yaml  ui-service.yaml
```

`configmap.yaml` is generic and needs **no change**. It renders every key under
`.Values.config` automatically:

```yaml
data:
{{- range $key, $value := .Values.config }}
  {{ $key }}: {{ $value | quote }}
{{- end }}
```

So a new environment variable is added by adding a key to `config:` in
`values.yaml` and nothing else.

Both existing Deployments pull config and secrets the same way. Copy this shape:

```yaml
          envFrom:
            - configMapRef:
                name: multimodal-rag-config
            - secretRef:
                name: multimodal-rag-secrets
```

Existing Secret is named `multimodal-rag-secrets`. Existing ConfigMap is named
`multimodal-rag-config`. Deployments are named `multimodal-rag-api` and
`multimodal-rag-ui`, each with the label `app: multimodal-rag-<tier>` used by
both the Service selector and the PDB selector.

CI sets image repository and tag at deploy time with
`--set image.<tier>.repository=... --set image.<tier>.tag=...`. Your new worker
image must follow the identical convention: `image.worker.repository` and
`image.worker.tag`, defaulting to `""` and `"latest"` in `values.yaml`.

## Deliverable 1: `helm/multimodal-rag/templates/worker-deployment.yaml`

A Deployment named `multimodal-rag-worker`, label `app: multimodal-rag-worker`.

- `replicas: {{ .Values.worker.replicaCount }}`
- Image from `.Values.image.worker.*`, `imagePullPolicy` from
  `.Values.image.pullPolicy`, exactly as the UI deployment does.
- `envFrom` both the ConfigMap and the Secret, as above.
- `resources` from `{{- toYaml .Values.worker.resources | nindent 12 }}`.
- Container port `{{ .Values.worker.metricsPort }}` named `metrics`.
- Pod annotations for Prometheus scraping:
  ```yaml
      metadata:
        labels:
          app: multimodal-rag-worker
        annotations:
          prometheus.io/scrape: "true"
          prometheus.io/port: "{{ .Values.worker.metricsPort }}"
          prometheus.io/path: "/metrics"
  ```
- **No liveness or readiness HTTP probe on the app port.** The worker serves no
  HTTP application traffic; it is a Kafka consumer. Use no probes at all rather
  than inventing an endpoint that does not exist. Write a one-line comment
  saying exactly that, so the omission reads as deliberate.
- `terminationGracePeriodSeconds: 60`. The worker handles SIGTERM and finishes
  the in-flight PDF before committing its Kafka offset (see patch 2). A short
  grace period would kill it mid-document and force a redelivery.

## Deliverable 2: `helm/multimodal-rag/templates/worker-service.yaml`

A ClusterIP Service named `multimodal-rag-worker-metrics` selecting
`app: multimodal-rag-worker`, exposing port `{{ .Values.worker.metricsPort }}`
to targetPort `metrics`. This exists so Prometheus can discover the endpoint by
Service rather than only by pod annotation. Type is ClusterIP, never
LoadBalancer: this must not be reachable from the internet.

## Deliverable 3: scrape annotations on the existing API and UI deployments

Add the same three `prometheus.io/*` annotations to the pod template metadata of
`api-deployment.yaml` and `ui-deployment.yaml`. Their metrics port comes from
`.Values.api.metricsPort` and `.Values.ui.metricsPort` respectively.

**Do not otherwise touch these two files.** In particular do not alter their
resources, probes, replica counts, or the UI's single-replica arrangement. The
UI running exactly one replica is a deliberate fix for a session-affinity bug
and must survive this patch untouched.

Also add the metrics container port to each, alongside the existing app port:

```yaml
          ports:
            - containerPort: {{ .Values.<tier>.port }}
            - containerPort: {{ .Values.<tier>.metricsPort }}
              name: metrics
```

## Deliverable 4: `helm/multimodal-rag/values.yaml` additions

Add a `worker:` block mirroring the style of the existing `api:` and `ui:`
blocks, including explanatory comments in the same voice (the existing comments
explain *why* a value is what it is, and reference observed incidents; match
that).

```yaml
worker:
  replicaCount: 1
  metricsPort: 9100
  resources:
    requests:
      cpu: 250m
      memory: 512Mi
    limits:
      cpu: "1"
      memory: 1024Mi
```

The 1024Mi limit is load-bearing and must be commented as such: this pod
inherits the PDF rasterization and CLIP embedding work that previously OOMKilled
the UI pod at a 1536Mi limit. It is sized against a measured cluster with about
4.2Gi of schedulable memory fragmented across three nodes, where no single node
has more than roughly 1.5Gi free. Do not raise it above 1024Mi without a node
being added first, and do not lower it, which would recreate the original OOM in
a new location.

Add `metricsPort: 9100` to both the existing `api:` and `ui:` blocks.

Add to `image:`:

```yaml
  worker:
    repository: ""   # set via --set image.worker.repository=<ECR_WORKER_REPO>
    tag: "latest"
```

Add to `config:` (these flow into the ConfigMap automatically):

```yaml
  KAFKA_ENABLED: "true"
  KAFKA_BOOTSTRAP_SERVERS: "kafka.kafka.svc.cluster.local:9092"
  KAFKA_INGEST_TOPIC: "rag.ingest.requested"
  KAFKA_EVENTS_TOPIC: "rag.events"
  KAFKA_CONSUMER_GROUP: "rag-ingest-worker"
  METRICS_ENABLED: "true"
  METRICS_PORT: "9100"
  INGEST_OUTPUT_DIR: "/app/artifacts/uploads"
```

Note `KAFKA_ENABLED` is `"true"` **here** while the code default in
`src/config.py` is `False`. That is intentional and must be commented: the same
image also runs on EC2 with no broker present, where the code default keeps the
in-process path active. Only the Kubernetes deployment opts in.

## Deliverable 5: worker PodDisruptionBudget

Extend `pdb.yaml` with a third PDB for the worker, named
`multimodal-rag-worker`, selecting `app: multimodal-rag-worker`, using
`maxUnavailable: {{ .Values.podDisruptionBudget.workerMaxUnavailable | default 1 }}`
and add `workerMaxUnavailable: 1` to the `podDisruptionBudget:` block in
`values.yaml`.

Use `maxUnavailable`, not `minAvailable`. The worker is a single replica, and a
PDB with `minAvailable: 1` against one replica permits zero evictions and wedges
every node drain forever. The existing file already contains a long comment
explaining this for the UI tier; do not duplicate that comment, reference it in
one short line instead.

**Critically: the `| default 1` is not decoration.** A PDB that renders with
neither `minAvailable` nor `maxUnavailable` is accepted by the API server and
then blocks all voluntary disruptions. An absent value must not be able to
produce that manifest.

## Hard constraints

- Helm silently ignores unrecognised values keys, so a misspelled key is
  dangerous rather than merely inert. Every key you reference in a template must
  exist in `values.yaml` with exactly that spelling and nesting.
- Do not add a `namespace:` field to any template. The chart deploys into the
  release namespace.
- Do not invent an ECR repository URL. Repository values stay `""` in the chart
  and are supplied by CI.
- Do not change `api.autoscaling` or `ui.autoscaling`. The UI's HPA is disabled
  deliberately.
- Emit whole files for new templates, and for edited existing files emit only
  the changed hunks with enough surrounding context to apply unambiguously.

## Known limitation to document, not to solve

The worker writes its built index to `INGEST_OUTPUT_DIR`, which is a container
filesystem path, so the UI pod cannot see it without a shared ReadWriteMany
volume. This patch does not add one. Put a comment in `worker-deployment.yaml`
stating the limitation plainly and naming the two ways out (an EFS-backed RWX
PVC, or moving the built index into object storage). Do not silently pretend the
path is shared.
