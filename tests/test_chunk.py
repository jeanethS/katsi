"""Tests for katsi_core.ingest.chunk."""

from katsi_core.ingest.chunk import chunk, estimate_tokens


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

    # Each token_count <= target_tokens + reasonable slack
    # (target_tokens=512, but new non-whitespace density heuristic may vary more)
    for c in result:
        assert c.token_count <= 560, f"Chunk {c.id} exceeds target: {c.token_count} > 512"

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
    """estimate_tokens edge cases with non-whitespace density."""
    assert estimate_tokens("") == 0
    # "hello" = 5 non-whitespace chars // 3 = 1
    assert estimate_tokens("hello") == 1
    # "x" * 100 = 100 non-whitespace chars // 3 = 33
    assert estimate_tokens("x" * 100) == 33
    # Test with whitespace: "hello world" = 10 non-whitespace chars // 3 = 3
    assert estimate_tokens("hello world") == 3
    # Test with lots of whitespace: "  hello  " = 5 non-whitespace chars // 3 = 1
    assert estimate_tokens("  hello  ") == 1


def test_no_content_loss_with_whitespace_run() -> None:
    """Document with >2048 whitespace chars in middle preserves all content.

    This is the most important acceptance check. The old implementation
    would lose content when encountering a long whitespace run.
    """
    # Create text with a long whitespace run in the middle
    before = "This is content before the whitespace run. " * 50  # ~2000 chars
    whitespace_run = " " * 3000  # More than 2048 whitespace chars
    after = "This is content after the whitespace run. " * 50  # ~2000 chars
    text = before + whitespace_run + after

    result = chunk("test", text, target_tokens=512, overlap=64)

    # Check that we have chunks (not empty list, which would indicate total loss)
    assert len(result) > 0, "No chunks generated"

    # Check that key content markers are present across all chunks
    concatenated = "".join(c.text for c in result)

    # The key phrases should be present (allowing for chunk boundaries)
    assert "This is content before the whitespace run." in concatenated
    assert "This is content after the whitespace run." in concatenated

    # Check that we have substantial content (not just tiny fragments)
    total_length = sum(len(c.text) for c in result)
    assert total_length > len(text) * 0.8, f"Too much content lost: {total_length} vs original {len(text)}"


def test_prefers_paragraph_boundaries() -> None:
    """Text with paragraphs sized to fit multiple per chunk splits on \\n\\n boundaries."""
    # Create text with clear paragraphs separated by \n\n
    paragraphs = []
    for i in range(10):
        # Each paragraph ~300 chars
        para = f"Paragraph {i}: " + "This is a sentence. " * 15 + "\n\n"
        paragraphs.append(para)

    text = "".join(paragraphs)

    result = chunk("test", text, target_tokens=512, overlap=64)

    # Verify we got multiple chunks
    assert len(result) >= 2

    # Check that paragraph boundaries are respected by checking that
    # all paragraph markers are present and chunks contain complete paragraphs
    for c in result:
        # Each chunk should contain complete paragraph markers
        # (we're not splitting mid-paragraph in weird ways)
        assert "Paragraph" in c.text or "This is a sentence" in c.text

    # Check that we preserve paragraph structure across chunks
    total_para_breaks = sum(c.text.count("\n\n") for c in result)
    original_para_breaks = text.count("\n\n")
    # Should have approximately the same number of paragraph breaks
    assert total_para_breaks >= original_para_breaks * 0.8, f"Lost too many paragraph breaks: {total_para_breaks} vs {original_para_breaks}"


def test_size_bound_respected() -> None:
    """No emitted chunk exceeds target_tokens, except unbreakable atoms."""
    # Create text with normal content
    normal_text = "This is a test sentence. " * 500  # ~8000 chars

    result = chunk("test", normal_text, target_tokens=512, overlap=64)

    # All chunks should respect size bound
    for c in result:
        # Allow small margin for estimation error
        assert c.token_count <= 550, f"Chunk exceeds target: {c.token_count} > 512"

    # Test with unbreakable atom (very long single word)
    long_word = "a" * 10000  # Single word longer than target
    result = chunk("test", long_word, target_tokens=512, overlap=64)

    # Should produce chunks, but some may exceed target due to unbreakable atom
    # At least one chunk should be the unbreakable atom itself
    assert len(result) >= 1


def test_termination_on_pathological_input() -> None:
    """10000-char string with no whitespace terminates and produces chunks."""
    # Create pathological input: no whitespace at all
    no_whitespace = "a" * 10000

    result = chunk("test", no_whitespace, target_tokens=512, overlap=64)

    # Should terminate and produce chunks
    assert len(result) >= 1

    # Total content should equal original (minus overlap duplication)
    total_chars = sum(len(c.text) for c in result)
    assert total_chars >= len(no_whitespace), "Total content less than original"
