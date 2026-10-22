"""Thin wrapper around the Anthropic SDK shared by discovery, qualification and call analysis."""

from __future__ import annotations

from typing import Any, TypeVar

import anthropic
from pydantic import BaseModel

# Server-side fallback: if the model declines a request, the API re-runs it on a
# fallback model inside the same call instead of returning an empty refusal.
FALLBACK_BETA = "server-side-fallback-2026-07-01"

T = TypeVar("T", bound=BaseModel)


class ClaudeError(RuntimeError):
    pass


class Claude:
    def __init__(self, model: str, client: anthropic.Anthropic | None = None):
        self.model = model
        self._client = client

    @property
    def client(self) -> anthropic.Anthropic:
        if self._client is None:
            self._client = anthropic.Anthropic()
        return self._client

    def create(self, **kwargs: Any):
        response = self.client.beta.messages.create(
            model=self.model, betas=[FALLBACK_BETA], fallbacks="default", **kwargs
        )
        if response.stop_reason == "refusal":
            raise ClaudeError(f"model odmítl požadavek: {response.stop_details}")
        return response

    def parse(self, output_format: type[T], **kwargs: Any) -> T:
        response = self.client.beta.messages.parse(
            model=self.model,
            betas=[FALLBACK_BETA],
            fallbacks="default",
            output_format=output_format,
            **kwargs,
        )
        if response.stop_reason == "refusal":
            raise ClaudeError(f"model odmítl požadavek: {response.stop_details}")
        if response.stop_reason == "max_tokens":
            raise ClaudeError("odpověď byla useknuta (max_tokens)")
        parsed = response.parsed_output
        if parsed is None:
            raise ClaudeError("odpověď neobsahuje strukturovaný výstup")
        return parsed
