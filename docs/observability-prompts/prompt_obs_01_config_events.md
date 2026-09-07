# Patch 1 of 4: settings + event publishing module

You are extending an existing production multimodal RAG codebase. Match its
conventions exactly. Do not restructure anything that already exists.

## Repo facts you must respect

- `src/config.py` holds ALL tunables in a frozen dataclass `Settings`, read from
  environment variables via `os.getenv` with fallbacks, exposed as `SETTINGS`.
  Every non-obvious setting carries a comment explaining WHY it exists and what
  failure it prevents. Read the excerpt below and match that voice precisely.
- Code reads settings, never literals.
- Existing property style: `has_openai_key` / `has_groq_key` returning bool.
- Python 3.11+, `from __future__ import annotations` at the top of modules.
- No em dash characters anywhere. Use a comma, colon, parentheses or a full stop.

Existing `src/config.py` excerpt (style reference, do not rewrite these):

```python
    # --- Image ingest bounds -----------------------------------------------
    # Real PDFs contain spacers, rules and bullets that carry no meaning, and
    # logos repeated on every page. Without these bounds a single large manual
    # decodes to gigabytes of base64 and exceeds the pod memory limit.
    image_min_width: int = int(os.getenv("IMAGE_MIN_WIDTH", "32"))
    image_max_per_doc: int = int(os.getenv("IMAGE_MAX_PER_DOC", "1500"))

    @property
    def has_openai_key(self) -> bool:
        return bool(os.getenv("OPENAI_API_KEY"))
```

## Context: the real incident this work fixes

A 13.5 MB image-heavy CAD PDF uploaded to the Streamlit UI rasterized hundreds
of page images for CLIP inside the UI pod's own process. The pod was OOMKilled
(exit 137), which killed every other user's session on that pod, not just the
uploader's. A 25 MB upload cap and a 2560Mi limit were applied as immediate
mitigations, but ingestion still runs in-process. This work moves ingestion off
the request path onto a Kafka-consumed worker so a heavy ingest can never again
take down the UI.

## What to produce in THIS patch

### A. Additions to `src/config.py` (give me ONLY the new block plus any new
properties, and state exactly where they go)

Add a section for the event/telemetry pipeline:

- `kafka_enabled` (bool, env `KAFKA_ENABLED`, default **False**). Critical: the
  default must be off so existing tests, local runs and the EC2 deployment
  behave exactly as they do today with no broker present.
- `kafka_bootstrap_servers` (str, env `KAFKA_BOOTSTRAP_SERVERS`, default `""`).
- `kafka_ingest_topic` (str, env `KAFKA_INGEST_TOPIC`, default
  `"rag.ingest.requested"`).
- `kafka_events_topic` (str, env `KAFKA_EVENTS_TOPIC`, default `"rag.events"`).
- `kafka_consumer_group` (str, env `KAFKA_CONSUMER_GROUP`, default
  `"rag-ingest-worker"`).
- `kafka_publish_timeout_sec` (float, env `KAFKA_PUBLISH_TIMEOUT_SEC`, default
  `"5"`). Comment must explain that a blocked broker must never stall a user
  request, so publishing is bounded and failure is non-fatal.
- `metrics_enabled` (bool, env `METRICS_ENABLED`, default **True**).
- `metrics_port` (int, env `METRICS_PORT`, default `"9100"`).

Add a property `has_kafka` returning True only when `kafka_enabled` is true AND
`kafka_bootstrap_servers` is non-empty. Comment why both are required.

### B. New file `src/events.py`

A small, dependency-tolerant event publisher. Requirements, all mandatory:

1. Module docstring in the house voice explaining that telemetry must never be
   able to break the thing it observes.
2. `kafka-python` must be an OPTIONAL import. Wrap the import in try/except
   ImportError and set a module-level flag. If the library is missing, or
   `SETTINGS.has_kafka` is False, every public function must become a silent
   no-op that returns False. The app must run identically with no Kafka
   installed and no broker reachable. This is the single most important
   property of this module.
3. A lazily-created singleton producer (create on first publish, not at import),
   because importing this module must never open a socket. Guard creation with a
   lock so concurrent Streamlit threads cannot create two producers.
4. `publish_event(event_type: str, payload: dict) -> bool` which sends a JSON
   value to `SETTINGS.kafka_events_topic`. It must:
   - stamp `event_type`, an ISO-8601 UTC `ts`, and a `service` field into the
     envelope;
   - never raise. Catch `Exception`, log at warning level, return False.
5. `publish_ingest_request(doc_id: str, pdf_bytes: bytes, filename: str) -> str | None`
   which publishes to `SETTINGS.kafka_ingest_topic` and returns a generated
   job id (uuid4 hex) on success, None on failure. The PDF bytes must be
   base64-encoded into the message. Add a comment noting the practical size
   ceiling: Kafka's default `message.max.bytes` is about 1 MB, so this path
   carries a `KAFKA_MAX_MESSAGE_MB` bound (add that setting too, default "20")
   and the broker must be configured to match. Reject oversized payloads by
   returning None rather than letting the producer raise.
6. Use `logging.getLogger(__name__)`, never `print`.
7. Type hints throughout. `from __future__ import annotations`.

### C. New file `src/metrics.py`

Prometheus metrics, equally tolerant:

1. `prometheus_client` as an OPTIONAL import, same pattern: if missing or
   `SETTINGS.metrics_enabled` is False, every helper is a no-op.
2. Define these metrics, and choose the correct metric TYPE for each. Justify
   each type choice in a brief comment:
   - RAG query latency, labelled by outcome
   - PDF ingestion duration, labelled by outcome (success/failure)
   - ingested pages and images per document
   - guardrail blocks, labelled by which rail fired (input/output)
   - LLM gateway calls, labelled by model and outcome, so fallback usage is
     visible
   - answer verification (DeepEval runtime guard) pass/fail
   Use histograms where you need distribution and quantiles, counters for
   monotonic totals. Pick explicit histogram buckets suited to RAG latency
   (single-digit seconds up to a minute), do NOT accept the library default
   buckets, and comment why.
3. `start_metrics_server()` that starts the exporter on `SETTINGS.metrics_port`,
   is idempotent (safe to call twice, second call is a no-op), and never raises.
4. A context manager or decorator `timed(histogram, **labels)` so call sites
   read cleanly.

## Output format

Return each file complete and runnable, in its own fenced code block, preceded
by the exact path. For `src/config.py` return only the block to insert and say
precisely where. No prose beyond a short note on any judgement call you made.
