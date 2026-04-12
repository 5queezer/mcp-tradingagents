"""
MCP OAuth Server -- App Factory.

Usage:
  from mcp_server.app import create_app
  app = create_app()

Or for a custom service:
  from mcp_server.app import create_app
  from mcp_server.auth import StaticPasswordProvider
  import fastmcp

  mcp = fastmcp.FastMCP("polymarket")

  @mcp.tool()
  def get_markets(keyword: str) -> list[dict]:
      ...

  app = create_app(mcp=mcp, provider=StaticPasswordProvider("s3cr3t"))
"""

import os
import logging
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

from .auth import SingleUserProvider, StaticPasswordProvider, TokenStore
from .oauth_routes import make_oauth_router

logger = logging.getLogger(__name__)


class BearerMiddleware(BaseHTTPMiddleware):
    """
    Validates Bearer tokens on /mcp/* routes.
    Passes through all other routes (OAuth endpoints, health).
    """
    PROTECTED_PREFIX = "/mcp"

    def __init__(self, app, store: TokenStore):
        super().__init__(app)
        self._store = store

    async def dispatch(self, request: Request, call_next):
        if not request.url.path.startswith(self.PROTECTED_PREFIX):
            return await call_next(request)

        auth = request.headers.get("authorization", "")
        if not auth.lower().startswith("bearer "):
            return JSONResponse(
                {"error": "invalid_token", "error_description": "Bearer token required"},
                status_code=401,
                headers={"WWW-Authenticate": "Bearer"},
            )

        token_str = auth[7:]
        entry = self._store.validate_token(token_str)
        if entry is None:
            return JSONResponse(
                {"error": "invalid_token", "error_description": "Token expired or unknown"},
                status_code=401,
                headers={"WWW-Authenticate": "Bearer"},
            )

        return await call_next(request)


def create_app(
    mcp=None,
    provider=None,
    base_url: str | None = None,
    title: str = "MCP OAuth Server",
) -> FastAPI:
    """
    App factory. Wires together:
      - OAuth 2.1 AS routes (/.well-known, /authorize, /token, /revoke)
      - MCP HTTP transport at /mcp (Bearer-protected via middleware)
      - CORS for claude.ai

    Args:
        mcp:       fastmcp.FastMCP instance. If None, a stub is used.
        provider:  AuthProvider instance. Defaults to env-driven selection.
        base_url:  Public URL used in OAuth metadata.
                   Falls back to BASE_URL env var, then http://localhost:8080.
        title:     OpenAPI / service title.
    """
    _base_url = base_url or os.getenv("BASE_URL", "http://localhost:8080")
    _provider = provider or _default_provider()
    _store = TokenStore()
    _mcp = mcp or _stub_mcp()

    app = FastAPI(title=title, docs_url="/docs", redoc_url=None)

    # 1. CORS -- claude.ai requires this
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["https://claude.ai", "https://api.claude.ai"],
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type"],
        allow_credentials=True,
    )

    # 2. Bearer enforcement on /mcp/*
    app.add_middleware(BearerMiddleware, store=_store)

    # 3. OAuth 2.1 AS routes
    oauth_router = make_oauth_router(
        store=_store,
        provider=_provider,
        base_url=_base_url,
    )
    app.include_router(oauth_router)

    # 4. MCP transport -- middleware handles auth, no extra deps needed
    app.mount("/mcp", _mcp.http_app())

    # 5. Health
    @app.get("/health")
    def health():
        return {"status": "ok", "base_url": _base_url}

    logger.info("MCP OAuth server ready at %s", _base_url)
    return app


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------

def _default_provider():
    pw = os.getenv("ADMIN_PASSWORD")
    if pw:
        logger.info("Using StaticPasswordProvider (ADMIN_PASSWORD set)")
        return StaticPasswordProvider(pw)
    logger.warning(
        "No ADMIN_PASSWORD set -- SingleUserProvider active. "
        "Protect /authorize at network level."
    )
    return SingleUserProvider()


def _stub_mcp():
    import fastmcp

    mcp = fastmcp.FastMCP("stub")

    @mcp.tool()
    def ping() -> str:
        """Health check tool."""
        return "pong"

    return mcp
