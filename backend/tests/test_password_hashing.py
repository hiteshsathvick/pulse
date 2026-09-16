from pulse.core.security import hash_password, verify_password


def test_hash_uses_argon2id() -> None:
    """DoD: Argon2 specifically -- not some other scheme."""
    assert hash_password("correct horse battery staple").startswith("$argon2id$")


def test_correct_password_verifies() -> None:
    password_hash = hash_password("correct horse battery staple")
    assert verify_password("correct horse battery staple", password_hash)


def test_wrong_password_does_not_verify() -> None:
    password_hash = hash_password("correct horse battery staple")
    assert not verify_password("wrong password", password_hash)


def test_each_hash_is_independently_salted() -> None:
    first = hash_password("same password")
    second = hash_password("same password")
    assert first != second
    assert verify_password("same password", first)
    assert verify_password("same password", second)
