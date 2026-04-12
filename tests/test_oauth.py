"""
Tests for MCP OAuth template.
Run: pytest tests/ -v
"""

import base64
import hashlib
import secrets
import pytest
from fastapi.testclient import TestClient

from mcp_server.auth import (
    SingleUserProvider,
    StaticPasswordProvider,
    TokenStore,
    verify_pkce,
)
from mcp_server.app import create_app


# ---------------------------------------------------------------------------
# PKCE Unit Tests
# ---------------------------------------------------------------------------

def make_pkce_pair():
    verifier = secrets.token_urlsafe(32)
    digest = hashlib.sha256(verifier.encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return verifier, challenge


def test_pkce_valid():
    verifier, challenge = make_pkce_pair()
    assert verify_pkce(verifier, challenge)


def test_pkce_wrong_verifier():
    _, challenge = make_pkce_pair()
    assert not verify_pkce("wrong_verifier", challenge)


def test_pkce_tampered_challenge():
    verifier, _ = make_pkce_pair()
    assert not verify_pkce(verifier, "tampered_challenge")


# ---------------------------------------------------------------------------
# TokenStore Unit Tests
# ---------------------------------------------------------------------------

def test_token_store_full_flow():
    store = TokenStore()
    verifier, challenge = make_pkce_pair()

    code = store.create_code(
        challenge=challenge,
        redirect_uri="https://claude.ai/callback",
        state="xyz",
        sub="test-user",
    )

    entry = store.consume_code(code)
    assert entry is not None
    assert entry.challenge == challenge

    # Code should be consumed (single-use)
    assert store.consume_code(code) is None


def test_token_store_invalid_token():
    store = TokenStore()
    assert store.validate_token("nonexistent") is None


def test_token_store_revoke():
    store = TokenStore()
    verifier, challenge = make_pkce_pair()
    code = store.create_code(challenge, "https://claude.ai/callback", "", "test-user")
    store.consume_code(code)  # consume so we can issue token

    token = store.create_token("user")
    assert store.validate_token(token) is not None

    store.revoke_token(token)
    assert store.validate_token(token) is None


# ---------------------------------------------------------------------------
# Integration Tests (Full OAuth Flow)
# ---------------------------------------------------------------------------

@pytest.fixture
def client():
    app = create_app(title="Test MCP")
    return TestClient(app, raise_server_exceptions=True)


def test_health(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


def test_oauth_metadata(client):
    resp = client.get("/.well-known/oauth-authorization-server")
    assert resp.status_code == 200
    data = resp.json()
    assert "authorization_endpoint" in data
    assert "token_endpoint" in data
    assert "S256" in data["code_challenge_methods_supported"]


def _full_pkce_flow(client) -> str:
    """Helper: runs full PKCE flow, returns access token."""
    verifier, challenge = make_pkce_pair()
    redirect_uri = "https://claude.ai/callback"

    # Step 1: /authorize (SingleUserProvider -- no login needed)
    resp = client.get(
        "/authorize",
        params={
            "response_type": "code",
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "redirect_uri": redirect_uri,
            "state": "test123",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 302
    location = resp.headers["location"]
    assert "code=" in location
    assert "state=test123" in location

    code = location.split("code=")[1].split("&")[0]

    # Step 2: /token
    resp = client.post(
        "/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "code_verifier": verifier,
            "redirect_uri": redirect_uri,
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert "access_token" in data
    assert data["token_type"] == "bearer"
    return data["access_token"]


def test_full_oauth_flow(client):
    token = _full_pkce_flow(client)
    assert len(token) > 10


def test_mcp_requires_bearer(client):
    resp = client.post("/mcp", json={})
    assert resp.status_code == 401


def test_mcp_with_valid_token(client):
    token = _full_pkce_flow(client)
    resp = client.post(
        "/mcp",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "jsonrpc": "2.0",
            "method": "tools/call",
            "params": {"name": "ping", "arguments": {}},
            "id": 1,
        },
    )
    # MCP might return 404 for unrecognized route shape, but NOT 401
    assert resp.status_code != 401


def test_code_single_use(client):
    """Auth codes must be consumed exactly once."""
    verifier, challenge = make_pkce_pair()
    resp = client.get(
        "/authorize",
        params={
            "response_type": "code",
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "redirect_uri": "https://claude.ai/callback",
            "state": "",
        },
        follow_redirects=False,
    )
    code = resp.headers["location"].split("code=")[1].split("&")[0]

    # First exchange: success
    r1 = client.post("/token", data={
        "grant_type": "authorization_code",
        "code": code,
        "code_verifier": verifier,
    })
    assert r1.status_code == 200

    # Second exchange: fail
    r2 = client.post("/token", data={
        "grant_type": "authorization_code",
        "code": code,
        "code_verifier": verifier,
    })
    assert r2.status_code == 400


def test_wrong_verifier_rejected(client):
    _, challenge = make_pkce_pair()
    resp = client.get(
        "/authorize",
        params={
            "response_type": "code",
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "redirect_uri": "https://claude.ai/callback",
            "state": "",
        },
        follow_redirects=False,
    )
    code = resp.headers["location"].split("code=")[1].split("&")[0]

    r = client.post("/token", data={
        "grant_type": "authorization_code",
        "code": code,
        "code_verifier": "wrong_verifier_that_will_fail",
    })
    assert r.status_code == 400
