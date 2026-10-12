"""Any OpenAI-compatible chat endpoint: OpenAI, OpenRouter, LM Studio, a local llama.cpp server, Ollama."""
from __future__ import annotations

import time

import httpx

from app.ai.base import LLMError, LLMProvider


class OpenAICompatibleProvider(LLMProvider):
    name = "openai"

    def __init__(self, cfg: dict, api_key: str, base_url: str):
        self.model = cfg.get("model", "gpt-4o-mini")
        self.base_url = (base_url or cfg.get("base_url") or "https://api.openai.com/v1").rstrip("/")
        self.api_key = api_key
        self.timeout = float(cfg.get("timeout_seconds", 60))
        self.temperature = float(cfg.get("temperature", 0.2))
        self.retries = int(cfg.get("retries", 3))
        self.json_mode = bool(cfg.get("json_mode", True))

    def available(self) -> tuple[bool, str]:
        if not self.api_key and "api.openai.com" in self.base_url:
            return False, "нет OPENAI_API_KEY в .env"
        return True, ""

    def generate(self, system: str, prompt: str, max_tokens: int = 700) -> str:
        body = {
            "model": self.model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}],
            "temperature": self.temperature,
            "max_tokens": max_tokens,
        }
        if self.json_mode:
            body["response_format"] = {"type": "json_object"}
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        last = None
        for attempt in range(self.retries):
            try:
                r = httpx.post(f"{self.base_url}/chat/completions", json=body, headers=headers, timeout=self.timeout)
            except httpx.HTTPError as exc:
                last = f"сеть: {exc}"
            else:
                if r.status_code == 200:
                    try:
                        return r.json()["choices"][0]["message"]["content"]
                    except (KeyError, IndexError, ValueError) as exc:
                        raise LLMError(f"неожиданный ответ API: {r.text[:200]}") from exc
                if r.status_code not in (408, 429) and r.status_code < 500:
                    raise LLMError(f"HTTP {r.status_code}: {r.text[:200]}")
                last = f"HTTP {r.status_code}"
            time.sleep(min(30, 2 ** attempt))
        raise LLMError(f"API недоступен после {self.retries} попыток: {last}")
