"""
Cryptographic module for LinkPilot Data Engine.
Provides AES-256-GCM encryption and decryption for sensitive credentials and tokens.
The master key is strictly retrieved from the environment variable LINKPILOT_MASTER_KEY.
"""
import os
import base64
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives import hashes
from app.config.settings import settings


def _derive_key(master_secret: str) -> bytes:
    """Derive a fixed 256-bit key from the master secret using HKDF-SHA256."""
    hkdf = HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=b"linkpilot-salt-v1",
        info=b"credential-encryption",
    )
    return hkdf.derive(master_secret.encode("utf-8"))


def encrypt_secret(plaintext: str) -> str:
    """
    Encrypt plaintext string using AES-256-GCM.
    Returns base64-encoded string: nonce (12 bytes) + ciphertext + tag.
    """
    key = _derive_key(settings.LINKPILOT_MASTER_KEY)
    aesgcm = AESGCM(key)
    nonce = os.urandom(12)
    ciphertext = aesgcm.encrypt(nonce, plaintext.encode("utf-8"), None)
    return base64.b64encode(nonce + ciphertext).decode("utf-8")


def decrypt_secret(encrypted_b64: str) -> str:
    """
    Decrypt base64-encoded string using AES-256-GCM.
    Extracts nonce (first 12 bytes) and decrypts payload.
    """
    key = _derive_key(settings.LINKPILOT_MASTER_KEY)
    aesgcm = AESGCM(key)
    data = base64.b64decode(encrypted_b64.encode("utf-8"))
    nonce = data[:12]
    ciphertext = data[12:]
    decrypted_bytes = aesgcm.decrypt(nonce, ciphertext, None)
    return decrypted_bytes.decode("utf-8")
