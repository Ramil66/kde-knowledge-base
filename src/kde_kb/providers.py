"""Model-independent request boundary and JSON chat transports."""

from __future__ import annotations

import json
import os
import tomllib
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path
from typing import Any, Literal, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .database import CatalogError


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    provider: Literal["ollama", "openai-compatible", "disabled"]
    base_url: str
    model: str = Field(min_length=1)
    temperature: float = Field(ge=0, le=2)
    timeout: float = Field(gt=0, le=600)
    attempts: int = Field(ge=1, le=3)

    @field_validator("base_url")
    @classmethod
    def server_url(cls, value: str) -> str:
        parts = urlsplit(value)
        if (
            parts.scheme not in ("https", "http")
            or not parts.hostname
            or parts.username
            or parts.query
            or parts.fragment
        ):
            raise ValueError("Expected an HTTP(S) server URL without credentials")
        return value.rstrip("/")


def settings() -> Settings:
    defaults = tomllib.loads(files("kde_kb").joinpath("default.toml").read_text())["llm"]
    custom = os.environ.get("KDE_KB_CONFIG")
    path = (
        Path(custom).expanduser()
        if custom
        else Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
        / "kde-kb/config.toml"
    )
    if custom or path.exists():
        with path.open("rb") as config:
            defaults.update(tomllib.load(config).get("llm", {}))
    return Settings.model_validate(defaults)


@dataclass(frozen=True)
class GenerationRequest:
    system: str
    document: str
    schema: dict[str, Any]
    prompt_version: str


@dataclass(frozen=True)
class GenerationResponse:
    content: str
    model: str


class LLMProvider(Protocol):
    def generate(self, request: GenerationRequest) -> GenerationResponse: ...


class EmbeddingProvider(Protocol):
    def embed(self, texts: list[str]) -> list[list[float]]: ...


class TokenCounter(Protocol):
    def count(self, text: str) -> int: ...


class ProviderError(CatalogError):
    pass


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class ChatProvider:
    def __init__(self, config: Settings) -> None:
        self.config = config

    def messages(self, request: GenerationRequest) -> list[dict[str, str]]:
        return [
            {"role": "system", "content": request.system},
            {"role": "user", "content": request.document},
        ]

    def post(self, route: str, payload: dict[str, Any], key: str | None = None) -> dict:
        headers = {"Content-Type": "application/json"}
        if key:
            headers["Authorization"] = f"Bearer {key}"
        request = Request(
            self.config.base_url + route, data=json.dumps(payload).encode(), headers=headers
        )
        try:
            with build_opener(NoRedirect()).open(request, timeout=self.config.timeout) as response:
                return json.load(response)
        except HTTPError as exc:
            raise ProviderError(f"Model endpoint returned HTTP {exc.code}") from exc
        except (URLError, OSError, ValueError) as exc:
            raise ProviderError("Model endpoint is unavailable or returned invalid JSON") from exc


class OllamaProvider(ChatProvider):
    def generate(self, request: GenerationRequest) -> GenerationResponse:
        result = self.post(
            "/api/chat",
            {
                "model": self.config.model,
                "messages": self.messages(request),
                "stream": False,
                "format": request.schema,
                "options": {"temperature": self.config.temperature},
            },
        )
        try:
            content = result["message"]["content"]
            if not isinstance(content, str):
                raise TypeError
            return GenerationResponse(content, self.config.model)
        except (KeyError, TypeError) as exc:
            raise ProviderError("Missing Ollama message content") from exc


class OpenAICompatibleProvider(ChatProvider):
    def generate(self, request: GenerationRequest) -> GenerationResponse:
        result = self.post(
            "/chat/completions",
            {
                "model": self.config.model,
                "messages": self.messages(request),
                "temperature": self.config.temperature,
                "response_format": {"type": "json_object"},
            },
            os.environ.get("KDE_KB_LLM_API_KEY"),
        )
        try:
            content = result["choices"][0]["message"]["content"]
            if not isinstance(content, str):
                raise TypeError
            return GenerationResponse(content, self.config.model)
        except (KeyError, TypeError, IndexError) as exc:
            raise ProviderError("Missing chat completion content") from exc


class MockProvider:
    def __init__(self, *responses: str) -> None:
        self.responses = iter(responses)
        self.requests: list[GenerationRequest] = []

    def generate(self, request: GenerationRequest) -> GenerationResponse:
        self.requests.append(request)
        try:
            return GenerationResponse(next(self.responses), "mock")
        except StopIteration as exc:
            raise ProviderError("Mock model unavailable") from exc


def configured_provider(config: Settings) -> LLMProvider:
    if config.provider == "disabled":
        raise ProviderError("LLM extraction is disabled")
    if config.provider == "ollama":
        return OllamaProvider(config)
    return OpenAICompatibleProvider(config)
