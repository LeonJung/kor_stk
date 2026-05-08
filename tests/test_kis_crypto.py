import base64

import pytest
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from ks_ws.kis.crypto import _decode_key_or_iv, _strip_pkcs7, aes_cbc_decrypt


def _encrypt(plain: str, key: bytes, iv: bytes) -> str:
    """Helper for tests — AES-256-CBC encrypt + base64. PKCS7 padded."""
    plain_b = plain.encode("utf-8")
    pad_len = 16 - (len(plain_b) % 16)
    padded = plain_b + bytes([pad_len]) * pad_len
    enc = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
    ct = enc.update(padded) + enc.finalize()
    return base64.b64encode(ct).decode("ascii")


def test_round_trip_with_ascii_key_and_iv():
    """KIS appears to send the key/iv as plain ASCII strings."""
    key_str = "0123456789abcdef0123456789abcdef"  # 32 ASCII chars
    iv_str = "fedcba9876543210"  # 16 ASCII chars
    plaintext = "005930^102030^70000^10"
    ct_b64 = _encrypt(plaintext, key_str.encode(), iv_str.encode())
    out = aes_cbc_decrypt(ct_b64, key_str, iv_str)
    assert out == plaintext


def test_decode_treats_input_as_raw_ascii():
    """KIS sends raw ASCII strings — we trust the byte length matches
    an AES size and let the cipher reject otherwise."""
    s = "0123456789abcdef0123456789abcdef"  # 32 chars → AES-256
    assert _decode_key_or_iv(s) == s.encode("ascii")


def test_strip_pkcs7_removes_valid_padding():
    plain_padded = b"hello\x03\x03\x03"
    assert _strip_pkcs7(plain_padded) == b"hello"


def test_strip_pkcs7_leaves_unpadded_data():
    """If padding bytes don't match the expected pattern, leave alone."""
    plain = b"abcdef"
    assert _strip_pkcs7(plain) == b"abcdef"


def test_invalid_key_size_rejected():
    with pytest.raises(ValueError):
        aes_cbc_decrypt("dummy", "tooshort", "fedcba9876543210")


def test_invalid_iv_size_rejected():
    with pytest.raises(ValueError):
        aes_cbc_decrypt(
            "dummy",
            "0123456789abcdef0123456789abcdef",
            "shortIV",
        )
