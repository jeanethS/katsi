"""Accuracy scoring for media adapter outputs.

Implements OCR, transcription, and captioning metrics using only standard
library (no numpy, jiwer, or external packages).
"""

from __future__ import annotations

import re

from benchmarks.media.contracts import AccuracyMetric, AccuracyScore


def character_accuracy(pred_text: str, true_text: str) -> AccuracyScore:
    """Character-level accuracy based on edit distance."""
    distance = levenshtein_distance(pred_text, true_text)
    max_len = max(len(pred_text), len(true_text))

    if max_len == 0:
        return AccuracyScore(metric=AccuracyMetric.CHARACTER_ACCURACY, value=1.0)

    accuracy = 1.0 - (distance / max_len)
    return AccuracyScore(
        metric=AccuracyMetric.CHARACTER_ACCURACY, value=accuracy, higher_is_better=True
    )


def word_accuracy(pred_text: str, true_text: str) -> AccuracyScore:
    """Word-level accuracy (exact word matches)."""
    pred_words = tokenize_words(pred_text)
    true_words = tokenize_words(true_text)

    if not true_words:
        return AccuracyScore(metric=AccuracyMetric.WORD_ACCURACY, value=1.0)

    if not pred_words:
        return AccuracyScore(metric=AccuracyMetric.WORD_ACCURACY, value=0.0)

    # Count matches (both words must exist and be in same position)
    matches = sum(
        1
        for p, t in zip(pred_words, true_words, strict=False)
        if p == t and p in true_words and t in pred_words
    )
    accuracy = matches / max(len(pred_words), len(true_words))

    return AccuracyScore(metric=AccuracyMetric.WORD_ACCURACY, value=accuracy, higher_is_better=True)


def text_iou(pred_text: str, true_text: str) -> AccuracyScore:
    """Text IoU for spatial OCR (bounding box overlap)."""
    # Parse bounding boxes (format: "x1,y1,x2,y2;text")
    pred_boxes = parse_bounding_boxes(pred_text)
    true_boxes = parse_bounding_boxes(true_text)

    if not true_boxes:
        return AccuracyScore(metric=AccuracyMetric.TEXT_IOU, value=1.0)

    if not pred_boxes:
        return AccuracyScore(metric=AccuracyMetric.TEXT_IOU, value=0.0)

    # Calculate IoU for each true box with best matching pred box
    ious = []
    for true_box in true_boxes:
        best_iou = 0.0
        for pred_box in pred_boxes:
            iou = calculate_box_iou(true_box, pred_box)
            best_iou = max(best_iou, iou)
        ious.append(best_iou)

    # Average IoU across all true boxes
    avg_iou = sum(ious) / len(ious) if ious else 0.0
    return AccuracyScore(metric=AccuracyMetric.TEXT_IOU, value=avg_iou, higher_is_better=True)


def word_error_rate(pred_text: str, true_text: str) -> AccuracyScore:
    """WER: (substitutions + deletions + insertions) / true_word_count."""
    pred_words = tokenize_words(pred_text)
    true_words = tokenize_words(true_text)

    if not true_words:
        return AccuracyScore(
            metric=AccuracyMetric.WORD_ERROR_RATE, value=0.0, higher_is_better=False
        )

    # Calculate edit distance at word level
    distance = levenshtein_distance(pred_words, true_words)
    wer = distance / len(true_words)

    return AccuracyScore(metric=AccuracyMetric.WORD_ERROR_RATE, value=wer, higher_is_better=False)


def character_error_rate(pred_text: str, true_text: str) -> AccuracyScore:
    """CER: character-level edit distance rate."""
    distance = levenshtein_distance(pred_text, true_text)
    max_len = max(len(pred_text), len(true_text))

    if max_len == 0:
        return AccuracyScore(
            metric=AccuracyMetric.CHARACTER_ERROR_RATE, value=0.0, higher_is_better=False
        )

    cer = distance / max_len
    return AccuracyScore(
        metric=AccuracyMetric.CHARACTER_ERROR_RATE, value=cer, higher_is_better=False
    )


def seq2seq_f1(pred_text: str, true_text: str) -> AccuracyScore:
    """F1 score for sequence-to-sequence (token overlap)."""
    pred_tokens = set(tokenize_words(pred_text))
    true_tokens = set(tokenize_words(true_text))

    if not true_tokens:
        return AccuracyScore(metric=AccuracyMetric.SEQ2SEQ_F1, value=1.0, higher_is_better=True)

    if not pred_tokens:
        return AccuracyScore(metric=AccuracyMetric.SEQ2SEQ_F1, value=0.0, higher_is_better=True)

    # Calculate precision, recall, F1
    intersection = pred_tokens & true_tokens
    precision = len(intersection) / len(pred_tokens) if pred_tokens else 0.0
    recall = len(intersection) / len(true_tokens) if true_tokens else 0.0

    f1 = 0.0 if precision + recall == 0 else 2 * (precision * recall) / (precision + recall)

    return AccuracyScore(metric=AccuracyMetric.SEQ2SEQ_F1, value=f1, higher_is_better=True)


# Helper functions


def levenshtein_distance(s1: str | list, s2: str | list) -> int:
    """Calculate Levenshtein edit distance between two strings or lists."""
    if len(s1) < len(s2):
        return levenshtein_distance(s2, s1)

    if len(s2) == 0:
        return len(s1)

    previous_row = list(range(len(s2) + 1))
    for i, c1 in enumerate(s1):
        current_row = [i + 1]
        for j, c2 in enumerate(s2):
            # Calculate costs
            insertions = previous_row[j + 1] + 1
            deletions = current_row[j] + 1
            substitutions = previous_row[j] + (c1 != c2)

            current_row.append(min(insertions, deletions, substitutions))
        previous_row = current_row

    return previous_row[-1]


def tokenize_words(text: str) -> list[str]:
    """Split text into words, handling whitespace and punctuation."""
    # Split on whitespace and strip punctuation
    words = re.split(r"\s+", text.strip())
    return [w.strip(".,!?;:\"'-()[]{}") for w in words if w.strip()]


def parse_bounding_boxes(text: str) -> list[tuple[int, int, int, int, str]]:
    """Parse bounding boxes from text format 'x1,y1,x2,y2;text'."""
    boxes = []
    for line in text.strip().split("\n"):
        if not line.strip():
            continue
        try:
            parts = line.split(";")
            if len(parts) >= 2:
                coords = parts[0].strip()
                content = ";".join(parts[1:]).strip()

                coord_parts = coords.split(",")
                if len(coord_parts) == 4:
                    x1, y1, x2, y2 = map(int, coord_parts)
                    boxes.append((x1, y1, x2, y2, content))
        except (ValueError, IndexError):
            continue
    return boxes


def calculate_box_iou(
    box1: tuple[int, int, int, int, str], box2: tuple[int, int, int, int, str]
) -> float:
    """Calculate IoU between two bounding boxes."""
    x1_1, y1_1, x2_1, y2_1, _ = box1
    x1_2, y1_2, x2_2, y2_2, _ = box2

    # Calculate intersection
    x1_i = max(x1_1, x1_2)
    y1_i = max(y1_1, y1_2)
    x2_i = min(x2_1, x2_2)
    y2_i = min(y2_1, y2_2)

    if x2_i <= x1_i or y2_i <= y1_i:
        return 0.0

    intersection_area = (x2_i - x1_i) * (y2_i - y1_i)

    # Calculate union
    area1 = (x2_1 - x1_1) * (y2_1 - y1_1)
    area2 = (x2_2 - x1_2) * (y2_2 - y1_2)
    union_area = area1 + area2 - intersection_area

    if union_area == 0:
        return 0.0

    return intersection_area / union_area
