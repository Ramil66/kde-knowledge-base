"""Model-independent request boundary and JSON chat transports."""

from __future__ import annotations

import json
import os
import tomllib
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from http.client import HTTPException
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
    num_ctx: int = Field(default=2048, ge=512)
    num_predict: int = Field(default=64, ge=16)
    block_size: int = Field(default=1024, ge=32)
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
    on_chunk: Callable[[int], None] | None = None


@dataclass(frozen=True)
class GenerationResponse:
    content: str
    model: str
    input_tokens: int | None = None
    output_tokens: int | None = None


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

    @contextmanager
    def stream(self, route: str, payload: dict[str, Any], key: str | None = None) -> Iterator:
        headers = {"Content-Type": "application/json"}
        if key:
            headers["Authorization"] = f"Bearer {key}"
        request = Request(
            self.config.base_url + route, data=json.dumps(payload).encode(), headers=headers
        )
        try:
            with build_opener(NoRedirect()).open(request, timeout=self.config.timeout) as response:
                yield response
        except HTTPError as exc:
            raise ProviderError(f"Model endpoint returned HTTP {exc.code}") from exc
        except (URLError, OSError, HTTPException, json.JSONDecodeError) as exc:
            raise ProviderError("Model endpoint is unavailable or returned invalid JSON") from exc


class OllamaProvider(ChatProvider):
    def generate(self, request: GenerationRequest) -> GenerationResponse:
        options = {"temperature": self.config.temperature}
        if request.prompt_version.startswith("invent-"):
            options.update(num_ctx=self.config.num_ctx, num_predict=self.config.num_predict)
        payload = {
            "model": self.config.model,
            "messages": self.messages(request),
            "stream": True,
            "format": request.schema,
            "options": options,
        }
        parts = []
        with self.stream("/api/chat", payload) as response:
            for line in response:
                if not line.strip():
                    continue
                frame = json.loads(line)
                if (
                    not isinstance(frame, dict)
                    or "error" in frame
                    or not isinstance(frame.get("message"), dict)
                ):
                    raise ProviderError("Invalid Ollama stream frame")
                part = frame["message"].get("content", "")
                if not isinstance(part, str):
                    raise ProviderError("Invalid Ollama message content")
                if part:
                    parts.append(part)
                    if request.on_chunk:
                        request.on_chunk(len(part))
                if frame.get("done") is True:
                    if frame.get("done_reason") == "length":
                        raise ProviderError("Ollama reached its generation limit")
                    return GenerationResponse(
                        "".join(parts),
                        self.config.model,
                        frame.get("prompt_eval_count"),
                        frame.get("eval_count"),
                    )
        raise ProviderError("Incomplete Ollama stream")


class OpenAICompatibleProvider(ChatProvider):
    def generate(self, request: GenerationRequest) -> GenerationResponse:
        payload = {
            "model": self.config.model,
            "messages": self.messages(request),
            "stream": True,
            "stream_options": {"include_usage": True},
            "temperature": self.config.temperature,
            "response_format": {"type": "json_object"},
        }
        if request.prompt_version.startswith("invent-"):
            payload["max_tokens"] = self.config.num_predict
        parts = []
        usage = {}
        with self.stream(
            "/chat/completions", payload, os.environ.get("KDE_KB_LLM_API_KEY")
        ) as response:
            for line in response:
                line = line.strip()
                if not line.startswith(b"data:"):
                    continue
                data = line[5:].strip()
                if data == b"[DONE]":
                    return GenerationResponse(
                        "".join(parts),
                        self.config.model,
                        usage.get("prompt_tokens"),
                        usage.get("completion_tokens"),
                    )
                frame = json.loads(data)
                if (
                    not isinstance(frame, dict)
                    or "error" in frame
                    or not isinstance(frame.get("choices"), list)
                ):
                    raise ProviderError("Invalid chat completion stream")
                usage = frame.get("usage") or usage
                for choice in frame["choices"]:
                    if choice.get("finish_reason") == "length":
                        raise ProviderError("Chat completion reached its generation limit")
                    part = choice.get("delta", {}).get("content")
                    if part:
                        parts.append(part)
                        if request.on_chunk:
                            request.on_chunk(len(part))
        raise ProviderError("Incomplete chat completion stream")


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
