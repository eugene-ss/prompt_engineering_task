from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

import httpx
from tenacity import (
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

from prompt_engineering.config import AppConfig, LLMConfig

logger = logging.getLogger(__name__)

_AUTH_HEADER = "api-key"

# Azure OpenAI chat completions URL (placeholders: base, deployment, api_version).
_COMPLETIONS_URL_TEMPLATE = (
    "{base}/openai/deployments/{deployment}/chat/completions?api-version={api_version}"
)

def _is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, (httpx.ConnectError, httpx.ReadTimeout)):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code == 429 or exc.response.status_code >= 500
    return False

@dataclass
class LLMResponse:
    raw: dict[str, Any] = field(default_factory=dict)
    latency_ms: float = 0.0

    @property
    def content(self) -> str:
        choices = self.raw.get("choices", [])
        if not choices:
            return ""
        return choices[0].get("message", {}).get("content", "")

    @property
    def finish_reason(self) -> str | None:
        choices = self.raw.get("choices", [])
        if not choices:
            return None
        return choices[0].get("finish_reason")

class LLMClient:
    def __init__(self, config: AppConfig, llm_config: LLMConfig) -> None:
        self._config = config
        self._llm_config = llm_config
        base = config.ENDPOINT_URL.rstrip("/")
        self._url = _COMPLETIONS_URL_TEMPLATE.format(
            base=base,
            deployment=llm_config.model,
            api_version=llm_config.api,
        )
        self._headers = {
            _AUTH_HEADER: config.API_KEY.get_secret_value(),
            "Content-Type": "application/json",
        }
        self._client: httpx.AsyncClient | None = None

    async def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(self._config.REQUEST_TIMEOUT_SECONDS),
                limits=httpx.Limits(
                    max_connections=self._config.MAX_CONCURRENT_REQUESTS,
                    max_keepalive_connections=20,
                ),
            )
        return self._client

    async def close(self) -> None:
        if self._client and not self._client.is_closed:
            await self._client.aclose()

    @retry(
        retry=retry_if_exception(_is_retryable),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=30),
        reraise=True,
    )
    async def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> LLMResponse:
        temp = temperature if temperature is not None else self._llm_config.temperature
        tokens = max_tokens if max_tokens is not None else self._llm_config.max_tokens
        client = await self._ensure_client()
        body: dict[str, Any] = {
            "messages": messages,
            "temperature": temp,
            "max_tokens": tokens,
        }

        logger.debug("LLM request to %s (msgs=%d)", self._url, len(messages))
        t0 = time.perf_counter()
        resp = await client.post(self._url, json=body, headers=self._headers)
        latency_ms = (time.perf_counter() - t0) * 1000

        if resp.status_code in (401, 403):
            key_preview = self._headers.get(_AUTH_HEADER, "")[:8]
            logger.error(
                "Authentication failed (%d). API key starts with '%s...'. "
                "Check that API_KEY is correct.",
                resp.status_code,
                key_preview,
            )

        resp.raise_for_status()
        data = resp.json()
        finish = data.get("choices", [{}])[0].get("finish_reason")
        logger.debug("LLM response in %.0f ms  finish=%s", latency_ms, finish)

        return LLMResponse(raw=data, latency_ms=latency_ms)