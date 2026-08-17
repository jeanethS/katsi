"""katsi configuration.

Loads from katsi.toml (TOML file) with env var overrides.
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
    data_dir: Path = Path.home() / ".katsi"
    lancedb_table: str = "chunks"
    kuzu_db: str = "graph"


class SQLiteSettings(BaseModel):
    """Configuration for the private authoritative workspace database."""

    filename: str = "workspace.sqlite3"
    busy_timeout_ms: int = Field(default=5_000, ge=0)
    schema_version: int = Field(default=4, ge=1)


class PortableStateSettings(BaseModel):
    """Location of owner-approved metadata relative to each workspace root."""

    relative_path: Path = Path(".katsi/project-state.json")


class ObserverSettings(BaseModel):
    enabled: bool = True
    debounce_seconds: float = Field(default=0.5, ge=0)
    stable_read_retries: int = Field(default=2, ge=0)
    stable_read_retry_seconds: float = Field(default=0.1, ge=0)
    max_file_bytes: int = Field(default=20_000_000, gt=0)
    reserved_path_prefix: str = ".katsi-stage-"


class LeaseSettings(BaseModel):
    advisory_ttl_seconds: int = Field(default=1_800, gt=0)
    exclusive_ttl_seconds: int = Field(default=300, gt=0)


class OperationLimitSettings(BaseModel):
    max_operations: int = Field(default=100, gt=0)
    max_affected_bytes: int = Field(default=100_000_000, gt=0)
    max_risk_class: str = "medium"


class RecoverySettings(BaseModel):
    blob_directory: str = "recovery"
    retention_days: int = Field(default=30, ge=0)
    staging_prefix: str = ".katsi-stage-"


class ProjectionWorkerSettings(BaseModel):
    batch_size: int = Field(default=100, gt=0)
    retry_delay_seconds: float = Field(default=1.0, ge=0)


class BriefSettings(BaseModel):
    """Serialization controls for Workspace Brief assembly.

    The byte budget itself is caller-supplied on each assembly call (OpenSpec
    task 10.5); these controls only bound how much recent history is consulted.
    """

    recent_event_limit: int = Field(default=20, gt=0)


class EnrichmentSettings(BaseModel):
    """Versioned inputs that decide whether local enrichment may be reused."""

    extraction_contract_version: str = "v1"
    prompt_version: str = "v1"
    chunking_version: str = "v1"
    semantic_settings_version: str = "v1"


class VerifierDefinitionSettings(BaseModel):
    id: str = Field(min_length=1)
    version: str = Field(min_length=1)
    executable: str = Field(min_length=1)
    fixed_args: list[str] = Field(default_factory=list)
    allowed_args: list[str] = Field(default_factory=list)
    working_directory: Path = Path(".")
    environment_allowlist: list[str] = Field(default_factory=list)
    timeout_seconds: float = Field(default=60.0, gt=0)
    output_limit_bytes: int = Field(default=65_536, gt=0)
    applicable_globs: list[str] = Field(default_factory=list)
    required: bool = False
    success_exit_codes: list[int] = Field(default_factory=lambda: [0])


class WorkspaceSettings(BaseModel):
    sqlite: SQLiteSettings = Field(default_factory=SQLiteSettings)
    portable_state: PortableStateSettings = Field(default_factory=PortableStateSettings)
    observer: ObserverSettings = Field(default_factory=ObserverSettings)
    leases: LeaseSettings = Field(default_factory=LeaseSettings)
    operations: OperationLimitSettings = Field(default_factory=OperationLimitSettings)
    recovery: RecoverySettings = Field(default_factory=RecoverySettings)
    projection_worker: ProjectionWorkerSettings = Field(default_factory=ProjectionWorkerSettings)
    enrichment: EnrichmentSettings = Field(default_factory=EnrichmentSettings)
    brief: BriefSettings = Field(default_factory=BriefSettings)
    verifiers: list[VerifierDefinitionSettings] = Field(default_factory=list)


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


class RetrievalWeights(BaseModel):
    """Every number that can move a file's rank lives here. Inline numeric
    literals in scoring code are a defect. See katsi-scoring-spec.md §3.1.
    """

    vector: float = 0.50
    entity_per_shared: float = 0.12
    entity_cap: float = 0.30
    topic_per_shared: float = 0.08
    topic_cap: float = 0.20
    reference_out: float = 0.25
    reference_in: float = 0.15
    duplicate_of: float = -0.05
    per_extra_hop: float = -0.02
    score_min: float = 0.0
    score_max: float = 1.0


class RetrieveSettings(BaseModel):
    top_k_chunks: int = 16
    top_k_files: int = 8
    graph_expand_hops: int = 1
    vector_weight: float = 0.6  # DEPRECATED — replaced by weights.vector (evidence table)
    graph_weight: float = 0.4  # DEPRECATED — replaced by weights via evidence table
    default_context_max_tokens: int = 3000
    weights: RetrievalWeights = Field(default_factory=RetrievalWeights)
    min_edge_weight: float = 0.35


class MCPSettings(BaseModel):
    enable_answer_tool: bool = False
    agent_credential_env: str = "KATSI_AGENT_CREDENTIAL"


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
    """Top-level settings. Loaded from katsi.toml if present, env-overridable."""

    model_config = SettingsConfigDict(
        env_prefix="KATSI_",
        env_nested_delimiter="__",
        extra="ignore",
    )

    ollama: OllamaSettings = Field(default_factory=OllamaSettings)
    store: StoreSettings = Field(default_factory=StoreSettings)
    ingest: IngestSettings = Field(default_factory=IngestSettings)
    retrieve: RetrieveSettings = Field(default_factory=RetrieveSettings)
    mcp: MCPSettings = Field(default_factory=MCPSettings)
    synth: SynthSettings = Field(default_factory=SynthSettings)
    workspace: WorkspaceSettings = Field(default_factory=WorkspaceSettings)
    lease: LeaseSettings = Field(default_factory=LeaseSettings)

    @classmethod
    def load(cls, config_path: Path | None = None) -> Settings:
        """Load settings from a TOML file or default locations.

        Env vars (KATSI_*) override the file. If no path given, look for
        katsi.toml in CWD then ~/.katsi/katsi.toml.
        """
        env_settings = cls()
        if config_path is None:
            cwd_path = Path.cwd() / "katsi.toml"
            home_path = Path.home() / ".katsi" / "katsi.toml"
            config_path = (
                cwd_path if cwd_path.exists() else (home_path if home_path.exists() else None)
            )
        if config_path is None or not config_path.exists():
            return env_settings
        with open(config_path, "rb") as f:
            data = tomllib.load(f).get("katsi", {})
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
