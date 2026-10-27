"""Accounts: scrypt passwords, server-side sessions and Google sign-in (OIDC code flow with PKCE).

Every account gets its own workspace tenant, so signing up never grants access to another tenant's data.
"""
import base64
import hashlib
import hmac
import json
import re
import secrets
import urllib.parse
import urllib.request
from datetime import timedelta, timezone
from sqlalchemy import delete, select
from .db import AuthSession, User, now
from .security import Principal

SESSION_COOKIE = "capstone_session"
OAUTH_COOKIE = "capstone_oauth"
WORKSPACE_GROUP = "personal"
EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

# OWASP minimum for scrypt (~16 MB per hash). Parameters are stored with each hash so they can be raised later.
SCRYPT_N, SCRYPT_R, SCRYPT_P = 2**14, 8, 1
SCRYPT_MAXMEM = 64 * 1024 * 1024


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def normalize_email(email: str) -> str | None:
    email = email.strip().lower()
    # 64 characters: the email becomes the principal's user, stored in 64-character owner/actor columns.
    return email if len(email) <= 64 and EMAIL.match(email) else None


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    key = hashlib.scrypt(password.encode(), salt=salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P,
                         maxmem=SCRYPT_MAXMEM, dklen=32)
    return f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${_b64(salt)}${_b64(key)}"


def verify_password(password: str, stored: str | None) -> bool:
    try:
        _, n, r, p, salt, key = (stored or _dummy_hash()).split("$")
        expected = _unb64(key)
        got = hashlib.scrypt(password.encode(), salt=_unb64(salt), n=int(n), r=int(r), p=int(p),
                             maxmem=SCRYPT_MAXMEM, dklen=len(expected))
    except ValueError:
        return False
    return stored is not None and hmac.compare_digest(got, expected)


_DUMMY: list[str] = []


def _dummy_hash() -> str:
    # Unknown emails still pay for one scrypt, so response time doesn't reveal which accounts exist.
    if not _DUMMY:
        _DUMMY.append(hash_password(secrets.token_urlsafe(16)))
    return _DUMMY[0]


def new_user(email: str, *, password_hash=None, google_sub=None, name=None, verified=False) -> User:
    return User(email=email, name=name, tenant=f"ws-{secrets.token_hex(8)}", password_hash=password_hash,
                google_sub=google_sub, email_verified=verified)


def principal_for(user: User) -> Principal:
    # Admin of a one-person workspace. Research approval still needs a second, independent reviewer.
    return Principal(tenant=user.tenant, user=user.email, groups=frozenset({WORKSPACE_GROUP}), role="admin")


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def start_session(db, user: User, hours: int) -> str:
    token = secrets.token_urlsafe(32)
    db.add(AuthSession(token_sha256=_digest(token), user_id=user.id, expires_at=now() + timedelta(hours=hours)))
    return token


def session_user(db, token: str) -> User | None:
    row = db.get(AuthSession, _digest(token))
    if row is None:
        return None
    expires = row.expires_at if row.expires_at.tzinfo else row.expires_at.replace(tzinfo=timezone.utc)  # SQLite
    if expires <= now():
        db.delete(row)
        db.commit()
        return None
    return db.get(User, row.user_id)


def end_session(db, token: str):
    db.execute(delete(AuthSession).where(AuthSession.token_sha256 == _digest(token)))


def google_account(db, sub: str, email: str, name: str | None) -> User:
    """Find or create the account for a Google identity whose email Google has verified."""
    user = db.scalar(select(User).where(User.google_sub == sub))
    if user:
        return user
    user = db.scalar(select(User).where(User.email == email))
    if user is None:
        user = new_user(email, google_sub=sub, name=name, verified=True)
        db.add(user)
        return user
    if not user.email_verified:
        # Whoever registered this email with a password never proved they own it. Google just did, so the
        # unverified password and its sessions are removed (prevents pre-registering a victim's address).
        user.password_hash = None
        db.execute(delete(AuthSession).where(AuthSession.user_id == user.id))
    user.google_sub, user.email_verified, user.name = sub, True, user.name or name
    return user


class GoogleAuthError(Exception):
    pass


class GoogleOIDC:
    AUTHORIZE = "https://accounts.google.com/o/oauth2/v2/auth"
    TOKEN = "https://oauth2.googleapis.com/token"
    JWKS = "https://www.googleapis.com/oauth2/v3/certs"
    ISSUERS = ["https://accounts.google.com", "accounts.google.com"]

    def __init__(self, client_id: str, client_secret: str, redirect_uri: str):
        import jwt
        self.client_id, self.client_secret, self.redirect_uri = client_id, client_secret, redirect_uri
        self.keys = jwt.PyJWKClient(self.JWKS, cache_keys=True)

    def start(self) -> tuple[str, str]:
        """Return the Google authorization URL and the value for the short-lived OAuth cookie."""
        state, nonce, verifier = (secrets.token_urlsafe(32) for _ in range(3))
        params = {
            "client_id": self.client_id, "redirect_uri": self.redirect_uri, "response_type": "code",
            "scope": "openid email profile", "state": state, "nonce": nonce, "prompt": "select_account",
            "code_challenge": _b64(hashlib.sha256(verifier.encode()).digest()), "code_challenge_method": "S256",
        }
        return f"{self.AUTHORIZE}?{urllib.parse.urlencode(params)}", f"{state}.{nonce}.{verifier}"

    def exchange_code(self, code: str, verifier: str) -> str:
        body = urllib.parse.urlencode({
            "code": code, "client_id": self.client_id, "client_secret": self.client_secret,
            "redirect_uri": self.redirect_uri, "grant_type": "authorization_code", "code_verifier": verifier,
        }).encode()
        request = urllib.request.Request(self.TOKEN, data=body, headers={"Accept": "application/json"})
        with urllib.request.urlopen(request, timeout=10) as response:
            return json.load(response)["id_token"]

    def identity(self, code: str, state: str, cookie: str) -> dict:
        """Check state against the cookie, redeem the code and verify the signed ID token."""
        import jwt
        try:
            expected_state, nonce, verifier = cookie.split(".")
        except ValueError:
            raise GoogleAuthError("missing or malformed OAuth cookie")
        if not hmac.compare_digest(state.encode(), expected_state.encode()):
            raise GoogleAuthError("state mismatch")
        try:
            id_token = self.exchange_code(code, verifier)
            key = self.keys.get_signing_key_from_jwt(id_token).key
            claims = jwt.decode(id_token, key, algorithms=["RS256"], audience=self.client_id, issuer=self.ISSUERS,
                                options={"require": ["exp", "iat", "sub", "email"]})
        except Exception as exc:
            raise GoogleAuthError("token exchange or verification failed") from exc
        if not hmac.compare_digest(str(claims.get("nonce", "")).encode(), nonce.encode()):
            raise GoogleAuthError("nonce mismatch")
        if claims.get("email_verified") is not True:
            raise GoogleAuthError("Google has not verified this email")
        email = normalize_email(claims["email"])
        if email is None:
            raise GoogleAuthError("email not supported")
        return {"sub": claims["sub"], "email": email, "name": (claims.get("name") or "")[:100] or None}
