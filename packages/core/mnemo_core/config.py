"""mnemo configuration.

Loads from mnemo.toml (TOML file) with env var overrides.
Never hardcode model names / paths / thresholds — read from this Settings object.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class OllamaSettings(BaseModel):
    host: str = "http://localhost:11434"
    embed_model: str = "bge-m3"
    llm_model: str = "qwen2.5:7b"
    timeout: float = 120.0


class StoreSettings(BaseModel):
    data_dir: Path = Path.home() / ".mnemo"
    lancedb_table: str = "chunks"
    kuzu_db: str = "graph"


class IngestSettings(BaseModel):
    chunk_token_target: int = 512
    chunk_token_overlap: int = 64
    dedup_similarity_threshold: float = 0.92
    include_globs: list[str] = Field(
        default_factory=lambda: [
            "**/*.md",
            "**/*.txt",
            "**/*.py",
            "**/*.ts",
            "**/*.pdf",
            "**/*.docx",
        ]
    )
    exclude_globs: list[str] = Field(
        default_factory=lambda: [
            "**/.git/**",
            "**/node_modules/**",
            "**/.venv/**",
            "**/__pycache__/**",
        ]
    )


class RetrieveSettings(BaseModel):
    top_k_chunks: int = 16
    top_k_files: int = 8
    graph_expand_hops: int = 1
    vector_weight: float = 0.6
    graph_weight: float = 0.4
    default_context_max_tokens: int = 3000


class MCPSettings(BaseModel):
    enable_answer_tool: bool = False


class SynthLocalSettings(BaseModel):
    model: str = "qwen2.5:7b"
    max_tokens: int = 800


class SynthCloudSettings(BaseModel):
    provider: str = "anthropic"
    model: str = ""
    api_key_env: str = "ANTHROPIC_API_KEY"
    enable_prompt_caching: bool = True
    max_tokens: int = 1024


class SynthAutoSettings(BaseModel):
    escalate_when_files_gte: int = 4
    escalate_when_tokens_gte: int = 2500
    escalate_on_intents: list[str] = Field(
        default_factory=lambda: ["compare", "contrast", "synthesize", "across", "difference"]
    )
    fallback_to_local_if_cloud_unavailable: bool = True


class SynthSettings(BaseModel):
    backend: str = "return_only"
    allow_per_call_override: bool = True
    local: SynthLocalSettings = Field(default_factory=SynthLocalSettings)
    cloud: SynthCloudSettings = Field(default_factory=SynthCloudSettings)
    auto: SynthAutoSettings = Field(default_factory=SynthAutoSettings)


class Settings(BaseSettings):
    """Top-level settings. Loaded from mnemo.toml if present, env-overridable."""

    model_config = SettingsConfigDict(
        env_prefix="MNEMO_",
        env_nested_delimiter="__",
        extra="ignore",
    )

    ollama: OllamaSettings = Field(default_factory=OllamaSettings)
    store: StoreSettings = Field(default_factory=StoreSettings)
    ingest: IngestSettings = Field(default_factory=IngestSettings)
    retrieve: RetrieveSettings = Field(default_factory=RetrieveSettings)
    mcp: MCPSettings = Field(default_factory=MCPSettings)
    synth: SynthSettings = Field(default_factory=SynthSettings)

    @classmethod
    def load(cls, config_path: Path | None = None) -> Settings:
        """Load settings from a TOML file or default locations.

        Env vars (MNEMO_*) override the file. If no path given, look for
        mnemo.toml in CWD then ~/.mnemo/mnemo.toml.
        """
        env_settings = cls()
        if config_path is None:
            cwd_path = Path.cwd() / "mnemo.toml"
            home_path = Path.home() / ".mnemo" / "mnemo.toml"
            config_path = (
                cwd_path if cwd_path.exists() else (home_path if home_path.exists() else None)
            )
        if config_path is None or not config_path.exists():
            return env_settings
        with open(config_path, "rb") as f:
            data = tomllib.load(f).get("mnemo", {})
        if not data:
            return env_settings
        # Merge file values over env defaults (env already resolved above).
        ovr = {}
        for k, v in env_settings.model_dump().items():
            section = data.get(k, {})
            merged = v.copy()
            merged.update(section)
            ovr[k] = merged
        return cls(**ovr)


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings.load()
    return _settings


def reset_settings() -> None:
    """Test helper: clears the cached singleton."""
    global _settings
    _settings = None
