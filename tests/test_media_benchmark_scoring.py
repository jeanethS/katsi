"""Tests for accuracy scoring system."""

from __future__ import annotations

from benchmarks.media.contracts import AccuracyMetric
from benchmarks.media.scoring import (
    character_accuracy,
    seq2seq_f1,
    text_iou,
    word_accuracy,
    word_error_rate,
)
from benchmarks.media.scoring import (
    character_error_rate as cer,
)


class TestCharacterAccuracy:
    """Tests for character-level accuracy metric."""

    def test_perfect_match(self):
        """Test that perfect match returns 1.0."""
        result = character_accuracy("hello world", "hello world")
        assert result.metric == AccuracyMetric.CHARACTER_ACCURACY
        assert result.value == 1.0
        assert result.higher_is_better is True

    def test_completely_different(self):
        """Test that completely different strings return 0.0."""
        result = character_accuracy("abc", "xyz")
        assert result.metric == AccuracyMetric.CHARACTER_ACCURACY
        assert result.value == 0.0
        assert result.higher_is_better is True

    def test_partial_match(self):
        """Test partial match returns expected value."""
        result = character_accuracy("hello", "hallo")
        assert result.metric == AccuracyMetric.CHARACTER_ACCURACY
        # 1 substitution out of 5 characters = 0.8 accuracy
        assert 0.7 <= result.value <= 0.9

    def test_empty_strings(self):
        """Test empty strings return 1.0."""
        result = character_accuracy("", "")
        assert result.value == 1.0

    def test_one_empty_string(self):
        """Test one empty string returns 0.0."""
        result = character_accuracy("test", "")
        assert result.value == 0.0


class TestWordAccuracy:
    """Tests for word-level accuracy metric."""

    def test_perfect_match(self):
        """Test perfect match returns 1.0."""
        result = word_accuracy("hello world", "hello world")
        assert result.metric == AccuracyMetric.WORD_ACCURACY
        assert result.value == 1.0

    def test_no_matching_words(self):
        """Test no matching words returns 0.0."""
        result = word_accuracy("hello world", "foo bar")
        assert result.value == 0.0

    def test_partial_match(self):
        """Test partial word match."""
        result = word_accuracy("hello world test", "hello moon test")
        # 2 out of 3 words match
        assert 0.6 <= result.value <= 0.7

    def test_empty_true_text(self):
        """Test empty true text returns 1.0."""
        result = word_accuracy("any text", "")
        assert result.value == 1.0

    def test_punctuation_handling(self):
        """Test punctuation is handled correctly."""
        result = word_accuracy("Hello, world!", "hello world")
        # Should match after punctuation removal
        assert result.value == 1.0


class TestTextIoU:
    """Tests for text IoU metric."""

    def test_perfect_overlap(self):
        """Test perfect box overlap returns 1.0."""
        text1 = "0,0,100,100;text1"
        text2 = "0,0,100,100;text1"
        result = text_iou(text1, text2)
        assert result.metric == AccuracyMetric.TEXT_IOU
        assert result.value == 1.0

    def test_no_overlap(self):
        """Test no overlap returns 0.0."""
        text1 = "0,0,100,100;text1"
        text2 = "200,200,300,300;text2"
        result = text_iou(text1, text2)
        assert result.value == 0.0

    def test_partial_overlap(self):
        """Test partial overlap returns intermediate value."""
        text1 = "0,0,100,100;text1"
        text2 = "50,50,150,150;text2"
        result = text_iou(text1, text2)
        assert 0.0 < result.value < 1.0

    def test_empty_boxes(self):
        """Test empty boxes return 1.0."""
        result = text_iou("", "")
        assert result.value == 1.0

    def test_malformed_boxes(self):
        """Test malformed boxes are skipped."""
        text1 = "0,0,100,100;valid text"
        text2 = "invalid;malformed box\n0,0,100,100;valid text"
        result = text_iou(text1, text2)
        # Should handle malformed lines gracefully
        assert 0.0 <= result.value <= 1.0


class TestWordErrorRate:
    """Tests for word error rate metric."""

    def test_perfect_match(self):
        """Test perfect match returns 0.0."""
        result = word_error_rate("hello world", "hello world")
        assert result.metric == AccuracyMetric.WORD_ERROR_RATE
        assert result.value == 0.0
        assert result.higher_is_better is False

    def test_completely_different(self):
        """Test completely different words returns high error rate."""
        result = word_error_rate("hello world", "foo bar")
        assert result.metric == AccuracyMetric.WORD_ERROR_RATE
        # 2 substitutions out of 2 words = 1.0 WER
        assert result.value == 1.0

    def test_partial_errors(self):
        """Test partial word errors."""
        result = word_error_rate("hello world test", "hello moon test")
        # 1 substitution out of 3 words = 0.33 WER
        assert 0.3 <= result.value <= 0.4

    def test_empty_true_text(self):
        """Test empty true text returns 0.0."""
        result = word_error_rate("any text", "")
        assert result.value == 0.0


class TestCharacterErrorRate:
    """Tests for character error rate metric."""

    def test_perfect_match(self):
        """Test perfect match returns 0.0."""
        result = cer("hello", "hello")
        assert result.metric == AccuracyMetric.CHARACTER_ERROR_RATE
        assert result.value == 0.0
        assert result.higher_is_better is False

    def test_completely_different(self):
        """Test completely different characters returns 1.0."""
        result = cer("abc", "xyz")
        assert result.metric == AccuracyMetric.CHARACTER_ERROR_RATE
        assert result.value == 1.0

    def test_partial_errors(self):
        """Test partial character errors."""
        result = cer("hello", "hallo")
        # 1 substitution out of 5 characters = 0.2 CER
        assert 0.1 <= result.value <= 0.3

    def test_empty_strings(self):
        """Test empty strings return 0.0."""
        result = cer("", "")
        assert result.value == 0.0


class TestSeq2SeqF1:
    """Tests for sequence-to-sequence F1 metric."""

    def test_perfect_match(self):
        """Test perfect match returns 1.0."""
        result = seq2seq_f1("hello world test", "hello world test")
        assert result.metric == AccuracyMetric.SEQ2SEQ_F1
        assert result.value == 1.0
        assert result.higher_is_better is True

    def test_no_overlap(self):
        """Test no token overlap returns 0.0."""
        result = seq2seq_f1("hello world", "foo bar")
        assert result.value == 0.0

    def test_partial_overlap(self):
        """Test partial token overlap."""
        result = seq2seq_f1("hello world test", "hello moon test")
        # Precision: 2/3, Recall: 2/3, F1: 2/3
        assert 0.6 <= result.value <= 0.7

    def test_empty_true_text(self):
        """Test empty true text returns 1.0."""
        result = seq2seq_f1("any text", "")
        assert result.value == 1.0

    def test_empty_pred_text(self):
        """Test empty prediction returns 0.0."""
        result = seq2seq_f1("", "hello world")
        assert result.value == 0.0


class TestHelperFunctions:
    """Tests for helper functions used in scoring."""

    def test_levenshtein_distance(self):
        """Test Levenshtein distance calculation."""
        from benchmarks.media.scoring import levenshtein_distance

        assert levenshtein_distance("", "") == 0
        assert levenshtein_distance("a", "a") == 0
        assert levenshtein_distance("abc", "abc") == 0
        assert levenshtein_distance("abc", "abx") == 1
        assert levenshtein_distance("abc", "xyz") == 3
        assert levenshtein_distance("kitten", "sitting") == 3

    def test_word_tokenization(self):
        """Test word tokenization."""
        from benchmarks.media.scoring import tokenize_words

        assert tokenize_words("hello world") == ["hello", "world"]
        assert tokenize_words("  hello   world  ") == ["hello", "world"]
        assert tokenize_words("hello, world!") == ["hello", "world"]
        assert tokenize_words("") == []

    def test_box_parsing(self):
        """Test bounding box parsing."""
        from benchmarks.media.scoring import parse_bounding_boxes

        text = "0,0,100,100;text1\n200,200,300,300;text2"
        boxes = parse_bounding_boxes(text)

        assert len(boxes) == 2
        assert boxes[0] == (0, 0, 100, 100, "text1")
        assert boxes[1] == (200, 200, 300, 300, "text2")

    def test_box_iou_calculation(self):
        """Test box IoU calculation."""
        from benchmarks.media.scoring import calculate_box_iou

        box1 = (0, 0, 100, 100, "text")
        box2 = (0, 0, 100, 100, "text")
        iou = calculate_box_iou(box1, box2)
        assert iou == 1.0

        box3 = (0, 0, 100, 100, "text")
        box4 = (200, 200, 300, 300, "text")
        iou = calculate_box_iou(box3, box4)
        assert iou == 0.0


class TestEdgeCases:
    """Tests for edge cases and special inputs."""

    def test_unicode_characters(self):
        """Test scoring with unicode characters."""
        result = character_accuracy("héllo wørld", "héllo wørld")
        assert result.value == 1.0

    def test_whitespace_variations(self):
        """Test scoring with different whitespace."""
        result = word_accuracy("hello   world", "hello world")
        assert result.value == 1.0

    def test_mixed_case(self):
        """Test scoring with mixed case."""
        result = word_accuracy("Hello World", "hello world")
        # Case-sensitive, so should be 0.0
        assert result.value == 0.0
