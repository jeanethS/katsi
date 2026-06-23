"""Tests for mnemo_core.ingest.chunk."""

from mnemo_core.ingest.chunk import chunk, estimate_tokens


def test_chunk_empty_returns_empty_list() -> None:
    """Empty text produces no chunks."""
    assert chunk("f", "") == []


def test_chunk_whitespace_only_returns_empty_list() -> None:
    """Whitespace-only text produces no chunks."""
    assert chunk("f", "   \n  ") == []


def test_chunk_small_text_single_chunk() -> None:
    """Text shorter than target produces one chunk with ord=0, id='f:0'."""
    text = "Hello world"
    result = chunk("f", text)
    assert len(result) == 1
    assert result[0].id == "f:0"
    assert result[0].ordinal == 0
    assert result[0].text == text
    assert result[0].token_count >= 1


def test_chunk_long_text_multiple_chunks() -> None:
    """~6000-char text split into >=4 chunks; verify ordinals, ids, overlap."""
    # Build a 6000-char repeating pattern so we get reliable chunk sizes
    text = "The quick brown fox jumps over the lazy dog. " * 150  # ~5700 chars
    # Pad to exactly 6000
    text = text[:6000]

    result = chunk("f", text, target_tokens=512, overlap=64)
    assert len(result) >= 4

    # Ordinals are 0,1,2,...
    for i, c in enumerate(result):
        assert c.ordinal == i
        assert c.id == f"f:{i}"

    # Each token_count <= target_tokens + small slack
    # (target_tokens=512, window=2048 chars → 512 tokens max, but approximations
    #  may leave partial chunk slightly smaller)
    for c in result:
        assert c.token_count <= 520

    # Consecutive chunks overlap: chunk N should appear within chunk N+1
    for i in range(len(result) - 1):
        # chunk N+1 should contain the tail of chunk N
        tail_of_prev = result[i].text[-256:]  # ~64 tokens worth
        assert tail_of_prev in result[i + 1].text


def test_chunk_ids_and_ordinals_are_deterministic() -> None:
    """Two calls with same args produce identical chunk lists."""
    text = "A " * 2000  # 4000 chars
    a = chunk("f", text, target_tokens=512, overlap=64)
    b = chunk("f", text, target_tokens=512, overlap=64)
    assert a == b


def test_chunk_token_count_positive() -> None:
    """Every emitted chunk has token_count >= 1."""
    text = "A " * 2000
    result = chunk("f", text, target_tokens=512, overlap=64)
    assert len(result) > 0
    for c in result:
        assert c.token_count >= 1


def test_estimate_tokens_basic() -> None:
    """estimate_tokens edge cases."""
    assert estimate_tokens("") == 0
    assert estimate_tokens("hello") == 1
    assert estimate_tokens("x" * 100) == 25
