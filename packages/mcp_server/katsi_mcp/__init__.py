"""katsi MCP server package."""

__version__ = "0.1.0"


def main() -> None:
    """Entrypoint for `katsi-mcp` script."""
    from katsi_mcp.server import main as _real
    _real()
