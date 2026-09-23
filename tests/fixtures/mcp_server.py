"""MCP taşıma testlerinde kullanılan yerel, salt okunur sunucu."""
import sys
from mcp.server.fastmcp import FastMCP

port = int(sys.argv[2]) if len(sys.argv) > 2 else 8765
server = FastMCP("omni-deneme", host="127.0.0.1", port=port, stateless_http=True)


@server.tool()
def toplam(a: int, b: int) -> int:
    """İki sayıyı toplar."""
    return a + b


@server.tool()
def fark(a: int, b: int) -> int:
    """İki sayının farkını bulur."""
    return a - b


if __name__ == "__main__":
    server.run(transport="streamable-http" if "--http" in sys.argv else "stdio")

