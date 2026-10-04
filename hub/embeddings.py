"""Embedder: local fastembed (ONNX) or deterministic fake backend."""
from __future__ import annotations

import hashlib
import logging
import sys
import threading
from dataclasses import dataclass

import numpy as np

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Profile:
    dim: int
    query_prefix: str
    doc_prefix: str
    chunk_chars: int


PROFILES: dict[str, Profile] = {
    "intfloat/multilingual-e5-small": Profile(384, "query: ", "passage: ", 900),
    "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2": Profile(384, "", "", 400),
    "BAAI/bge-small-en-v1.5": Profile(384, "", "", 900),
}
DEFAULT_PROFILE = Profile(384, "", "", 600)
FALLBACKS = [
    "intfloat/multilingual-e5-small",
    "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2",
    "BAAI/bge-small-en-v1.5",
]
FAKE_DIM = 32


def _normalize(m: np.ndarray) -> np.ndarray:
    m = np.asarray(m, dtype=np.float32)
    n = np.linalg.norm(m, axis=1, keepdims=True)
    n[n == 0] = 1.0
    return (m / n).astype(np.float32)


def _register_e5() -> None:
    from fastembed import TextEmbedding
    from fastembed.common.model_description import ModelSource, PoolingType

    name = "intfloat/multilingual-e5-small"
    if any(m["model"] == name for m in TextEmbedding.list_supported_models()):
        return
    TextEmbedding.add_custom_model(
        model=name,
        pooling=PoolingType.MEAN,
        normalization=True,
        sources=ModelSource(hf=name),
        dim=384,
        model_file="onnx/model.onnx",
    )


class Embedder:
    """embed_documents / embed_query return L2-normalized float32 arrays."""

    def __init__(self, backend: str, model: str, cache_dir: str = "", base_url: str = "",
                 api_key: str = "") -> None:
        self.backend = backend
        self.model = model if backend != "fake" else "fake"
        self.cache_dir = cache_dir
        self._lock = threading.Lock()
        self._te = None
        if backend == "fake":
            self.profile = Profile(FAKE_DIM, "", "", 900)
            self.dim = FAKE_DIM
        elif backend == "local":
            self._load_local(model)
            self.profile = PROFILES.get(self.model, DEFAULT_PROFILE)
            self.dim = self.profile.dim
        elif backend == "openai":
            from openai import OpenAI

            self._client = OpenAI(base_url=base_url or None, api_key=api_key or "not-needed",
                                  timeout=120, max_retries=2)
            self.dim = int(self._embed_openai(["dimension probe"]).shape[1])
            self.profile = Profile(self.dim, "", "", 900)
        else:
            raise ValueError(f"unsupported EMBED_BACKEND {backend!r} (local|openai|fake)")

    def _load_local(self, wanted: str) -> None:
        from fastembed import TextEmbedding

        order = [wanted] + [m for m in FALLBACKS if m != wanted]
        last: Exception | None = None
        for name in order:
            try:
                if name == "intfloat/multilingual-e5-small":
                    _register_e5()
                kwargs = {"cache_dir": self.cache_dir} if self.cache_dir else {}
                self._te = TextEmbedding(model_name=name, **kwargs)
                if name != wanted:
                    log.warning("embedding model %s unavailable, using %s", wanted, name)
                self.model = name
                return
            except Exception as e:  # noqa: BLE001
                log.warning("embedding model %s failed to load: %s", name, e)
                last = e
        raise RuntimeError("no embedding model could be loaded") from last

    def _fake(self, texts: list[str]) -> np.ndarray:
        out = np.zeros((len(texts), FAKE_DIM), dtype=np.float32)
        for i, t in enumerate(texts):
            for tok in t.lower().split():
                h = hashlib.sha256(tok.encode("utf-8")).digest()
                out[i, h[0] % FAKE_DIM] += 1.0
                out[i, h[1] % FAKE_DIM] += 0.5
        for i in range(len(texts)):
            if not out[i].any():
                out[i, 0] = 1.0
        return _normalize(out)

    def _embed_openai(self, texts: list[str]) -> np.ndarray:
        parts = []
        for i in range(0, len(texts), 64):
            resp = self._client.embeddings.create(model=self.model, input=texts[i : i + 64])
            parts.append(np.array([d.embedding for d in resp.data], dtype=np.float32))
        return _normalize(np.vstack(parts))

    def _embed(self, texts: list[str]) -> np.ndarray:
        if not texts:
            return np.zeros((0, self.dim), dtype=np.float32)
        if self.backend == "openai":
            return self._embed_openai(texts)
        if self.backend == "fake":
            return self._fake(texts)
        parts = []
        with self._lock:
            for i in range(0, len(texts), 16):
                parts.append(np.array(list(self._te.embed(texts[i : i + 16])), dtype=np.float32))
        return _normalize(np.vstack(parts))

    def embed_documents(self, texts: list[str]) -> np.ndarray:
        return self._embed([self.profile.doc_prefix + t for t in texts])

    def embed_query(self, text: str) -> np.ndarray:
        return self._embed([self.profile.query_prefix + text])[0]


def main(argv: list[str]) -> int:
    if "--download" not in argv:
        print("usage: python -m hub.embeddings --download")
        return 2
    from .config import Settings

    s = Settings.from_env()
    e = Embedder("local", s.embed_model, s.model_cache_dir)
    v = e.embed_query("hello")
    print(f"model={e.model} dim={v.shape[0]}")
    if e.model != s.embed_model:
        print(f"WARNING: requested {s.embed_model} but loaded {e.model}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
