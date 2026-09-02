"""Streamlit UI for the multimodal RAG service.

Ask a question, pick the mode, and see the grounded answer plus the exact text
and image sources it used. The A2A mode also shows the retriever/verifier
transcript so the agent exchange is visible.

Run:
    streamlit run app_streamlit.py

Build the index first:
    python scripts/make_sample_pdf.py
    python scripts/build_index.py
"""

from __future__ import annotations

import base64

import streamlit as st

from src.config import SETTINGS
from src.gateway import get_gateway
from src.index import MultimodalIndex

st.set_page_config(page_title="Multimodal RAG", layout="wide")


@st.cache_resource(show_spinner=True)
def _load_index():
    """Load the saved index once per session."""
    try:
        return MultimodalIndex.load()
    except FileNotFoundError:
        return None


@st.cache_resource(show_spinner=False)
def _guard(_index):
    from src.guard import GuardedRAG

    return GuardedRAG(_index)


def _show_image(index: MultimodalIndex, image_id: str) -> None:
    b64 = index.image_store.get(image_id)
    if not b64:
        return
    st.image(base64.b64decode(b64), caption=image_id, width=360)


st.title("Multimodal RAG: dual-encoder, guardrails, A2A")
st.caption(
    "Text via a dedicated text encoder, images via CLIP, two separate indexes. "
    "Answers run behind NeMo input/output rails. The sample document is "
    "synthetic; its numbers are illustrative."
)

index = _load_index()
if index is None:
    st.warning(
        "No index found. Build one first:\n\n"
        "```\npython scripts/make_sample_pdf.py\n"
        "python scripts/build_index.py\n```"
    )
    st.stop()

with st.sidebar:
    st.subheader("Settings")
    st.write(f"Text backend: `{SETTINGS.text_encoder_backend}`")
    st.write(f"Vision model: `{SETTINGS.vision_model}`")
    st.write(f"Guardrails: `{'on' if SETTINGS.guardrails_enabled else 'off'}`")
    st.write(f"OpenAI key present: `{SETTINGS.has_openai_key}`")
    mode = st.radio("Mode", ["Guarded RAG", "A2A (retriever + verifier)"])

question = st.text_input("Ask a question about the document")
go = st.button("Ask", type="primary")

if go and question.strip():
    if mode == "Guarded RAG":
        with st.spinner("Answering behind guardrails..."):
            result = _guard(index).ask(question)
        if result["blocked"]:
            st.error(result["answer"])
        else:
            st.markdown("### Answer")
            st.write(result["answer"])
        ans_obj = result.get("answer_obj")
        if ans_obj is not None:
            with st.expander("Sources used"):
                st.write("Text chunks:")
                for h in ans_obj.context.text_hits:
                    st.write(f"- `{h.chunk.id}` (score {h.score:.3f})")
                    st.caption(h.chunk.text[:300])
                if ans_obj.context.image_hits:
                    st.write("Images:")
                    for h in ans_obj.context.image_hits:
                        _show_image(index, h.image_id)
    else:
        from src.a2a import run_a2a

        with st.spinner("Retriever and verifier are exchanging messages..."):
            res = run_a2a(index, question)
        st.markdown("### Answer")
        st.write(res.final_answer)
        badge = "accepted" if res.accepted else "not accepted (retries exhausted)"
        st.caption(f"Verifier verdict: {badge} after {res.rounds} round(s).")
        with st.expander("A2A transcript"):
            for m in res.transcript:
                st.markdown(f"**{m.sender} -> {m.kind}**")
                st.json(m.payload)

    with st.expander("Gateway audit (cost / tokens / models)"):
        st.json(get_gateway().summary())
