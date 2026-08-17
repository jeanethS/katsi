from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING

from pydantic import ValidationError

from katsi_core.config import Settings
from katsi_core.models import Extraction

if TYPE_CHECKING:
    import ollama


_FENCE_RE = re.compile(r"^```(?:json)?\s*\n?|\n?```\s*$", re.DOTALL | re.MULTILINE)


def _clean_json(raw: str) -> str:
    """Strip markdown code fences and surrounding whitespace from raw LLM output."""
    s = raw.strip()
    s = _FENCE_RE.sub("", s).strip()
    return s


class ExtractionError(Exception):
    """Raised when the local LLM fails to return valid Extraction JSON
    after the retry."""


SYSTEM_PROMPT = """You are a precise document analyzer.
Read the user-provided text and respond with ONE JSON object matching exactly
the shape:

{
  "summary": "string: a 1-3 sentence summary of the document",
  "entities": [{"name": "string", "kind": "string in [person, org, project]"}],
  "topics": ["string: a topic the document is about"],
  "references": ["string: any file paths, filenames, or document references this file points at"]
}

Return ONLY the JSON object. No prose, no code fences, no markdown, no
preamble, no trailing text. Output nothing else."""


# Handed to ollama as a decoding constraint. Written out explicitly rather than
# derived from Extraction.model_json_schema(): `entities` is typed `list[dict]`
# there, which produces `additionalProperties: true` with no required keys, so
# the model may emit entities lacking name/kind that StrictExtraction rejects.
EXTRACTION_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "entities": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "kind": {"type": "string", "enum": ["person", "org", "project"]},
                },
                "required": ["name", "kind"],
                "additionalProperties": False,
            },
        },
        "topics": {"type": "array", "items": {"type": "string"}},
        "references": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["summary", "entities", "topics", "references"],
    "additionalProperties": False,
}


class LLMClient:
    """Ollama chat client with strict-JSON extraction.  Retries ONCE on a parse
    failure, then raises :class:`ExtractionError`."""

    def __init__(
        self,
        settings: Settings | None = None,
        client: ollama.Client | None = None,
    ) -> None:
        """Settings + optional injected client (deferred if ``None``)."""
        self._settings = settings or Settings()
        self._client = client

    def _get_client(self) -> ollama.Client:
        if self._client is None:
            import ollama

            self._client = ollama.Client(
                host=self._settings.ollama.host,
                timeout=self._settings.ollama.timeout,
            )
        return self._client

    def _chat(self, system_prompt: str, user_text: str, format: object = "json") -> str:
        """Internal: invoke ollama chat and return the content string.

        ``format`` accepts ollama's ``"json"`` free-form mode or a JSON Schema
        dict, which constrains decoding to that exact shape.

        Uses ``self._settings.ollama.llm_model``.
        """
        resp = self._get_client().chat(
            model=self._settings.ollama.llm_model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_text},
            ],
            format=format,
            options={"temperature": 0.1, "num_ctx": self._settings.ollama.num_ctx},
        )
        return resp.message.content

    def chat(
        self,
        user_text: str,
        *,
        system: str | None = None,
        temperature: float = 0.1,
        model: str | None = None,
        max_tokens: int | None = None,
    ) -> str:
        """Raw chat returning the model's ``message.content`` string.

        ``model`` overrides ``ollama.llm_model``; ``max_tokens`` caps the
        response via ollama's ``num_predict`` option.
        """
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": user_text})
        options: dict = {"temperature": temperature}
        if max_tokens is not None:
            options["num_predict"] = max_tokens
        resp = self._get_client().chat(
            model=model or self._settings.ollama.llm_model,
            messages=messages,
            options=options,
        )
        return resp.message.content

    def extract(self, text: str, *, attempts: int = 2) -> Extraction:
        """Single-model-call extraction of the :class:`Extraction` JSON contract.

        Loop up to *attempts* times:

        1. Call ``_chat(SYSTEM_PROMPT, text)`` — temperature 0.1, format json.
        2. Parse the returned content as JSON.  Tolerate: leading/trailing
           whitespace; a leading ``\\`\\`\\`json`` or ``\\`\\`\\``` fence pair
           surrounding the JSON; a trailing ``\\`\\`\\``` if fence open was stripped.
        3. Validate into ``Extraction(**parsed)``.  On
           :class:`ValidationError` or :class:`json.JSONDecodeError`, retry
           (up to *attempts* times).

        If all attempts fail, raises :class:`ExtractionError` with a message
        including the original error + the raw model output for the final
        attempt.
        """
        last_err: Exception | None = None
        last_raw = ""
        # Anything past the context window is dropped by ollama anyway; cutting
        # it here keeps the prompt coherent instead of silently truncated.
        budget = self._settings.ingest.max_extraction_chars
        if len(text) > budget:
            text = text[:budget]
        for _ in range(attempts):
            try:
                raw = self._chat(SYSTEM_PROMPT, text, format=EXTRACTION_SCHEMA)
                last_raw = raw
                cleaned = _clean_json(raw)
                parsed = json.loads(cleaned)
                if isinstance(parsed, str):
                    parsed = json.loads(parsed)
                return Extraction(**parsed)
            except (json.JSONDecodeError, TypeError, ValueError, ValidationError) as e:
                last_err = e
                continue
        raise ExtractionError(
            f"LLM did not return valid Extraction JSON after {attempts} attempts. "
            f"Last error: {last_err!r}. Last raw output: {last_raw[:400]!r}"
        )
