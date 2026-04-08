from __future__ import annotations

import json
from urllib import request

from job_apps_system.config.secrets import get_secret


class AnthropicClient:
    def __init__(self, session=None) -> None:
        self._api_key = get_secret("anthropic_api_key", session=session)
        if not self._api_key:
            raise ValueError("Anthropic API key is not configured.")

    def generate_text(
        self,
        *,
        model: str,
        system_prompt: str,
        user_prompt: str,
        max_tokens: int = 1800,
        temperature: float = 0.3,
    ) -> str:
        payload = {
            "model": model,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "system": system_prompt,
            "messages": [
                {
                    "role": "user",
                    "content": user_prompt,
                }
            ],
        }
        req = request.Request(
            "https://api.anthropic.com/v1/messages",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "content-type": "application/json",
                "x-api-key": self._api_key,
                "anthropic-version": "2023-06-01",
            },
            method="POST",
        )
        with request.urlopen(req, timeout=90) as response:
            data = json.loads(response.read().decode("utf-8"))

        parts = []
        for item in data.get("content", []):
            if item.get("type") == "text" and item.get("text"):
                parts.append(item["text"])
        return "\n".join(parts).strip()
