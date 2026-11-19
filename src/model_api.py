"""Small OpenAI-compatible chat transport for the editorial pipeline."""
from types import SimpleNamespace

import httpx


class ModelClient:
    def __init__(self, *, api_key, timeout, base_url):
        self.http = httpx.Client(timeout=timeout, follow_redirects=False)
        self.url = base_url.rstrip("/") + "/chat/completions"
        self.headers = {"Authorization": f"Bearer {api_key}"}

    def create(self, *, model, max_tokens, system, messages, **options):
        response = self.http.post(self.url, headers=self.headers, json={
            "model": model, "max_tokens": max_tokens,
            "messages": [{"role": "system", "content": system}, *messages],
            **options,
        })
        response.raise_for_status()
        payload = response.json()
        choice = payload["choices"][0]
        raw_usage = payload.get("usage") or {}
        usage = SimpleNamespace(input_tokens=raw_usage.get("prompt_tokens"),
                                output_tokens=raw_usage.get("completion_tokens"),
                                cache_read_input_tokens=raw_usage.get("prompt_cache_hit_tokens"))
        reason = choice.get("finish_reason")
        return SimpleNamespace(
            content=[SimpleNamespace(text=choice["message"].get("content"))],
            stop_reason="end_turn" if reason == "stop" else reason,
            usage=usage,
        )

    def close(self):
        self.http.close()
