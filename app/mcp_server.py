"""In-container MCP endpoint (FastMCP) so Claude can trigger a run on demand.

Exposes a small set of tools over streamable-HTTP — `trigger_run` (schedule a
search cycle), `get_status` (latest cycle stats), and `list_searches` (active
searches). It runs in a daemon thread (mirroring :func:`app.health.start_health_server`)
and never executes cycles itself: it only flips the existing manual-trigger flag
and reads the health snapshot, so there is no concurrent-cycle risk.

``fastmcp`` / ``uvicorn`` are imported lazily inside :func:`start_mcp_server`, so
the core app has no hard dependency on them unless ``MCP_ENABLED`` is set.
"""

from __future__ import annotations

import logging
import threading
from typing import Callable, List, Optional

from .config import Config

log = logging.getLogger("flatwatch.mcp")

# Type aliases for the callables main() injects.
RequestRun = Callable[..., dict]
GetStatus = Callable[[], dict]
ListSearches = Optional[Callable[[], List[dict]]]


def start_mcp_server(
    cfg: Config,
    *,
    request_run: RequestRun,
    get_status: GetStatus,
    list_searches: ListSearches = None,
):
    """Start the FastMCP server in a daemon thread. Returns the server or None.

    No-op (returns None) when ``MCP_ENABLED`` is false or the optional deps are
    missing — flatwatch keeps running either way.
    """
    if not cfg.mcp_enabled:
        return None
    try:
        import uvicorn
        from fastmcp import FastMCP
    except ImportError as exc:  # optional dependency
        log.error("MCP_ENABLED but fastmcp/uvicorn not installed (%s) — MCP disabled.", exc)
        return None

    # Capture the injected callables under private names so the tool functions
    # below (which reuse the names get_status/list_searches) don't shadow them.
    _request_run, _get_status, _list_searches = request_run, get_status, list_searches

    try:
        mcp = FastMCP("flatwatch")

        @mcp.tool
        def trigger_run(wait: bool = False) -> dict:
            """Trigger a flatwatch search cycle.

            With wait=false (default) it schedules a cycle and returns
            immediately; call get_status afterwards to see the result. With
            wait=true it blocks until the cycle finishes (up to a few minutes for
            large searches) and returns the resulting stats.
            """
            return _request_run(wait=wait)

        @mcp.tool
        def get_status() -> dict:
            """Return the latest poll-cycle stats and health (status, last_cycle,
            last_success_at, consecutive_failures, new_count, …)."""
            return _get_status()

        if _list_searches is not None:
            @mcp.tool
            def list_searches() -> list:
                """List the currently active searches (label, url, source_type)."""
                return _list_searches()

        app = _build_app(mcp, cfg)

        config = uvicorn.Config(app, host=cfg.mcp_host, port=cfg.mcp_port, log_level="warning")
        server = uvicorn.Server(config)
        server.install_signal_handlers = lambda: None  # required: not the main thread
        thread = threading.Thread(target=server.run, daemon=True, name="mcp")
        thread.start()
    except Exception as exc:  # never let an MCP/FastMCP issue crash the app
        log.error("Could not start MCP endpoint (%s) — continuing without it.", exc)
        return None

    auth = "token-protected" if cfg.mcp_auth_token else "open (LAN-only)"
    log.info("MCP endpoint listening on http://%s:%d%s (%s).", cfg.mcp_host, cfg.mcp_port, cfg.mcp_path, auth)
    return server


def _build_app(mcp, cfg: Config):
    """Return the streamable-HTTP ASGI app, wrapped with optional bearer auth."""
    app = mcp.http_app(path=cfg.mcp_path)
    if cfg.mcp_auth_token:
        app = _BearerAuth(app, cfg.mcp_auth_token)
    return app


class _BearerAuth:
    """Minimal ASGI middleware enforcing ``Authorization: Bearer <token>``."""

    def __init__(self, app, token: str):
        self.app = app
        self._expected = f"Bearer {token}"

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            headers = dict(scope.get("headers") or [])
            if headers.get(b"authorization", b"").decode() != self._expected:
                await send({"type": "http.response.start", "status": 401,
                            "headers": [(b"content-type", b"text/plain")]})
                await send({"type": "http.response.body", "body": b"unauthorized"})
                return
        await self.app(scope, receive, send)
