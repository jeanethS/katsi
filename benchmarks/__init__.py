"""Katsi media adapter benchmark suite.

This package provides local benchmarking infrastructure for OCR, transcription,
and captioning adapters. Benchmarks run entirely on your hardware - no data
leaves your machine, no API calls are made.

Usage:
    python -m benchmarks.media.cli --capability ocr --adapter tesseract
"""

from __future__ import annotations
