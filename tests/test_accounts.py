import time
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from capstone.api import create_app
from capstone.auth import GoogleOIDC, hash_password, verify_password
from capstone.config import Settings
from tests.conftest import TOKENS

CSRF = {"X-Requested-With": "capstone"}
PASSWORD = "correct horse battery"


def signup(client, email="ada@example.com", password=PASSWORD):
    return client.post("/v1/auth/signup", json={"email": email, "password": password})


def test_password_hash_roundtrip():
    stored = hash_password(PASSWORD)
    assert stored.startswith("scrypt$") and PASSWORD not in stored
    assert verify_password(PASSWORD, stored)
    assert not verify_password("wrong password!", stored)
    assert not verify_password(PASSWORD, None)
    assert not verify_password(PASSWORD, "garbage")


def test_signup_creates_private_workspace_and_session(client):
    r = signup(client)
    assert r.status_code == 201, r.text
    me = r.json()
    assert me["user"] == "ada@example.com" and me["role"] == "admin" and me["groups"] == ["personal"]
    assert me["tenant"].startswith("ws-") and not me["release_operator"]
    cookie = r.headers["set-cookie"]
    assert "capstone_session=" in cookie and "HttpOnly" in cookie and "samesite=lax" in cookie.lower()
    assert client.get("/v1/me").json() == me


def test_each_signup_gets_its_own_tenant(app):
    a, b = TestClient(app), TestClient(app)
    assert signup(a, "a@example.com").json()["tenant"] != signup(b, "b@example.com").json()["tenant"]
    r = a.post("/v1/documents", headers=CSRF, json={"title": "A notes", "group": "personal",
                                                     "text": "Project falcon launches on the ninth"})
    assert r.status_code == 201, r.text
    assert b.post("/v1/rag/query", headers=CSRF, json={"question": "project falcon launch"}).json()["citations"] == []
    assert a.post("/v1/rag/query", headers=CSRF, json={"question": "project falcon launch"}).json()["citations"]


def test_workspace_cannot_see_static_tenants_or_release(client, headers):
    client.post("/v1/documents", headers=headers["alice"], json={"title": "Acme", "group": "engineering",
                                                                  "text": "Acme payroll runs on Fridays"})
    signup(client)
    assert client.get("/v1/documents").json()["documents"] == []
    assert client.get("/v1/models").status_code == 403


def test_duplicate_and_invalid_signup(client):
    assert signup(client).status_code == 201
    assert signup(client, "ADA@example.com").status_code == 409
    assert signup(client, "not-an-email").status_code == 422
    assert signup(client, "x" * 60 + "@example.com").status_code == 422
    assert signup(client, "short@example.com", "short").status_code == 422


def test_login_and_logout(app):
    signup(TestClient(app))
    client = TestClient(app)
    assert client.post("/v1/auth/login", json={"email": "ada@example.com", "password": "wrong password"}).status_code == 401
    assert client.post("/v1/auth/login", json={"email": "nobody@example.com", "password": PASSWORD}).json() == \
        {"detail": "Invalid email or password"}
    r = client.post("/v1/auth/login", json={"email": " Ada@Example.com ", "password": PASSWORD})
    assert r.status_code == 200 and r.json()["user"] == "ada@example.com"
    token = client.cookies["capstone_session"]
    assert client.post("/v1/auth/logout").status_code == 403  # CSRF header required
    assert client.post("/v1/auth/logout", headers=CSRF).status_code == 204
    # The session is revoked server-side, not just cleared from the browser.
    assert TestClient(app, cookies={"capstone_session": token}).get("/v1/me").status_code == 401


def test_cookie_requests_need_csrf_header_but_bearer_does_not(client, headers):
    signup(client)
    body = {"question": "anything"}
    assert client.post("/v1/rag/query", json=body).status_code == 403
    assert client.post("/v1/rag/query", json=body, headers=CSRF).status_code == 200
    assert client.post("/v1/rag/query", json=body, headers=headers["alice"]).status_code == 200


def test_login_is_rate_limited(client):
    signup(client)
    codes = [client.post("/v1/auth/login", json={"email": "ada@example.com", "password": "wrong password"}).status_code
             for _ in range(11)]
    assert codes[:10] == [401] * 10 and codes[10] == 429


def test_forged_session_cookie_rejected(app):
    assert TestClient(app, cookies={"capstone_session": "forged"}).get("/v1/me").status_code == 401


def test_google_disabled_by_default(client):
    assert client.get("/v1/auth/config").json() == {"password": True, "google": False}
    assert client.get("/v1/auth/google/start", follow_redirects=False).status_code == 404


# --- Google sign-in, against a fake Google that signs ID tokens with a test RSA key ---------------------

KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


class FakeKeys:
    def get_signing_key_from_jwt(self, _token):
        return type("Key", (), {"key": KEY.public_key()})()


@pytest.fixture
def google_app(artifact_root):
    settings = Settings(database_url="sqlite://", redis_url=None, api_tokens=TOKENS, routing_secret="x" * 40,
                        artifact_root=str(artifact_root), google_client_id="client-123",
                        google_client_secret="secret")
    app = create_app(settings, init_tables=True)
    app.state.google.keys = FakeKeys()
    return app


def google_login(app, claims=None, key=KEY, tamper_state=False):
    client = TestClient(app)
    start = client.get("/v1/auth/google/start", follow_redirects=False)
    assert start.status_code == 302 and start.headers["location"].startswith(GoogleOIDC.AUTHORIZE)
    assert "code_challenge_method=S256" in start.headers["location"]
    state, nonce, _ = client.cookies["capstone_oauth"].split(".")
    token = jwt.encode({"iss": "https://accounts.google.com", "aud": "client-123", "sub": "google-1",
                        "email": "ada@example.com", "email_verified": True, "nonce": nonce,
                        "iat": int(time.time()), "exp": int(time.time()) + 300, **(claims or {})},
                       key, algorithm="RS256")
    app.state.google.exchange_code = lambda code, verifier: token
    r = client.get("/v1/auth/google/callback", params={"code": "c", "state": "evil" if tamper_state else state},
                   follow_redirects=False)
    return client, r


def test_google_sign_in_creates_account(google_app):
    assert TestClient(google_app).get("/v1/auth/config").json()["google"] is True
    client, r = google_login(google_app)
    assert r.status_code == 302 and r.headers["location"] == "/"
    assert client.get("/v1/me").json()["user"] == "ada@example.com"
    # Signing in again reuses the same account and workspace.
    tenant = client.get("/v1/me").json()["tenant"]
    again, _ = google_login(google_app)
    assert again.get("/v1/me").json()["tenant"] == tenant


@pytest.mark.parametrize("claims,kwargs", [
    ({"email_verified": False}, {}),
    ({"aud": "someone-else"}, {}),
    ({"nonce": "replayed"}, {}),
    ({"iss": "https://evil.example"}, {}),
    ({"exp": int(time.time()) - 10}, {}),
    ({}, {"key": rsa.generate_private_key(public_exponent=65537, key_size=2048)}),
    ({}, {"tamper_state": True}),
])
def test_google_rejects_bad_tokens(google_app, claims, kwargs):
    client, r = google_login(google_app, claims, **kwargs)
    assert r.headers["location"] == "/?auth_error=google"
    assert client.get("/v1/me").status_code == 401


def test_google_takes_over_unverified_password_account(google_app):
    squatter = TestClient(google_app)
    tenant = signup(squatter).json()["tenant"]
    _, r = google_login(google_app)
    assert r.headers["location"] == "/"
    # The pre-registered password and its sessions no longer work; the Google user owns the account.
    assert squatter.get("/v1/me").status_code == 401
    assert TestClient(google_app).post("/v1/auth/login", json={"email": "ada@example.com",
                                                               "password": PASSWORD}).status_code == 401
    client, _ = google_login(google_app)
    assert client.get("/v1/me").json()["tenant"] == tenant
