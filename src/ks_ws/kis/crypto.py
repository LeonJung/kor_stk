"""AES helpers for KIS realtime account-notification frames.

Body of an encrypted KIS WS frame is base64 ciphertext under
AES-256-CBC. The key + iv arrive in the JSON subscription ack as
**raw ASCII strings** of the standard AES sizes — e.g. a 32-character
ASCII key for AES-256, a 16-character ASCII IV. We treat them as such
without trying to decode base64 (the forms are ambiguous at certain
lengths, and observed KIS responses use ASCII directly).

PKCS7 padding is stripped from the decrypted plaintext before
returning a UTF-8 string.
"""

import base64

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes


def _decode_key_or_iv(s: str) -> bytes:
    """Decode a KIS-supplied key/iv ASCII string into bytes."""
    return s.encode("ascii")


def _strip_pkcs7(plain: bytes) -> bytes:
    if not plain:
        return plain
    pad_len = plain[-1]
    if 1 <= pad_len <= 16 and plain[-pad_len:] == bytes([pad_len]) * pad_len:
        return plain[:-pad_len]
    return plain  # not padded — return as-is


def aes_cbc_decrypt(ciphertext_b64: str, key: str, iv: str) -> str:
    """Decrypt a KIS WS encrypted frame body.

    ``ciphertext_b64`` is base64-encoded ciphertext as it arrives in the
    WS frame. ``key`` and ``iv`` come from the subscription ack JSON.
    """
    key_bytes = _decode_key_or_iv(key)
    iv_bytes = _decode_key_or_iv(iv)
    if len(iv_bytes) != 16:
        raise ValueError(f"iv must be 16 bytes, got {len(iv_bytes)}")
    if len(key_bytes) not in (16, 24, 32):
        raise ValueError(f"key must be 16/24/32 bytes, got {len(key_bytes)}")
    ciphertext = base64.b64decode(ciphertext_b64)
    cipher = Cipher(algorithms.AES(key_bytes), modes.CBC(iv_bytes))
    decryptor = cipher.decryptor()
    plain = decryptor.update(ciphertext) + decryptor.finalize()
    return _strip_pkcs7(plain).decode("utf-8")
