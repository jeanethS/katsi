from __future__ import annotations

from typing import TYPE_CHECKING

from mnemo_core.config import Settings

if TYPE_CHECKING:
    import ollama


class EmbedClient:
    """Ollama embeddings wrapper. Reads model/host from settings."""

    def __init__(
        self,
        settings: Settings | None = None,
        client: ollama.Client | None = None,
    ) -> None:
        """Hold settings; if *client* is ``None``, lazily build ``ollama.Client`` on
        first call to *embed* (deferred so that construction without a running
        Ollama server is safe — important for tests + import-time).
        """
        self._settings = settings or Settings()
        self._client = client
        self._dim: int | None = None

    def _get_client(self) -> ollama.Client:
        if self._client is None:
            import ollama

            self._client = ollama.Client(
                host=self._settings.ollama.host,
                timeout=self._settings.ollama.timeout,
            )
        return self._client

    def embed(self, texts: list[str]) -> list[list[float]]:
        """Batch-embed *texts* against ``Settings.ollama.embed_model``.

        Returns ``list[list[float]]`` — same length as *texts*.
        If *texts* is empty, return ``[]`` (no API call).
        """
        if not texts:
            return []
        resp = self._get_client().embed(
            model=self._settings.ollama.embed_model,
            input=texts,
        )
        # ollama 0.6.x returns object with .embeddings / ["embeddings"]
        return list(resp["embeddings"])

    @property
    def dim(self) -> int:
        """Embedding dimension.

        Resolved lazily by embedding a single probe text (``'hello'``) and
        reading ``len(vector)``.  Cached after first call.
        """
        if self._dim is None:
            vector = self.embed(["hello"])[0]
            self._dim = len(vector)
        return self._dim
