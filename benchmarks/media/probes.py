"""Media adapter availability probes.

Detects and probes OCR, transcription, and captioning adapters for installation
and basic functionality. All probes run via subprocess to avoid importing media
runtimes directly.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from benchmarks.media.contracts import CandidateAdapter, CapabilityKind, ProbeResult


def probe_ocr_availability(adapter: CandidateAdapter) -> ProbeResult:
    """Check if an OCR adapter is installed and functional."""
    try:
        # Try importing the adapter's package
        import_script = f"""
import sys
try:
    import importlib.metadata
    version = importlib.metadata.version('{adapter.name}')
    print(f'{{version}}')
except Exception as e:
    print(f'ERROR: {{e}}', file=sys.stderr)
    sys.exit(1)
"""

        result = subprocess.run(
            [sys.executable, "-c", import_script],
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
                reason=f"Package import failed: {result.stderr.strip()}",
            )

        version_detected = result.stdout.strip()

        # Try a lightweight API call if available
        test_script = f"""
import sys
try:
    import {adapter.name}
    if hasattr({adapter.name}, '__version__'):
        print({adapter.name}.__version__)
    else:
        print('version_unknown')
except Exception as e:
    print(f'ERROR: {{e}}', file=sys.stderr)
    sys.exit(1)
"""

        test_result = subprocess.run(
            [sys.executable, "-c", test_script],
            capture_output=True,
            text=True,
            shell=False,
            timeout=10,
        )

        if test_result.returncode != 0:
            return ProbeResult(
                available=False,
                adapter=adapter,
                version_detected=version_detected,
                error_message=test_result.stderr.strip(),
                reason=f"API test failed: {test_result.stderr.strip()}",
            )

        return ProbeResult(
            available=True,
            adapter=adapter,
            version_detected=version_detected,
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


def probe_transcription_availability(adapter: CandidateAdapter) -> ProbeResult:
    """Check if a transcription adapter is installed and functional."""
    try:
        # Try importing the adapter's package
        import_script = f"""
import sys
try:
    import importlib.metadata
    version = importlib.metadata.version('{adapter.name}')
    print(f'{{version}}')
except Exception as e:
    print(f'ERROR: {{e}}', file=sys.stderr)
    sys.exit(1)
"""

        result = subprocess.run(
            [sys.executable, "-c", import_script],
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
                reason=f"Package import failed: {result.stderr.strip()}",
            )

        version_detected = result.stdout.strip()

        # Try a lightweight API call if available
        test_script = f"""
import sys
try:
    import {adapter.name}
    if hasattr({adapter.name}, '__version__'):
        print({adapter.name}.__version__)
    else:
        print('version_unknown')
except Exception as e:
    print(f'ERROR: {{e}}', file=sys.stderr)
    sys.exit(1)
"""

        test_result = subprocess.run(
            [sys.executable, "-c", test_script],
            capture_output=True,
            text=True,
            shell=False,
            timeout=10,
        )

        if test_result.returncode != 0:
            return ProbeResult(
                available=False,
                adapter=adapter,
                version_detected=version_detected,
                error_message=test_result.stderr.strip(),
                reason=f"API test failed: {test_result.stderr.strip()}",
            )

        return ProbeResult(
            available=True,
            adapter=adapter,
            version_detected=version_detected,
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


def probe_captioning_availability(adapter: CandidateAdapter) -> ProbeResult:
    """Check if a captioning adapter is installed and functional."""
    try:
        # Try importing the adapter's package
        import_script = f"""
import sys
try:
    import importlib.metadata
    version = importlib.metadata.version('{adapter.name}')
    print(f'{{version}}')
except Exception as e:
    print(f'ERROR: {{e}}', file=sys.stderr)
    sys.exit(1)
"""

        result = subprocess.run(
            [sys.executable, "-c", import_script],
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
                reason=f"Package import failed: {result.stderr.strip()}",
            )

        version_detected = result.stdout.strip()

        # Try a lightweight API call if available
        test_script = f"""
import sys
try:
    import {adapter.name}
    if hasattr({adapter.name}, '__version__'):
        print({adapter.name}.__version__)
    else:
        print('version_unknown')
except Exception as e:
    print(f'ERROR: {{e}}', file=sys.stderr)
    sys.exit(1)
"""

        test_result = subprocess.run(
            [sys.executable, "-c", test_script],
            capture_output=True,
            text=True,
            shell=False,
            timeout=10,
        )

        if test_result.returncode != 0:
            return ProbeResult(
                available=False,
                adapter=adapter,
                version_detected=version_detected,
                error_message=test_result.stderr.strip(),
                reason=f"API test failed: {test_result.stderr.strip()}",
            )

        return ProbeResult(
            available=True,
            adapter=adapter,
            version_detected=version_detected,
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
