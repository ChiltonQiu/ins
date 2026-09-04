import pytest

from renewal.crypto import (
    MAGIC, generate_key, is_sealed, load_key, seal, unseal,
)


def _key():
    return load_key(generate_key())


def test_round_trip():
    key = _key()
    assert unseal(seal(b"hello", key), key) == b"hello"


def test_sealed_bytes_do_not_contain_the_plaintext():
    key = _key()
    assert b"hello" not in seal(b"hello", key)


def test_sealed_bytes_carry_the_magic_header():
    assert seal(b"x", _key()).startswith(MAGIC)


def test_is_sealed_distinguishes_a_plain_pdf():
    assert not is_sealed(b"%PDF-1.7\n...")
    assert is_sealed(seal(b"%PDF-1.7\n...", _key()))


def test_two_seals_of_the_same_plaintext_differ():
    """A fresh nonce each time, so identical documents are not identifiable by
    their ciphertext on disk."""
    key = _key()
    assert seal(b"same", key) != seal(b"same", key)


def test_a_wrong_key_fails_loudly():
    sealed = seal(b"secret", _key())
    with pytest.raises(Exception):
        unseal(sealed, _key())


def test_tampered_ciphertext_fails_loudly():
    key = _key()
    sealed = bytearray(seal(b"secret", key))
    sealed[-1] ^= 0xFF
    with pytest.raises(Exception):
        unseal(bytes(sealed), key)


def test_load_key_of_none_is_none():
    assert load_key(None) is None
    assert load_key("") is None
