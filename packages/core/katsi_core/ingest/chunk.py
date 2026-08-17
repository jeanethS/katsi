"""Text chunking with approximate token counting.

Token estimation uses non-whitespace density: count non-whitespace chars // 3.
This excludes indentation and blank lines from the estimate, giving better
results for code and padded text.
"""

from __future__ import annotations

from katsi_core.models import Chunk


def estimate_tokens(text: str) -> int:
    """Cheap token estimate using non-whitespace density.

    Counts non-whitespace characters // 3. Rationale: excludes indentation
    and blank lines from the estimate, giving better results for code and
    padded text (cAST, arXiv 2506.15655).

    Always >= 1 for non-empty text. Returns 0 for empty text.
    """
    if not text:
        return 0
    non_ws_count = sum(1 for c in text if not c.isspace())
    return max(1, non_ws_count // 3)


def _split_recursively(
    text: str,
    target_tokens: int,
    separators: list[str],
) -> list[str]:
    """Split text recursively using separator hierarchy.

    Args:
        text: Text to split.
        target_tokens: Target chunk size in tokens.
        separators: List of separators to try, in priority order.

    Returns:
        List of text pieces, each within target_tokens except unbreakable atoms.
    """
    # Base case: if text fits, return it as single piece
    if estimate_tokens(text) <= target_tokens:
        return [text]

    # Try each separator in priority order
    for sep_idx, separator in enumerate(separators):
        # Handle empty separator as special case (character-level splitting)
        if separator == "":
            # Perform hard character slice
            pieces = []
            remaining = text
            while remaining:
                # Calculate target in characters (approximate)
                target_chars = target_tokens * 3  # Convert back to char estimate
                if len(remaining) <= target_chars:
                    pieces.append(remaining)
                    break
                else:
                    pieces.append(remaining[:target_chars])
                    remaining = remaining[target_chars:]
            return pieces

        if separator not in text:
            continue

        # Split on this separator
        pieces = text.split(separator)

        # Greedily merge adjacent pieces to stay within target
        merged_pieces: list[str] = []
        current_group = pieces[0] if pieces else ""

        for piece in pieces[1:]:
            test_group = current_group + separator + piece
            if estimate_tokens(test_group) <= target_tokens:
                current_group = test_group
            else:
                # Current group is full, start a new one
                if current_group:
                    merged_pieces.append(current_group)
                current_group = piece

        if current_group:
            merged_pieces.append(current_group)

        # Recurse into any pieces still over target using remaining separators
        remaining_seps = separators[sep_idx + 1:]
        final_pieces: list[str] = []

        for piece in merged_pieces:
            if estimate_tokens(piece) <= target_tokens:
                final_pieces.append(piece)
            elif remaining_seps:
                # Recurse with remaining separators
                final_pieces.extend(_split_recursively(
                    piece, target_tokens, remaining_seps
                ))
            else:
                # No more separators - this piece is an unbreakable atom
                final_pieces.append(piece)

        return final_pieces

    # No separator found and no empty separator - return as single piece
    return [text]


def _apply_overlap(pieces: list[str], overlap: int) -> list[str]:
    """Apply overlap as a post-pass over pieces.

    Prepends the trailing overlap tokens' worth of text from piece N to piece N+1.
    """
    if len(pieces) <= 1:
        return pieces

    overlapped_pieces = [pieces[0]]

    for i in range(1, len(pieces)):
        prev_piece = pieces[i - 1]
        current_piece = pieces[i]

        # Extract overlap tokens from tail of previous piece
        # Count non-whitespace characters to find overlap boundary
        overlap_chars_needed = overlap * 3  # Approximate chars per token
        overlap_text = ""
        ws_count = 0

        # Work backwards from end of prev_piece to find overlap boundary
        for c in reversed(prev_piece):
            overlap_text = c + overlap_text
            if not c.isspace():
                ws_count += 1
                if ws_count >= overlap:
                    break

        overlapped_pieces.append(overlap_text + current_piece)

    return overlapped_pieces


def chunk(
    file_id: str,
    text: str,
    *,
    target_tokens: int = 512,
    overlap: int = 64,
) -> list[Chunk]:
    """Split *text* into ~*target_tokens*-sized chunks with ~*overlap*-token overlap.

    Parameters
    ----------
    file_id:
        Stable identifier for the source file. Used as chunk id prefix.
    text:
        Source text to split.
    target_tokens:
        Approximate target size per chunk in tokens (non-whitespace density).
    overlap:
        Approximate overlap between consecutive chunks in tokens.

    Notes
    -----
    - ``target_tokens`` and ``overlap`` use the non-whitespace density heuristic:
      ``sum(1 for c in text if not c.isspace()) // 3``.
    - Chunking uses recursive separator splitting with hierarchy:
      ``["\\n\\n", "\\n", ". ", " ", ""]``.
    - Overlap is applied as a post-pass that prepends trailing text from chunk N
      to chunk N+1.
    - Each chunk's id is ``f"{file_id}:{ordinal}"`` with ordinal starting at 0.
    - ``token_count`` is ``estimate_tokens(chunk_text)`` (always >= 1 if non-empty).
    - If text is empty or whitespace-only, returns [].
    - If the entire text is shorter than *target_tokens*, returns ONE chunk
      containing the whole text.
    """
    if not text or not text.strip():
        return []

    # Check if entire text fits in one chunk
    if estimate_tokens(text) <= target_tokens:
        return [
            Chunk(
                id=f"{file_id}:0",
                file_id=file_id,
                ordinal=0,
                text=text,
                token_count=estimate_tokens(text),
            )
        ]

    # Split recursively using separator hierarchy
    separators = ["\n\n", "\n", ". ", " ", ""]
    pieces = _split_recursively(text, target_tokens, separators)

    # Apply overlap as post-pass
    overlapped_pieces = _apply_overlap(pieces, overlap)

    # Build chunk objects
    chunks: list[Chunk] = []
    for ordinal, piece_text in enumerate(overlapped_pieces):
        chunks.append(
            Chunk(
                id=f"{file_id}:{ordinal}",
                file_id=file_id,
                ordinal=ordinal,
                text=piece_text,
                token_count=estimate_tokens(piece_text),
            )
        )

    return chunks
