"""Media adapter availability probes.

Detects and probes OCR, transcription, and captioning adapters for installation
and basic functionality. All probes run via subprocess to avoid importing media
runtimes directly.
"""

from __future__ import annotations

import subprocess
import sys

from benchmarks.media.contracts import CandidateAdapter, ProbeResult

_PROBE_SCRIPT = """
import sys
try:
    import {module} as _mod
except Exception as e:
    print(f'ERROR: {{e}}', file=sys.stderr)
    sys.exit(1)

try:
    import importlib.metadata
    print(importlib.metadata.version('{module}'))
except Exception:
    print(getattr(_mod, '__version__', 'stdlib'))
"""


def _probe_availability(adapter: CandidateAdapter) -> ProbeResult:
    """Check whether `adapter.name` can be imported as a Python module.

    A single subprocess both imports the module and reports its version:
    `importlib.metadata.version()` alone (the prior approach) only works for
    third-party packages with installed dist-info metadata, so it always
    fails for stdlib modules and any module without package metadata even
    though the module itself imports fine. Importing first, then treating
    metadata lookup as a best-effort version hint, fixes that.
    """
    try:
        script = _PROBE_SCRIPT.format(module=adapter.name)
        result = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            shell=False,
            timeout=10,
        )

        if result.returncode != 0:
            return ProbeResult(
                available=False,
                adapter=adapter,
                error_message=result.stderr.strip(),
                reason=f"Module import failed: {result.stderr.strip()}",
            )

        return ProbeResult(
            available=True,
            adapter=adapter,
            version_detected=result.stdout.strip(),
        )

    except subprocess.TimeoutExpired:
        return ProbeResult(
            available=False,
            adapter=adapter,
            error_message="Probe timed out after 10 seconds",
            reason="Probe operation timed out",
        )
    except Exception as e:
        return ProbeResult(
            available=False,
            adapter=adapter,
            error_message=str(e),
            reason=f"Probe failed with exception: {e}",
        )


def probe_ocr_availability(adapter: CandidateAdapter) -> ProbeResult:
    """Check if an OCR adapter is installed and functional."""
    return _probe_availability(adapter)


def probe_transcription_availability(adapter: CandidateAdapter) -> ProbeResult:
    """Check if a transcription adapter is installed and functional."""
    return _probe_availability(adapter)


def probe_captioning_availability(adapter: CandidateAdapter) -> ProbeResult:
    """Check if a captioning adapter is installed and functional."""
    return _probe_availability(adapter)
