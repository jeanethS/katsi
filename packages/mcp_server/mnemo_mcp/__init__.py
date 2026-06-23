"""mnemo MCP server package."""

__version__ = "0.1.0"


def main() -> None:
    """Entrypoint for `mnemo-mcp` script."""
    from mnemo_mcp.server import main as _real
    _real()
