"""Password hashing.

scrypt rather than a KDF from a dependency: the project's dependency list is
deliberately short, and the standard library's is memory-hard.
"""

import pytest

from renewal.auth.passwords import (
    DUMMY_HASH, hash_password, needs_rehash, verify_password,
)


def test_a_password_verifies_against_its_own_hash():
    encoded = hash_password("correct horse battery staple")
    assert verify_password("correct horse battery staple", encoded)


def test_a_wrong_password_does_not_verify():
    encoded = hash_password("correct horse battery staple")
    assert not verify_password("Correct horse battery staple", encoded)


def test_two_hashes_of_one_password_differ():
    """Salted. Equal hashes would mean two accounts with the same password
    are visibly the same account in a database dump."""
    assert hash_password("hunter2 hunter2") != hash_password("hunter2 hunter2")


def test_the_encoded_form_carries_its_parameters():
    encoded = hash_password("hunter2 hunter2")
    scheme, n, r, p, salt, digest = encoded.split("$")
    assert scheme == "scrypt"
    assert (int(n), int(r), int(p)) == (2 ** 15, 8, 1)
    assert salt and digest


def test_a_hash_at_current_parameters_does_not_need_rehashing():
    assert not needs_rehash(hash_password("hunter2 hunter2"))


def test_a_cheaper_hash_needs_rehashing():
    """Cost gets raised over time. An old hash must still verify, and must be
    replaced the next time the password is available in plaintext."""
    encoded = hash_password("hunter2 hunter2")
    scheme, n, r, p, salt, digest = encoded.split("$")
    cheaper = "$".join([scheme, str(2 ** 14), r, p, salt, digest])
    assert needs_rehash(cheaper)


def test_a_cheaper_hash_still_verifies():
    from renewal.auth import passwords

    salt = b"0123456789abcdef"
    dk = passwords._derive("hunter2 hunter2", salt, 2 ** 14, 8, 1)
    cheaper = "$".join(
        ["scrypt", str(2 ** 14), "8", "1", passwords._b64(salt),
         passwords._b64(dk)]
    )
    assert verify_password("hunter2 hunter2", cheaper)


@pytest.mark.parametrize(
    "encoded",
    ["", "not-a-hash", "scrypt$x$8$1$aaaa$bbbb", "scrypt$32768$8$1$aaaa",
     "bcrypt$32768$8$1$aaaa$bbbb"],
)
def test_a_malformed_hash_is_false_not_an_exception(encoded):
    """A corrupt row must fail the login, not 500 the login page."""
    assert not verify_password("hunter2 hunter2", encoded)


def test_the_dummy_hash_verifies_against_nothing():
    """It exists to burn the same time as a real verification when no account
    matches, so response timing does not disclose which addresses exist."""
    assert not verify_password("hunter2 hunter2", DUMMY_HASH)
    assert not needs_rehash(DUMMY_HASH)
