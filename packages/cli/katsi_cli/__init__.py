"""katsi CLI package."""

__version__ = "0.1.0"


def main() -> None:
    """Entrypoint for `katsi` script."""
    from katsi_cli.main import main as _real

    _real()
