# Engineering standard for this project

All software work in this repository follows the agent-skills lifecycle discipline
defined by:

**https://github.com/addyosmani/agent-skills**

That pack encodes the quality gates senior engineers apply consistently. This file
records how it governs this project so any agent (or person) picking the work up
builds and tests the same way.

## The lifecycle: DEFINE -> PLAN -> BUILD -> VERIFY -> REVIEW -> SHIP

| Phase | What it means here |
|-------|--------------------|
| DEFINE | The scope is fixed: dual-encoder multimodal RAG, NeMo Guardrails, LiteLLM gateway, A2A loop, DeepEval offline gate, FastAPI + Streamlit. DevOps (ArgoCD, Ansible, k8s) is out of scope. |
| PLAN | Changes are atomic (~100 lines), each independently reviewable and safe to roll back. |
| BUILD | Implement incrementally. Source-driven: verify a library's real API before writing against it, never from memory. |
| VERIFY | Mandatory. "Seems right" is never sufficient. Every claim of "it works" is backed by runtime evidence (command output, a saved index, an HTTP response, a passing test), never assertion. |
| REVIEW | Read the target before overwriting or deleting. Match surrounding code conventions. |
| SHIP | Streamlit first (the user deploys there to confirm it runs), then the rest. |

## Non-negotiable rules carried into this project

- **Verification is evidence, not opinion.** No feature is "done" until its output is
  shown. Retrieval is proven by a real query returning real hits; the API is proven by
  a real request/response; the eval gate is proven by a real run.
- **Ground before writing.** Confirm the installed version and the real symbol/return
  shape of any library (transformers, deepeval, litellm, nemoguardrails) before coding.
  The transformers 5.x CLIP return-shape fix in `src/encoders.py` is an example of this
  rule catching a real bug.
- **No fabricated metrics.** Quote no eval or model number until code produces it. The
  sample document and its figures are synthetic and disclosed as such.
- **Secrets stay closed.** Never open, echo, or commit `.env` or key material. Code reads
  keys from the environment only.
- **Style.** No em-dash or en-dash in any project file. Cambridge B2 professional English.

## How to build and test (the canonical sequence)

```bash
pip install -r requirements.txt        # into the shared P1 venv (Python 3.11)
cp .env.example .env                    # add OPENAI_API_KEY (already provided for testing)

python scripts/make_sample_pdf.py       # synthetic data
python scripts/build_index.py           # build + save the dual FAISS index

# VERIFY (runtime evidence, in order)
python scripts/run_eval.py              # DeepEval gate (needs OPENAI_API_KEY)
uvicorn src.api:app --port 8000         # FastAPI: /health, /ask, /ask_a2a
streamlit run app_streamlit.py          # Streamlit UI (deploy target)
```

Nothing is reported as passing here that was not observed passing.
