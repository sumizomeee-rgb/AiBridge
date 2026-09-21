from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
from pathlib import Path
from typing import Any

from cryptography.fernet import Fernet, InvalidToken


class SecretBox:
    def __init__(self, key_path: Path):
        key_path.parent.mkdir(parents=True, exist_ok=True)
        if not key_path.exists():
            key_path.write_bytes(Fernet.generate_key())
            try:
                os.chmod(key_path, 0o600)
            except OSError:
                pass
        self._fernet = Fernet(key_path.read_bytes().strip())

    def encrypt_json(self, value: dict[str, Any] | None) -> str | None:
        if not value:
            return None
        raw = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()
        return self._fernet.encrypt(raw).decode()

    def decrypt_json(self, value: str | None) -> dict[str, Any]:
        if not value:
            return {}
        try:
            return json.loads(self._fernet.decrypt(value.encode()).decode())
        except (InvalidToken, ValueError, json.JSONDecodeError):
            return {}


def new_gateway_token() -> str:
    return "ab_" + secrets.token_urlsafe(32)


def hash_token(token: str, salt: bytes | None = None) -> tuple[str, str]:
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", token.encode(), salt, 180_000)
    return base64.urlsafe_b64encode(salt).decode(), base64.urlsafe_b64encode(digest).decode()


def verify_token(token: str, salt_text: str, digest_text: str) -> bool:
    salt = base64.urlsafe_b64decode(salt_text.encode())
    _, candidate = hash_token(token, salt)
    return hmac.compare_digest(candidate, digest_text)
