from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


@dataclass(frozen=True)
class LLMConfig:
    base_url: str
    api_key: str
    model: str
    temperature: float


class LLMClient:
    def __init__(self, cfg: LLMConfig) -> None:
        self._cfg = cfg

    def chat_completions(self, messages: list[dict[str, str]], timeout_s: int = 30) -> str | None:
        if not self._cfg.api_key:
            return None

        base = self._cfg.base_url.rstrip("/")
        url = f"{base}/v1/chat/completions"
        body = {
            "model": self._cfg.model,
            "temperature": self._cfg.temperature,
            "messages": messages,
        }

        req = Request(
            url=url,
            data=json.dumps(body).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self._cfg.api_key}",
            },
            method="POST",
        )

        try:
            with urlopen(req, timeout=timeout_s) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except (HTTPError, URLError, TimeoutError, ValueError):
            return None

        choices = payload.get("choices") or []
        if not choices:
            return None
        msg = (choices[0].get("message") or {}).get("content")
        if not isinstance(msg, str) or not msg.strip():
            return None
        return msg.strip()

    def extract_json(self, messages: list[dict[str, str]], timeout_s: int = 30) -> dict[str, Any] | None:
        text = self.chat_completions(messages, timeout_s=timeout_s)
        if not text:
            return None
        start = text.find("{")
        end = text.rfind("}")
        if start == -1 or end == -1 or end <= start:
            return None
        try:
            return json.loads(text[start : end + 1])
        except ValueError:
            return None
