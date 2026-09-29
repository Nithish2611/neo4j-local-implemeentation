"""LLM providers behind one interface: Ollama (local) and Gemini.

`history` is a list of {"role": "user" | "assistant", "content": str} turns that
precede the current `user` message.
"""
import random
import time
from collections.abc import Iterator
from typing import Protocol

from .config import Settings

History = list[dict]


class LLMClient(Protocol):
    name: str

    def complete(self, system: str, user: str, history: History | None = None) -> str: ...

    def stream(self, system: str, user: str, history: History | None = None) -> Iterator[str]: ...


class OllamaClient:
    """Any OpenAI-compatible endpoint (Ollama, Groq, ...)."""

    def __init__(self, base_url: str, model: str, api_key: str = "ollama") -> None:
        from openai import OpenAI

        # The SDK already retries 429/5xx with backoff; a long timeout covers
        # the first request while the local model loads into memory.
        self._client = OpenAI(base_url=base_url, api_key=api_key, max_retries=5, timeout=300)
        self._model = model
        self.name = f"ollama:{model}"

    def _messages(self, system: str, user: str, history: History | None) -> list[dict]:
        return [{"role": "system", "content": system}, *(history or []), {"role": "user", "content": user}]

    def complete(self, system: str, user: str, history: History | None = None) -> str:
        response = self._client.chat.completions.create(
            model=self._model, temperature=0.1, messages=self._messages(system, user, history),
        )
        return response.choices[0].message.content

    def stream(self, system: str, user: str, history: History | None = None) -> Iterator[str]:
        response = self._client.chat.completions.create(
            model=self._model, temperature=0.1, stream=True,
            messages=self._messages(system, user, history),
        )
        for chunk in response:
            if chunk.choices and chunk.choices[0].delta.content:
                yield chunk.choices[0].delta.content


class GeminiClient:
    RETRYABLE = frozenset({429, 500, 503, 504})

    def __init__(self, api_key: str, model: str, max_attempts: int = 6) -> None:
        from google import genai

        self._client = genai.Client(api_key=api_key)
        self._model = model
        self._max_attempts = max_attempts
        self.name = f"gemini:{model}"

    def _chat(self, system: str, history: History | None):
        from google.genai import types

        contents = [
            types.Content(role="model" if m["role"] == "assistant" else "user",
                          parts=[types.Part(text=m["content"])])
            for m in (history or [])
        ]
        config = types.GenerateContentConfig(system_instruction=system, temperature=0.1)
        return self._client.chats.create(model=self._model, config=config, history=contents)

    def _backoff(self, exc, attempt: int) -> None:
        delay = min(60.0, 2.0 * 2 ** (attempt - 1)) * random.uniform(0.5, 1.0)
        print(f"  Gemini returned {exc.code}; retry {attempt}/{self._max_attempts - 1} in {delay:.1f}s...")
        time.sleep(delay)

    def complete(self, system: str, user: str, history: History | None = None) -> str:
        from google.genai import errors

        for attempt in range(1, self._max_attempts + 1):
            try:
                return self._chat(system, history).send_message(user).text
            except errors.APIError as exc:
                if exc.code not in self.RETRYABLE or attempt == self._max_attempts:
                    raise
                self._backoff(exc, attempt)

    def stream(self, system: str, user: str, history: History | None = None) -> Iterator[str]:
        from google.genai import errors

        for attempt in range(1, self._max_attempts + 1):
            started = False
            try:
                for chunk in self._chat(system, history).send_message_stream(user):
                    if chunk.text:
                        started = True
                        yield chunk.text
                return
            except errors.APIError as exc:
                # Retrying after partial output would duplicate text, so only
                # retry failures that happen before the first chunk.
                if started or exc.code not in self.RETRYABLE or attempt == self._max_attempts:
                    raise
                self._backoff(exc, attempt)


def build_llm(settings: Settings, provider: str | None = None) -> LLMClient:
    provider = (provider or settings.llm_provider).lower()
    if provider == "ollama":
        return OllamaClient(settings.ollama_base_url, settings.ollama_model)
    if provider == "gemini":
        if not settings.gemini_api_key:
            raise ValueError("MY_API_KEY is not set (needed for the gemini provider).")
        return GeminiClient(settings.gemini_api_key, settings.gemini_model)
    raise ValueError(f"Unknown LLM provider {provider!r} (use 'ollama' or 'gemini').")
