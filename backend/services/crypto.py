"""Shared encryption helpers (Fernet) for secrets at rest."""
import os
import base64
from cryptography.fernet import Fernet


def _load_key() -> bytes:
    raw = os.getenv("ENCRYPTION_KEY")
    if not raw:
        # Ephemeral key — encrypted data won't survive a restart. Only used in
        # dev when ENCRYPTION_KEY is unset.
        return Fernet.generate_key()
    try:
        key = raw.encode() if isinstance(raw, str) else raw
        Fernet(key)  # validate
        return key
    except Exception:
        key = raw.encode() if isinstance(raw, str) else raw
        return base64.urlsafe_b64encode(key.ljust(32, b"\0")[:32])


_fernet = Fernet(_load_key())


def encrypt_json(value) -> str:
    """Encrypt a JSON-serialisable value; return a token string."""
    import json
    return _fernet.encrypt(json.dumps(value).encode()).decode()


def decrypt_json(token: str):
    """Decrypt a token produced by encrypt_json; return the original value."""
    import json
    if not token:
        return None
    return json.loads(_fernet.decrypt(token.encode()).decode())
