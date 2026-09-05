"""Central configuration for the multimodal RAG service.

Every tunable lives here so the rest of the code reads settings, never literals.
Values come from environment variables (loaded from a .env file at import time)
with sensible fallbacks. No secret is ever hardcoded: the OpenAI key is read
from the environment only.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

# Load .env once, at import. If it is missing, the environment is used as-is.
load_dotenv()

# --- Paths -----------------------------------------------------------------
ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"
ARTIFACTS_DIR = ROOT / "artifacts"
INDEX_DIR = ARTIFACTS_DIR / "index"
GOLDENS_DIR = ROOT / "goldens"
GUARDRAILS_DIR = ROOT / "guardrails" / "config"

for _d in (DATA_DIR, ARTIFACTS_DIR, INDEX_DIR):
    _d.mkdir(parents=True, exist_ok=True)


def _get_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    """Immutable settings snapshot for the whole service."""

    # --- Text encoder (the dual-encoder text side) -------------------------
    # Two backends are supported. "openai" uses text-embedding-3-small over the
    # gateway; "minilm" uses a local sentence-transformers model and needs no
    # key. The default is local so the pipeline runs offline for tests.
    text_encoder_backend: str = os.getenv("TEXT_ENCODER_BACKEND", "minilm")
    openai_text_embed_model: str = os.getenv(
        "OPENAI_TEXT_EMBED_MODEL", "text-embedding-3-small"
    )
    minilm_model: str = os.getenv(
        "MINILM_MODEL", "sentence-transformers/all-MiniLM-L6-v2"
    )

    # --- Image encoder (the dual-encoder image side) -----------------------
    clip_model: str = os.getenv("CLIP_MODEL", "openai/clip-vit-base-patch32")

    # --- Answering (vision) LLM, called through the gateway ----------------
    vision_model: str = os.getenv("VISION_MODEL", "gpt-4o-mini")
    gateway_fallbacks: tuple[str, ...] = field(
        default_factory=lambda: tuple(
            m.strip()
            for m in os.getenv(
                "GATEWAY_FALLBACKS", "gpt-4o,groq/openai/gpt-oss-120b"
            ).split(",")
            if m.strip()
        )
    )
    gateway_cache: bool = _get_bool("GATEWAY_CACHE", True)
    # Per-call wall-clock cap. A stalled provider fails at this point and the
    # fallback chain takes over, so no single call can hang the whole app.
    gateway_timeout_sec: float = float(os.getenv("GATEWAY_TIMEOUT_SEC", "60"))

    # --- Guardrails --------------------------------------------------------
    guardrails_enabled: bool = _get_bool("GUARDRAILS_ENABLED", True)
    guardrails_model: str = os.getenv("GUARDRAILS_MODEL", "gpt-4o-mini")
    # When guardrails are enabled but cannot run (no judge key, or the rails
    # fail to build/execute), the default is to answer UNGUARDED so the
    # service stays usable. Set this to block instead of degrading, for
    # deployments where an unguarded answer is worse than an error.
    guardrails_fail_closed: bool = _get_bool("GUARDRAILS_FAIL_CLOSED", False)

    # --- Retrieval ---------------------------------------------------------
    text_chunk_size: int = int(os.getenv("TEXT_CHUNK_SIZE", "500"))
    text_chunk_overlap: int = int(os.getenv("TEXT_CHUNK_OVERLAP", "100"))
    top_k_text: int = int(os.getenv("TOP_K_TEXT", "4"))
    top_k_image: int = int(os.getenv("TOP_K_IMAGE", "3"))

    # --- Eval gate ---------------------------------------------------------
    eval_judge_model: str = os.getenv("EVAL_JUDGE_MODEL", "gpt-4o-mini")
    eval_threshold: float = float(os.getenv("EVAL_THRESHOLD", "0.7"))
    # Fallback judge for the rare golden whose structured verdict list makes the
    # primary judge (gpt-4o-mini) loop up to its 16384-token output ceiling. This
    # model has a much larger output budget and speaks the same OpenAI-compatible
    # structured-output API, so it can finish the exact call gpt-4o-mini cannot.
    # Groq is already this project's gateway fallback provider (see
    # GATEWAY_FALLBACKS), so no new provider is introduced. Only used when the
    # primary judge hits the ceiling; every other verdict stays on gpt-4o-mini.
    # gpt-oss-20b (not the 120b or a reasoning model): it returns clean JSON on
    # DeepEval's text-generation path. The reasoning variants emit a thinking
    # preamble before the JSON, which DeepEval's trim_and_load_json rejects.
    eval_fallback_judge_model: str = os.getenv(
        "EVAL_FALLBACK_JUDGE_MODEL", "openai/gpt-oss-20b"
    )
    eval_fallback_judge_base_url: str = os.getenv(
        "EVAL_FALLBACK_JUDGE_BASE_URL", "https://api.groq.com/openai/v1"
    )
    eval_fallback_judge_key_env: str = os.getenv(
        "EVAL_FALLBACK_JUDGE_KEY_ENV", "GROQ_API_KEY"
    )

    # --- Answer verification guard loop (DeepEval runtime) -----------------
    verify_answers: bool = _get_bool("VERIFY_ANSWERS", False)
    verify_max_retries: int = int(os.getenv("VERIFY_MAX_RETRIES", "1"))

    @property
    def has_openai_key(self) -> bool:
        return bool(os.getenv("OPENAI_API_KEY"))

    @property
    def has_groq_key(self) -> bool:
        return bool(os.getenv("GROQ_API_KEY"))


SETTINGS = Settings()
