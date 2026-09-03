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
import tempfile
from pathlib import Path

import streamlit as st

from src.config import SETTINGS
from src.gateway import get_gateway
from src.index import MultimodalIndex

st.set_page_config(page_title="Multimodal RAG", layout="wide")


@st.cache_resource(show_spinner=True)
def _load_index():
    """Load the saved sample index once per session.

    A missing index is the expected first-run case and returns None quietly so
    the page prompts for an upload. Any other failure (a partial or corrupt
    index, a faiss or pickle read error) also returns None but surfaces the
    reason, so a broken sample index degrades to "upload a PDF" instead of
    crashing the whole page.
    """
    try:
        return MultimodalIndex.load()
    except FileNotFoundError:
        return None
    except Exception as exc:  # noqa: BLE001 - a bad index must not crash the UI
        st.warning(
            f"The saved sample index could not be loaded ({exc}). "
            "Upload a PDF to build a fresh one."
        )
        return None


@st.cache_resource(show_spinner=False)
def _guard(_index):
    from src.guard import GuardedRAG

    return GuardedRAG(_index)


def _build_index_from_pdf(pdf_bytes: bytes, doc_id: str) -> MultimodalIndex:
    """Ingest an uploaded PDF (text + images) and build a fresh index from it.

    The file is written to a temp path only for PyMuPDF to open, then removed.
    The resulting index lives in the session; nothing is persisted server-side,
    which is what we want on an ephemeral, shared host.
    """
    from src.ingest import ingest_pdf

    tmp = None
    try:
        with tempfile.NamedTemporaryFile(
            suffix=".pdf", delete=False
        ) as fh:
            fh.write(pdf_bytes)
            tmp = fh.name
        ingested = ingest_pdf(tmp, doc_id=doc_id)
        index = MultimodalIndex().build([ingested])
        index._uploaded_stats = (  # noqa: SLF001  small UI-only annotation
            len(ingested.text_chunks),
            len(ingested.images),
        )
        return index
    finally:
        if tmp:
            try:
                Path(tmp).unlink()
            except OSError:
                pass


def _show_image(index: MultimodalIndex, image_id: str) -> None:
    b64 = index.image_store.get(image_id)
    if not b64:
        return
    st.image(base64.b64decode(b64), caption=image_id, width=360)


st.title("Multimodal RAG: dual-encoder, guardrails, A2A")
st.caption(
    "Text via a dedicated text encoder, images via CLIP, two separate indexes. "
    "Answers run behind NeMo input/output rails. The default document is the "
    "paper \"Attention Is All You Need\"; upload your own PDF to replace it."
)

sample_index = _load_index()

with st.sidebar:
    st.subheader("Your document")
    st.caption(
        "Upload a PDF that mixes text and images. Both are indexed: text through "
        "the text encoder, page images through CLIP. Your upload replaces the "
        "default document for this session and is not stored on the server."
    )
    uploaded = st.file_uploader("Upload a PDF", type=["pdf"])
    if uploaded is not None:
        # Rebuild only when a different file is uploaded, not on every rerun.
        sig = (uploaded.name, uploaded.size)
        if st.session_state.get("_uploaded_sig") != sig:
            with st.spinner(f"Ingesting {uploaded.name} (text + images)..."):
                try:
                    st.session_state["_uploaded_index"] = _build_index_from_pdf(
                        uploaded.getvalue(), doc_id=Path(uploaded.name).stem
                    )
                    st.session_state["_uploaded_sig"] = sig
                except Exception as exc:  # surface the reason, do not crash the app
                    st.session_state.pop("_uploaded_index", None)
                    st.session_state.pop("_uploaded_sig", None)
                    st.error(f"Could not read that PDF: {exc}")
        idx = st.session_state.get("_uploaded_index")
        if idx is not None:
            n_txt, n_img = getattr(idx, "_uploaded_stats", (0, 0))
            st.success(
                f"Indexed **{uploaded.name}**: {n_txt} text chunk(s), "
                f"{n_img} image(s)."
            )
            if n_txt == 0 and n_img == 0:
                st.warning(
                    "No text or images were extracted. If this is a scanned PDF, "
                    "the pages are images of text that this build does not OCR."
                )
    if st.session_state.get("_uploaded_index") is not None:
        if st.button("Clear upload, use default"):
            st.session_state.pop("_uploaded_index", None)
            st.session_state.pop("_uploaded_sig", None)
            st.rerun()

    st.subheader("Settings")
    st.write(f"Text backend: `{SETTINGS.text_encoder_backend}`")
    st.write(f"Vision model: `{SETTINGS.vision_model}`")
    st.write(f"Guardrails: `{'on' if SETTINGS.guardrails_enabled else 'off'}`")
    st.write(f"OpenAI key present: `{SETTINGS.has_openai_key}`")
    mode = st.radio("Mode", ["Guarded RAG", "A2A (retriever + verifier)"])

# The uploaded document, when present, is the active index; otherwise the sample.
index = st.session_state.get("_uploaded_index") or sample_index
if index is None:
    st.info(
        "No document loaded yet. Upload a PDF in the sidebar to get started. "
        "It can contain both text and images."
    )
    st.stop()

active_label = (
    "your uploaded PDF"
    if st.session_state.get("_uploaded_index") is not None
    else "the default paper (Attention Is All You Need)"
)
st.caption(f"Answering from: **{active_label}**.")

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
