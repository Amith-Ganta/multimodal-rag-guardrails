# Multimodal RAG: dual-encoder, guardrails, A2A, gateway, eval gate

A retrieval-augmented question-answering service over PDFs that carry both text
and figures. It closes a set of gaps that a CLIP-only notebook leaves open:

- **Dual-encoder retrieval.** Text is embedded by a dedicated text encoder
  (local MiniLM by default, or OpenAI `text-embedding-3-small`); images are
  embedded by CLIP. They live in two separate FAISS indexes. A query hits both.
  This deliberately avoids pushing long document text through CLIP's 77-token
  text tower, which silently truncates and loses meaning. CLIP's text tower is
  used only on short query strings, for cross-modal image retrieval.
- **NeMo Guardrails.** Every answer runs behind input and output rails
  (jailbreak / injection / secret-leak checks) before it reaches the caller.
- **LiteLLM gateway.** One `complete()` call fronts the answering model, with
  transparent fallbacks, a local cache, and a per-call audit log (model, tokens,
  cost).
- **A2A loop.** A retriever agent drafts an answer and a verifier agent returns
  a JSON accept/revise verdict, exchanged over a bounded message loop.
- **DeepEval offline gate.** A pytest suite scores answers over a golden set
  with answer-relevancy, GEval correctness, and faithfulness. Judge model is
  `gpt-4o-mini`; it skips cleanly when no key is present.
- **Two surfaces.** A FastAPI service and a Streamlit UI.

DevOps (ArgoCD, Ansible, Kubernetes) is intentionally out of scope here.

## The sample data is synthetic

`scripts/make_sample_pdf.py` generates `data/sample_report.pdf` and its numbers
(for example an accuracy of 0.86 and a three-bar chart) are illustrative, not
measurements from a real system. The golden answers in
`goldens/multimodal_goldens.json` match that synthetic document. Swap in your
own PDFs to evaluate real content.

## Layout

```
src/
  config.py     central Settings singleton (paths, models, thresholds)
  encoders.py   TextEncoder (minilm/openai) + ImageEncoder (CLIP)
  ingest.py     PDF -> text chunks + extracted images (base64)
  index.py      two FAISS indexes, cross-modal search, save/load
  gateway.py    LiteLLM gateway with fallbacks, cache, audit log
  answer.py     retrieve -> build vision messages -> grounded answer
  guard.py      NeMo input/output rails around the pipeline
  a2a.py        retriever + verifier agents over a message loop
  api.py        FastAPI service
guardrails/config/   NeMo config.yml + prompts.yml
goldens/             synthetic golden set for the eval gate
evals/               DeepEval pytest gate
scripts/             make_sample_pdf, build_index, run_eval
app_streamlit.py     Streamlit UI
```

## Setup

Uses the shared project venv (Python 3.11). From this folder:

```bash
pip install -r requirements.txt
cp .env.example .env   # then fill in OPENAI_API_KEY
```

The text side runs locally with MiniLM and needs no key. A key is required for
answering (the vision model), guardrails, the A2A verifier, and the eval judge.

## Run it

Build the index (MiniLM and CLIP download on first run):

```bash
python scripts/make_sample_pdf.py
python scripts/build_index.py
```

FastAPI:

```bash
uvicorn src.api:app --host 0.0.0.0 --port 8000
```

- `GET /health`
- `POST /ask` with `{"question": "..."}`
- `POST /ask_a2a` with `{"question": "..."}`
- `GET /gateway/summary`

Streamlit:

```bash
streamlit run app_streamlit.py
```

## Evaluate

```bash
python scripts/run_eval.py
# equivalently:
deepeval test run evals/test_multimodal_rag.py
```

The judge is `gpt-4o-mini` and needs `OPENAI_API_KEY`. LLM-judged scores are
probabilistic evidence with reasons attached, not proof of correctness. Without
a key the suite skips every case rather than failing.

## Configuration

Everything is driven by environment variables read in `src/config.py`; see
`.env.example` for the full list. Notable switches:

- `TEXT_ENCODER_BACKEND=minilm|openai`
- `GUARDRAILS_ENABLED=true|false`
- `VISION_MODEL`, `GATEWAY_FALLBACKS`, `GATEWAY_CACHE`
- `EVAL_JUDGE_MODEL`, `EVAL_THRESHOLD`
