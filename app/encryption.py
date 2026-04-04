from __future__ import annotations

import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy.types import Text, TypeDecorator

from .config import settings


def _build_key() -> bytes:
    if settings.field_encryption_key:
        return settings.field_encryption_key.encode("utf-8")
    digest = hashlib.sha256(settings.secret_key.encode("utf-8")).digest()
    return base64.urlsafe_b64encode(digest)


fernet = Fernet(_build_key())


class EncryptedText(TypeDecorator):
    impl = Text
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None or value == "":
            return value
        return fernet.encrypt(str(value).encode("utf-8")).decode("utf-8")

    def process_result_value(self, value, dialect):
        if value is None or value == "":
            return value
        try:
            return fernet.decrypt(value.encode("utf-8")).decode("utf-8")
        except (InvalidToken, ValueError):
            return value
