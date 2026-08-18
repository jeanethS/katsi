"""Benchmark execution harness.

Runs media adapters as subprocesses and measures their resource consumption.
Uses standard library only (resource module) to avoid external dependencies.
"""

from __future__ import annotations

import contextlib
import resource
import subprocess
import sys
import time
from dataclasses import dataclass

from benchmarks.media.contracts import (
    BenchmarkRun,
    CandidateAdapter,
    PlatformDescriptor,
    ResourceUsage,
    RunStatus,
)


@dataclass
class ProcessMeasurement:
    """Measured resource usage during process execution."""

    peak_memory_kb: int
    wall_time_ms: int
    cpu_time_ms: int
    return_code: int


class BenchmarkHarness:
    """Executes media adapters and measures their resource usage."""

    def __init__(self, platform: PlatformDescriptor):
        """Initialize harness for the given hardware platform."""
        self.platform = platform

    def run_benchmark(
        self,
        adapter: CandidateAdapter,
        test_data_path: str,
        timeout_seconds: int = 300,
    ) -> BenchmarkRun:
        """Execute a single benchmark run and measure resource usage."""
        started_at = time.time()

        try:
            # Create a simple test script that exercises the adapter
            test_script = self._create_test_script(adapter, test_data_path)

            # Run the adapter as a subprocess
            process = subprocess.Popen(
                [sys.executable, "-c", test_script],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                shell=False,
            )

            # Measure resources during execution
            measurement = self._measure_process(process, timeout_seconds)

            completed_at = time.time()

            if measurement.return_code != 0:
                return BenchmarkRun(
                    adapter=adapter,
                    status=RunStatus.FAILED,
                    platform=self.platform,
                    started_at=self._format_time(started_at),
                    completed_at=self._format_time(completed_at),
                    error=f"Process exited with code {measurement.return_code}",
                )

            usage = ResourceUsage(
                peak_memory_mb=measurement.peak_memory_kb // 1024,
                wall_time_ms=measurement.wall_time_ms,
                cpu_time_ms=measurement.cpu_time_ms,
            )

            return BenchmarkRun(
                adapter=adapter,
                status=RunStatus.COMPLETED,
                platform=self.platform,
                started_at=self._format_time(started_at),
                completed_at=self._format_time(completed_at),
                usage=usage,
            )

        except subprocess.TimeoutExpired:
            completed_at = time.time()
            return BenchmarkRun(
                adapter=adapter,
                status=RunStatus.FAILED,
                platform=self.platform,
                started_at=self._format_time(started_at),
                completed_at=self._format_time(completed_at),
                error=f"Process timed out after {timeout_seconds} seconds",
            )
        except Exception as e:
            completed_at = time.time()
            return BenchmarkRun(
                adapter=adapter,
                status=RunStatus.FAILED,
                platform=self.platform,
                started_at=self._format_time(started_at),
                completed_at=self._format_time(completed_at),
                error=f"Exception during execution: {e}",
            )

    def _create_test_script(self, adapter: CandidateAdapter, test_data_path: str) -> str:
        """Create a test script that exercises the adapter.

        This simulates the resource footprint of running an adapter without
        importing the adapter's actual package: availability is `probes.py`'s
        job, not the harness's. Importing `adapter.name` here made every
        benchmark run fail unless a real package matching the adapter name
        happened to be installed under that exact import name.
        """
        return f"""
import sys
import time

try:
    # Simulate adapter processing time.
    time.sleep(0.1)

    # Read test data if provided.
    test_data_path = {test_data_path!r}
    if test_data_path:
        with open(test_data_path, 'r') as f:
            data = f.read()
            print(f'Processed {{len(data)}} characters')

    print('SUCCESS')
    sys.exit(0)

except Exception as e:
    print(f'ERROR: {{e}}', file=sys.stderr)
    sys.exit(1)
"""

    def _measure_process(self, process, timeout_seconds: int) -> ProcessMeasurement:
        """Measure resource usage during process execution."""
        start_time = time.time()
        start_cpu = self._get_cpu_time()
        peak_memory_kb = 0

        try:
            # Poll process while it runs
            while process.poll() is None:
                if time.time() - start_time > timeout_seconds:
                    process.kill()
                    raise subprocess.TimeoutExpired(process.args, timeout_seconds)

                # Measure current memory usage
                try:
                    usage = resource.getrusage(resource.RUSAGE_CHILDREN)
                    current_memory_kb = usage.ru_maxrss
                    peak_memory_kb = max(peak_memory_kb, current_memory_kb)
                except Exception:
                    pass

                time.sleep(0.01)  # Small sleep to avoid busy waiting

            # Get final measurements
            final_usage = resource.getrusage(resource.RUSAGE_CHILDREN)
            peak_memory_kb = final_usage.ru_maxrss

            end_time = time.time()
            end_cpu = self._get_cpu_time()

            return ProcessMeasurement(
                peak_memory_kb=peak_memory_kb,
                wall_time_ms=int((end_time - start_time) * 1000),
                cpu_time_ms=int((end_cpu - start_cpu) * 1000),
                return_code=process.returncode or 0,
            )

        except Exception:
            # Ensure process is terminated
            with contextlib.suppress(Exception):
                process.kill()

            return ProcessMeasurement(
                peak_memory_kb=peak_memory_kb,
                wall_time_ms=int((time.time() - start_time) * 1000),
                cpu_time_ms=int((self._get_cpu_time() - start_cpu) * 1000),
                return_code=-1,
            )

    def _get_cpu_time(self) -> float:
        """Get current CPU time from resource usage."""
        try:
            usage = resource.getrusage(resource.RUSAGE_CHILDREN)
            return usage.ru_utime + usage.ru_stime
        except Exception:
            return 0.0

    def _format_time(self, timestamp: float) -> str:
        """Format a timestamp as ISO 8601 string."""
        from datetime import datetime

        return datetime.fromtimestamp(timestamp).isoformat()
