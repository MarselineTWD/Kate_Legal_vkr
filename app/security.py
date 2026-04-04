from __future__ import annotations

import base64
import hashlib
import hmac
import os


ITERATIONS = 260_000


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, ITERATIONS)
    return "pbkdf2_sha256$%d$%s$%s" % (
        ITERATIONS,
        base64.urlsafe_b64encode(salt).decode("ascii"),
        base64.urlsafe_b64encode(dk).decode("ascii"),
    )


def verify_password(plain_password: str, stored_hash: str) -> bool:
    try:
        algorithm, iterations, salt_b64, hash_b64 = stored_hash.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        salt = base64.urlsafe_b64decode(salt_b64.encode("ascii"))
        expected = base64.urlsafe_b64decode(hash_b64.encode("ascii"))
        calculated = hashlib.pbkdf2_hmac("sha256", plain_password.encode("utf-8"), salt, int(iterations))
        return hmac.compare_digest(calculated, expected)
    except Exception:
        return False
