"""Fernet for secrets stored in the database, and signed unsubscribe tokens."""

from cryptography.fernet import Fernet, InvalidToken
from itsdangerous import BadSignature, URLSafeSerializer

from app.config import settings

_unsubscribe = URLSafeSerializer(settings.secret_key, salt="unsubscribe")


def _fernet() -> Fernet:
    if not settings.fernet_key:
        raise RuntimeError("FERNET_KEY is not set in .env.")
    return Fernet(settings.fernet_key.encode())


def encrypt_secret(plain: str) -> str:
    return _fernet().encrypt(plain.encode()).decode()


def decrypt_secret(token: str) -> str:
    try:
        return _fernet().decrypt(token.encode()).decode()
    except InvalidToken:
        raise RuntimeError("Could not decrypt a stored secret. Was FERNET_KEY changed? Re-enter the password.")


def make_unsubscribe_token(email: str) -> str:
    # No expiry: links in old emails must keep working.
    return _unsubscribe.dumps({"e": email.lower()})


def read_unsubscribe_token(token: str) -> str | None:
    try:
        data = _unsubscribe.loads(token)
    except BadSignature:
        return None
    return data.get("e") if isinstance(data, dict) else None


def unsubscribe_url(email: str) -> str:
    return f"{settings.base_url.rstrip('/')}/u/{make_unsubscribe_token(email)}"
