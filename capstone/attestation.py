"""Ed25519 attestation envelopes for release evidence.

CI holds the private key (a CI secret) and signs evidence; the API holds only public keys
(CAPSTONE_ATTESTATION_KEYS), so neither an operator nor the API itself can forge a report.
"""
from __future__ import annotations
import base64
import hashlib
import json

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey


class AttestationError(ValueError):
    pass


def canonical(payload: dict) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()


def key_id(public: Ed25519PublicKey) -> str:
    raw = public.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return hashlib.sha256(raw).hexdigest()[:16]


def generate_keypair() -> tuple[str, str, str]:
    """Return (private PEM, public PEM, key id)."""
    private = Ed25519PrivateKey.generate()
    private_pem = private.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                        serialization.NoEncryption()).decode()
    public = private.public_key()
    public_pem = public.public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode()
    return private_pem, public_pem, key_id(public)


def sign(payload: dict, private_pem: str) -> dict:
    private = serialization.load_pem_private_key(private_pem.encode(), password=None)
    if not isinstance(private, Ed25519PrivateKey):
        raise AttestationError("Attestation key must be Ed25519")
    body = canonical(payload)
    return {"payload": base64.b64encode(body).decode(), "signature": base64.b64encode(private.sign(body)).decode(),
            "key_id": key_id(private.public_key())}


def load_public_keys(mapping: dict[str, str]) -> dict[str, Ed25519PublicKey]:
    keys = {}
    for kid, pem in mapping.items():
        public = serialization.load_pem_public_key(pem.encode())
        if not isinstance(public, Ed25519PublicKey) or key_id(public) != kid:
            raise AttestationError("Trusted key is not Ed25519 or its key id does not match")
        keys[kid] = public
    return keys


def verify(envelope: dict, trusted: dict[str, Ed25519PublicKey]) -> tuple[dict, str]:
    """Return (payload, payload sha256) if the envelope is signed by a trusted key."""
    try:
        public = trusted[envelope["key_id"]]
        body = base64.b64decode(envelope["payload"], validate=True)
        public.verify(base64.b64decode(envelope["signature"], validate=True), body)
        payload = json.loads(body)
    except (KeyError, TypeError, ValueError, InvalidSignature):
        raise AttestationError("Evidence signature is invalid or from an untrusted key") from None
    if not isinstance(payload, dict) or canonical(payload) != body:
        raise AttestationError("Evidence payload is not canonical")
    return payload, hashlib.sha256(body).hexdigest()
