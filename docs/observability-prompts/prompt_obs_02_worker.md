# Patch 2 of 4: Kafka ingestion worker + call-site instrumentation

Depends on patch 1 (`src/config.py` additions, `src/events.py`, `src/metrics.py`).
Assume those exist exactly as you wrote them.

## Goal

Move PDF ingestion off the Streamlit request path. Today `app_streamlit.py` calls
`_build_index_from_pdf` inline, which rasterizes every page image and runs CLIP
inside the UI pod. A 13.5 MB CAD manual OOMKilled that pod (exit 137) and took
every other user's session with it. After this patch the UI publishes a job to
Kafka and a separate worker Deployment does the heavy work.

## Existing code you are modifying

`app_streamlit.py`, the sidebar upload block (current shipped state):

```python
    uploaded = st.file_uploader("Upload a PDF", type=["pdf"])
    if uploaded is not None:
        # Rebuild only when a different file is uploaded, not on every rerun.
        sig = (uploaded.name, uploaded.size)
        if st.session_state.get("_uploaded_sig") != sig:
            # Ingest runs in this pod's own process, so an oversized PDF does not
            # fail one request, it OOMKills the container and takes every other
            # session on the pod with it. Reject above the cap instead.
            max_upload_mb = int(os.getenv("MAX_UPLOAD_MB", "25"))
            if uploaded.size > max_upload_mb * 1024 * 1024:
                st.error(...)
            else:
                with st.spinner(f"Ingesting {uploaded.name} (text + images)..."):
                    try:
                        st.session_state["_uploaded_index"] = _build_index_from_pdf(
                            uploaded.getvalue(), doc_id=Path(uploaded.name).stem
                        )
                        st.session_state["_uploaded_sig"] = sig
                    except Exception as exc:
                        st.session_state.pop("_uploaded_index", None)
                        st.session_state.pop("_uploaded_sig", None)
                        st.error(f"Could not read that PDF: {exc}")
```

and the function it calls:

```python
def _build_index_from_pdf(pdf_bytes: bytes, doc_id: str) -> MultimodalIndex:
    from src.ingest import ingest_pdf
    tmp = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as fh:
            fh.write(pdf_bytes)
            tmp = fh.name
        ingested = ingest_pdf(tmp, doc_id=doc_id)
        index = MultimodalIndex().build([ingested])
        index._uploaded_stats = (len(ingested.text_chunks), len(ingested.images))
        return index
    finally:
        if tmp:
            try:
                Path(tmp).unlink()
            except OSError:
                pass
```

## Hard constraint: the in-process path must survive

`SETTINGS.has_kafka` is False by default. When it is False the UI must behave
EXACTLY as it does today, calling `_build_index_from_pdf` in-process. Do not
delete that path. The Kafka path is an alternative branch, not a replacement.
This matters because the same image runs on EC2 with no broker.

## What to produce

### A. New file `src/ingest_worker.py`

A standalone Kafka consumer, runnable as `python -m src.ingest_worker`.

1. Consume from `SETTINGS.kafka_ingest_topic` with group
   `SETTINGS.kafka_consumer_group`.
2. `enable_auto_commit=False`. Commit only AFTER the job finishes (success or
   terminal failure). Explain in a comment why auto-commit would silently drop
   a job when the worker is OOMKilled mid-ingest, which is the exact failure
   this whole design exists to survive.
3. For each message: base64-decode the PDF, write to a temp file, call
   `src.ingest.ingest_pdf`, build a `MultimodalIndex`, and persist the result so
   the UI can pick it up. Persist as a pickle under a directory from a NEW
   setting `ingest_output_dir` (env `INGEST_OUTPUT_DIR`, default
   `str(SETTINGS.ARTIFACTS_DIR / "uploads")`). Name the file by job id.
   State clearly in a comment that this requires a shared ReadWriteMany volume
   or a single UI replica to be visible to the UI, and that it is the known
   limitation of this design.
4. Emit a `rag.events` event at start, success and failure via
   `events.publish_event`, carrying job id, doc id, page count, image count and
   duration.
5. Record metrics from `src/metrics.py`: ingestion duration by outcome, pages
   and images per document. Call `start_metrics_server()` once at startup so
   Prometheus can scrape the worker itself.
6. A failure on ONE message must never kill the worker. Wrap per-message work in
   try/except, log the traceback, emit a failure event, commit the offset, and
   continue to the next message. A poison-pill PDF must not become an infinite
   crash loop.
7. Handle SIGTERM cleanly: finish the message in flight, commit, close the
   consumer, exit 0. Kubernetes sends SIGTERM on rollout, and an unclean exit
   means a duplicated or lost job.
8. Structured JSON log lines to stdout (one JSON object per line, with `level`,
   `ts`, `event`, `job_id`). Fluent Bit tails stdout into Elasticsearch, so plain
   text would be unqueryable. Do not use `print`.

### B. Modify `app_streamlit.py`

1. In the upload block, when `SETTINGS.has_kafka` is True, call
   `events.publish_ingest_request(...)` instead of ingesting inline. Store the
   returned job id in session state and show a clear status message telling the
   user the document is being processed in the background.
2. Add a small poll: on each rerun, if a job id is pending, check for the
   worker's output file and load it into `_uploaded_index` when it appears.
   Keep it simple and readable.
3. **Honesty requirement:** if the publish returns None (broker unreachable),
   fall back to the in-process path rather than silently doing nothing, and
   surface a warning that background processing was unavailable. Never leave the
   user looking at a spinner for a job that was never queued.
4. Instrument the existing query path with `src/metrics.py`: time the RAG query,
   record the outcome, and record guardrail blocks where the guard fires.
5. Call `start_metrics_server()` once at app startup, guarded so Streamlit's
   rerun model does not try to bind the port repeatedly.

### C. New file `Dockerfile.worker` (or a documented stage in the existing
Dockerfile, your call, state which and why)

The worker image needs the same `src/` and model dependencies as the API, and
its entrypoint is `python -m src.ingest_worker`. Keep it as close to the existing
Dockerfile as possible. Do not add CUDA or GPU torch, the cluster is CPU-only and
the image must stay small.

### D. `requirements.txt` additions

Add `kafka-python` and `prometheus-client` with pinned versions. Note explicitly
that both are optional at runtime by design, but pinned here so the worker image
has them.

## Style rules

- Match the existing codebase: `from __future__ import annotations`, type hints,
  settings not literals, comments explaining WHY not WHAT.
- No em dash characters.
- Return complete files in fenced blocks with exact paths. For
  `app_streamlit.py` return only the changed regions with enough surrounding
  context to apply unambiguously, and say which lines they replace.
