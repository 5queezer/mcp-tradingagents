"""
OAuth 2.1 Authorization Server routes.

Mounts at root level. Provides:
  GET  /.well-known/oauth-authorization-server
  GET  /.well-known/oauth-protected-resource  (RFC 9728)
  POST /register                               (RFC 7591)
  GET  /authorize
  POST /token
  POST /revoke                                 (RFC 7009)
"""

import logging
import time
from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse

from .auth import AuthProvider, ClientStore, TokenStore, verify_pkce

logger = logging.getLogger(__name__)

SUPPORTED_METHODS = ["S256"]


def make_oauth_router(
    store: TokenStore,
    client_store: ClientStore,
    provider: AuthProvider,
    base_url: str,          # e.g. "https://my-service.run.app"
    scopes_supported: list[str] | None = None,
) -> APIRouter:
    router = APIRouter()
    scopes = scopes_supported or ["mcp:tools"]
    base_url = base_url.rstrip("/")

    # -----------------------------------------------------------------------
    # Protected Resource Metadata (RFC 9728)
    # Tells claude.ai which AS governs this MCP resource.
    # -----------------------------------------------------------------------

    @router.get("/.well-known/oauth-protected-resource")
    def protected_resource_metadata():
        return {
            "resource": base_url,
            "authorization_servers": [base_url],
        }

    # -----------------------------------------------------------------------
    # Authorization Server Discovery (RFC 8414)
    # -----------------------------------------------------------------------

    @router.get("/.well-known/oauth-authorization-server")
    def oauth_metadata():
        return {
            "issuer": base_url,
            "authorization_endpoint": f"{base_url}/authorize",
            "token_endpoint": f"{base_url}/token",
            "revocation_endpoint": f"{base_url}/revoke",
            "registration_endpoint": f"{base_url}/register",
            "code_challenge_methods_supported": SUPPORTED_METHODS,
            "response_types_supported": ["code"],
            "grant_types_supported": ["authorization_code"],
            "token_endpoint_auth_methods_supported": ["none"],
            "scopes_supported": scopes,
        }

    # -----------------------------------------------------------------------
    # Dynamic Client Registration (RFC 7591)
    # claude.ai registers itself before the first authorize request.
    # We accept any registration and issue a client_id; redirect_uri is
    # stored and validated in /authorize.
    # -----------------------------------------------------------------------

    @router.post("/register", status_code=201)
    async def register(request: Request):
        try:
            body = await request.json()
        except Exception:
            return JSONResponse(
                {"error": "invalid_request", "error_description": "Malformed JSON body"},
                status_code=400,
            )
        redirect_uris = body.get("redirect_uris", [])
        if not redirect_uris:
            return JSONResponse(
                {"error": "invalid_client_metadata", "error_description": "redirect_uris required"},
                status_code=400,
            )
        try:
            client = client_store.register(
                redirect_uris=redirect_uris,
                client_name=body.get("client_name", ""),
            )
        except ValueError as exc:
            return JSONResponse(
                {"error": "invalid_redirect_uri", "error_description": str(exc)},
                status_code=400,
            )
        return JSONResponse({
            "client_id": client.client_id,
            "client_id_issued_at": int(client.issued_at),
            "redirect_uris": client.redirect_uris,
            "client_name": client.client_name,
            "token_endpoint_auth_method": "none",
            "grant_types": ["authorization_code"],
            "response_types": ["code"],
        }, status_code=201)

    # -----------------------------------------------------------------------
    # Authorization Endpoint
    # -----------------------------------------------------------------------

    @router.get("/authorize")
    def authorize(
        request: Request,
        response_type: str = "code",
        client_id: str = "",
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

        # Validate client and redirect_uri against registration
        if client_id:
            client = client_store.get(client_id)
            if client is None:
                return JSONResponse({"error": "invalid_client"}, status_code=400)
            if redirect_uri not in client.redirect_uris:
                return JSONResponse(
                    {"error": "invalid_request", "error_description": "redirect_uri not registered for this client"},
                    status_code=400,
                )

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
            return JSONResponse({"error": "unsupported_grant_type"}, status_code=400)

        auth_code = store.consume_code(code)
        if auth_code is None:
            return JSONResponse(
                {"error": "invalid_grant", "error_description": "code expired or unknown"},
                status_code=400,
            )
        if not verify_pkce(code_verifier, auth_code.challenge):
            return JSONResponse(
                {"error": "invalid_grant", "error_description": "PKCE verification failed"},
                status_code=400,
            )

        if redirect_uri and redirect_uri != auth_code.redirect_uri:
            return JSONResponse(
                {"error": "invalid_grant", "error_description": "redirect_uri mismatch"},
                status_code=400,
            )

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
