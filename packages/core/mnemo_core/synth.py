from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from mnemo_core.clients.llm import LLMClient
from mnemo_core.config import Settings
from mnemo_core.models import ContextBundle

logger = logging.getLogger(__name__)


class SynthConfigError(ValueError):
    pass


class SynthUnavailableError(RuntimeError):
    pass


@dataclass
class SynthResult:
    text: str | None
    bundle: ContextBundle
    mode: str
    escalated: bool = False


@runtime_checkable
class Synthesizer(Protocol):
    def answer(self, question: str, bundle: ContextBundle) -> SynthResult: ...


def render_bundle_prompt(question: str, bundle: ContextBundle) -> str:
    lines = [
        "Answer the question using ONLY the context below. If the context is",
        "insufficient, say so briefly.",
        "",
        "# Question",
        question,
    ]
    if bundle.files:
        lines.append("")
        lines.append("# Files")
        for h in bundle.files:
            summary = h.summary or "(no summary)"
            lines.append(f"- {h.path} (score={h.score:.3f}; {h.why})")
            lines.append(f"  SUMMARY: {summary}")
    if bundle.chunks:
        lines.append("")
        lines.append("# Top chunks")
        for c in bundle.chunks:
            lines.append(f"--- {c.id} ({c.token_count} tokens) ---")
            lines.append(c.text)
    if bundle.relationships:
        lines.append("")
        lines.append("# Relationships")
        lines.extend(bundle.relationships)
    return "\n".join(lines)


class ReturnOnlySynthesizer:
    def answer(self, question: str, bundle: ContextBundle) -> SynthResult:
        return SynthResult(text=None, bundle=bundle, mode="return_only")


class LocalSynthesizer:
    def __init__(self, settings: Settings, client: LLMClient | None = None) -> None:
        self._settings = settings
        self._llm = client or LLMClient(settings)

    def answer(self, question: str, bundle: ContextBundle) -> SynthResult:
        prompt = render_bundle_prompt(question, bundle)
        try:
            text = self._llm.chat(prompt, temperature=0.2)
        except Exception as exc:
            raise SynthUnavailableError(
                f"Local model {self._settings.ollama.llm_model} at "
                f"{self._settings.ollama.host} unavailable: {exc}"
            ) from exc
        return SynthResult(text=text, bundle=bundle, mode="local")


class CloudSynthesizer:
    def __init__(self, settings: Settings, client=None) -> None:
        self._settings = settings
        self._client = client
        cloud = settings.synth.cloud
        if not cloud.model:
            raise SynthConfigError(
                "cloud mode requires synth.cloud.model; "
                "set [mnemo.synth.cloud] model=... in mnemo.toml"
            )
        api_key = os.environ.get(cloud.api_key_env)
        if not api_key:
            raise SynthConfigError(
                f"cloud mode requires {cloud.api_key_env} env var; "
                f"set {cloud.api_key_env}=<your key>"
            )
        self._api_key = api_key

    def _get_client(self):
        if self._client is None:
            import anthropic

            self._client = anthropic.Anthropic(api_key=self._api_key)
        return self._client

    def answer(self, question: str, bundle: ContextBundle) -> SynthResult:
        cloud = self._settings.synth.cloud
        prompt = render_bundle_prompt(question, bundle)
        system_content = (
            "You are a precise file-context assistant. Answer the user's question "
            "using ONLY the provided context. Be concise and grounded. "
            "If the context is insufficient, say so briefly."
        )
        system = (
            [{"type": "text", "text": system_content, "cache_control": {"type": "ephemeral"}}]
            if cloud.enable_prompt_caching
            else system_content
        )
        messages = [{"role": "user", "content": prompt}]
        try:
            client = self._get_client()
            response = client.messages.create(
                model=cloud.model,
                system=system,
                messages=messages,
                max_tokens=1024,
            )
        except Exception as exc:
            raise SynthUnavailableError(
                f"Cloud model {cloud.model} ({cloud.provider}) unavailable: {exc}"
            ) from exc
        text = response.content[0].text
        return SynthResult(text=text, bundle=bundle, mode="cloud")


class AutoSynthesizer:
    def __init__(self, settings: Settings, *, local=None, cloud=None) -> None:
        self._settings = settings
        self._local = local
        self._cloud = cloud

    def answer(self, question: str, bundle: ContextBundle) -> SynthResult:
        auto = self._settings.synth.auto
        n_files = max(len(bundle.files), len({c.file_id for c in bundle.chunks}))
        escalate = False
        if n_files >= auto.escalate_when_files_gte:
            escalate = True
        if bundle.token_estimate >= auto.escalate_when_tokens_gte:
            escalate = True
        if any(kw in question.lower() for kw in auto.escalate_on_intents):
            escalate = True
        if escalate:
            cloud = self._cloud or CloudSynthesizer(self._settings)
            try:
                result = cloud.answer(question, bundle)
                result.escalated = True
                return result
            except (SynthUnavailableError, SynthConfigError) as exc:
                if auto.fallback_to_local_if_cloud_unavailable:
                    logger.warning("cloud unavailable, falling back to local: %s", exc)
                    local = self._local or LocalSynthesizer(self._settings)
                    result = local.answer(question, bundle)
                    result.escalated = True
                    return result
                raise
        local = self._local or LocalSynthesizer(self._settings)
        return local.answer(question, bundle)


def build_synthesizer(
    settings: Settings, mode: str | None = None, llm_client: LLMClient | None = None
) -> Synthesizer:
    if mode is not None and not settings.synth.allow_per_call_override:
        raise SynthConfigError(
            "per-call mode override disabled; set synth.allow_per_call_override=true"
        )
    resolved = mode if mode is not None else settings.synth.backend
    if resolved == "return_only":
        return ReturnOnlySynthesizer()
    if resolved == "local":
        return LocalSynthesizer(settings, client=llm_client)
    if resolved == "cloud":
        return CloudSynthesizer(settings)
    if resolved == "auto":
        return AutoSynthesizer(settings)
    raise ValueError(f"Unknown synthesis mode: {resolved!r}")
