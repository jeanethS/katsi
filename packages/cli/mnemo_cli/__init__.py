"""mnemo CLI package."""

__version__ = "0.1.0"


def main() -> None:
    """Entrypoint for `mnemo` script."""
    from mnemo_cli.main import main as _real
    _real()
