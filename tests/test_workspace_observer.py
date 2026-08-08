from pathlib import Path

import pytest

from katsi_core.workspace.observer import WatchdogObserver


def test_watchdog_adapter_loads_lazily_without_optional_dependency(tmp_path: Path) -> None:
    observer = WatchdogObserver()
    with pytest.raises(RuntimeError, match="optional watchdog"):
        observer.start(tmp_path, lambda _path: None)
