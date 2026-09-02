# Streamlit UI image for the multimodal RAG service.
#
# Design notes:
#  - python:3.11-slim keeps the base small; the app targets Python 3.11.
#  - Torch is installed from the CPU-only index so the image does not pull the
#    ~2-3 GB CUDA/cuDNN stack it never uses on a CPU host.
#  - The FAISS index and the synthetic sample PDF are baked in at build time, so
#    the container boots straight into a working UI with no build step and no
#    network fetch of the sample data.
#  - Model weights (MiniLM, CLIP) download on first run and are cached in the
#    layer's HF cache dir; set HF_HOME to a writable path.
#  - No secret is baked in. The answering LLM key is supplied at run time via the
#    OPENAI_API_KEY environment variable; retrieval itself uses local MiniLM and
#    needs no key.
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    HF_HOME=/app/.hf_cache

WORKDIR /app

# Build tooling only for the pip install step; removed in the same layer.
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*

# Install CPU-only torch first so the resolver does not pull the CUDA build,
# then the rest of the requirements.
RUN pip install --no-cache-dir torch --index-url https://download.pytorch.org/whl/cpu

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Build the sample PDF and the FAISS index at build time so the image is
# self-contained. If they are already present in the context this is a no-op
# refresh; either way the container starts with a ready index.
RUN python scripts/make_sample_pdf.py && python scripts/build_index.py

EXPOSE 8501

# Streamlit needs to bind all interfaces inside the container and skip its
# first-run e-mail prompt for headless operation.
ENV STREAMLIT_SERVER_ADDRESS=0.0.0.0 \
    STREAMLIT_SERVER_PORT=8501 \
    STREAMLIT_SERVER_HEADLESS=true \
    STREAMLIT_BROWSER_GATHER_USAGE_STATS=false

HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0) if urllib.request.urlopen('http://localhost:8501/_stcore/health').read()==b'ok' else sys.exit(1)"

CMD ["streamlit", "run", "app_streamlit.py"]
