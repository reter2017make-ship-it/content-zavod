"""Local GGUF model through llama.cpp (llama-cpp-python, Metal on Apple Silicon). Works on macOS 13.2."""
from __future__ import annotations

import logging
import threading

from app.ai.base import LLMError, LLMProvider

log = logging.getLogger("ai.llamacpp")


class LlamaCppProvider(LLMProvider):
    name = "llamacpp"

    def __init__(self, cfg: dict):
        self.repo_id = cfg.get("repo_id", "Qwen/Qwen2.5-7B-Instruct-GGUF")
        self.files = list(cfg.get("files") or [])
        self.model_path = cfg.get("model_path")
        self.n_ctx = int(cfg.get("n_ctx", 4096))
        self.gpu_layers = int(cfg.get("n_gpu_layers", -1))
        self.temperature = float(cfg.get("temperature", 0.2))
        self.allow_download = bool(cfg.get("allow_download", True))
        self._llm = None
        self._lock = threading.Lock()

    def available(self) -> tuple[bool, str]:
        try:
            import llama_cpp  # noqa: F401
        except Exception as exc:  # ImportError or a dylib that does not load on this macOS
            return False, f"llama-cpp-python не загружается: {exc}"
        try:
            self._resolve_path(download=False)
        except Exception:
            if not self.allow_download:
                return False, f"модель {self.repo_id} не скачана"
        return True, ""

    def _resolve_path(self, download: bool) -> str:
        if self.model_path:
            return self.model_path
        if not self.files:
            raise LLMError("в config.yaml не указаны ai.llamacpp.files")
        from huggingface_hub import hf_hub_download
        paths = [hf_hub_download(self.repo_id, f, local_files_only=not download) for f in self.files]
        return paths[0]  # llama.cpp picks up the other split parts from the same folder

    def _model(self):
        with self._lock:
            if self._llm is None:
                from llama_cpp import Llama
                path = self._resolve_path(download=self.allow_download)
                log.info("loading %s", path)
                self._llm = Llama(model_path=path, n_ctx=self.n_ctx, n_gpu_layers=self.gpu_layers, verbose=False)
            return self._llm

    def generate(self, system: str, prompt: str, max_tokens: int = 700) -> str:
        try:
            out = self._model().create_chat_completion(
                messages=[{"role": "system", "content": system}, {"role": "user", "content": prompt}],
                max_tokens=max_tokens,
                temperature=self.temperature,
                response_format={"type": "json_object"},
            )
            return out["choices"][0]["message"]["content"]
        except LLMError:
            raise
        except Exception as exc:
            raise LLMError(f"llama.cpp: {exc}") from exc
