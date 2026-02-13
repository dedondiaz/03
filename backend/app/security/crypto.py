import base64
import os
from cryptography.fernet import Fernet


def _get_fernet() -> Fernet:
    key = os.getenv("APP_ENCRYPTION_KEY")
    if not key:
        raise RuntimeError("APP_ENCRYPTION_KEY is required")
    # must be urlsafe base64 32-byte key for Fernet
    raw = base64.urlsafe_b64decode(key.encode("utf-8"))
    if len(raw) != 32:
        raise RuntimeError("APP_ENCRYPTION_KEY must decode to 32 bytes")
    return Fernet(key.encode("utf-8"))


def encrypt_str(plaintext: str) -> str:
    return _get_fernet().encrypt(plaintext.encode("utf-8")).decode("utf-8")


def decrypt_str(ciphertext: str) -> str:
    return _get_fernet().decrypt(ciphertext.encode("utf-8")).decode("utf-8")
