"""
OAuth 2.1 Authorization Server routes.

Mounts at root level. Provides:
  GET  /.well-known/oauth-authorization-server
  GET  /authorize
  POST /token
  POST /revoke   (optional, RFC 7009)
"""

import logging
from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse

from .auth import AuthProvider, TokenStore, verify_pkce

logger = logging.getLogger(__name__)

SUPPORTED_METHODS = ["S256"]


def make_oauth_router(
    store: TokenStore,
    provider: AuthProvider,
    base_url: str,          # e.g. "https://my-service.run.app"
    scopes_supported: list[str] | None = None,
) -> APIRouter:
    router = APIRouter()
    scopes = scopes_supported or ["mcp:tools"]

    # -----------------------------------------------------------------------
    # Discovery (RFC 8414)
    # -----------------------------------------------------------------------

    @router.get("/.well-known/oauth-authorization-server")
    def oauth_metadata():
        return {
            "issuer": base_url,
            "authorization_endpoint": f"{base_url}/authorize",
            "token_endpoint": f"{base_url}/token",
            "revocation_endpoint": f"{base_url}/revoke",
            "code_challenge_methods_supported": SUPPORTED_METHODS,
            "response_types_supported": ["code"],
            "grant_types_supported": ["authorization_code"],
            "token_endpoint_auth_methods_supported": ["none"],
            "scopes_supported": scopes,
        }

    # -----------------------------------------------------------------------
    # Authorization Endpoint
    # -----------------------------------------------------------------------

    @router.get("/authorize")
    def authorize(
        request: Request,
        response_type: str = "code",
        code_challenge: str = "",
        code_challenge_method: str = "S256",
        redirect_uri: str = "",
        state: str = "",
        scope: str = "",
    ):
        if response_type != "code":
            raise HTTPException(400, "unsupported_response_type")
        if code_challenge_method not in SUPPORTED_METHODS:
            raise HTTPException(400, "invalid_request: only S256 supported")
        if not code_challenge:
            raise HTTPException(400, "invalid_request: code_challenge required")
        if not redirect_uri:
            raise HTTPException(400, "invalid_request: redirect_uri required")

        sub = provider.authenticate(request)
        if sub is None:
            # Multi-user: render login page, then redirect here after creds
            # For now: 401 with WWW-Authenticate hint
            raise HTTPException(
                401,
                detail="authentication_required",
                headers={"WWW-Authenticate": "Bearer"},
            )

        code = store.create_code(
            challenge=code_challenge,
            redirect_uri=redirect_uri,
            state=state,
            sub=sub,
        )
        sep = "&" if "?" in redirect_uri else "?"
        location = f"{redirect_uri}{sep}code={code}&state={state}"
        logger.info("Issued auth code for sub=%s", sub)
        return RedirectResponse(location, status_code=302)

    # -----------------------------------------------------------------------
    # Token Endpoint
    # -----------------------------------------------------------------------

    @router.post("/token")
    async def token(
        grant_type: str = Form(...),
        code: str = Form(...),
        code_verifier: str = Form(...),
        redirect_uri: str = Form(default=""),
    ):
        if grant_type != "authorization_code":
            raise HTTPException(400, "unsupported_grant_type")

        auth_code = store.consume_code(code)
        if auth_code is None:
            raise HTTPException(400, "invalid_grant: code expired or unknown")
        if not verify_pkce(code_verifier, auth_code.challenge):
            raise HTTPException(400, "invalid_grant: PKCE verification failed")

        if redirect_uri and redirect_uri != auth_code.redirect_uri:
            raise HTTPException(400, "invalid_grant: redirect_uri mismatch")

        access_token = store.create_token(auth_code.sub)

        logger.info("Issued access token for sub=%s", auth_code.sub)
        return JSONResponse({
            "access_token": access_token,
            "token_type": "bearer",
            "expires_in": 3600,
            "scope": " ".join(scopes),
        })

    # -----------------------------------------------------------------------
    # Revocation Endpoint (RFC 7009)
    # -----------------------------------------------------------------------

    @router.post("/revoke")
    async def revoke(token: str = Form(...)):
        store.revoke_token(token)
        return JSONResponse({}, status_code=200)

    return router
