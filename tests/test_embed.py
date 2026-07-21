"""Tests for EmbedClient — all use a fake in-memory ollama client."""

from katsi_core.clients.embed import EmbedClient


class _FakeEmbedResp:
    def __init__(self, vectors):
        self.embeddings = vectors

    def __getitem__(self, k):
        if k == "embeddings":
            return self.embeddings
        raise KeyError(k)


class _FakeOllama:
    def __init__(self, dim: int = 1024):
        self.dim = dim
        self.calls: list[list[str]] = []

    def embed(self, model, input):
        self.calls.append(list(input))
        return _FakeEmbedResp(
            [[0.01 * (i + 1)] * self.dim for i in range(len(input))]
        )


def test_embed_batches_returns_vectors():
    fake = _FakeOllama(dim=8)
    c = EmbedClient(client=fake)
    out = c.embed(["hello", "world"])
    assert len(out) == 2
    assert len(out[0]) == 8
    assert fake.calls == [["hello", "world"]]


def test_embed_empty_returns_empty():
    c = EmbedClient(client=_FakeOllama())
    assert c.embed([]) == []


def test_dim_is_cached_after_first_probe():
    fake = _FakeOllama(dim=16)
    c = EmbedClient(client=fake)
    d1 = c.dim
    d2 = c.dim
    assert d1 == 16 == d2
    assert len(fake.calls) == 1
