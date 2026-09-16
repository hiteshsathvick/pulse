import uuid
from datetime import timedelta

import pytest

from pulse.core.security import InvalidAccessToken, create_access_token, decode_access_token


def test_round_trips_the_user_id() -> None:
    user_id = uuid.uuid4()
    token = create_access_token(user_id)
    assert decode_access_token(token) == user_id


def test_expired_token_is_rejected() -> None:
    """DoD: token expiry. Crafts an already-expired token directly rather
    than sleeping or mocking time."""
    token = create_access_token(uuid.uuid4(), expires_delta=timedelta(seconds=-1))
    with pytest.raises(InvalidAccessToken):
        decode_access_token(token)


def test_garbage_token_is_rejected() -> None:
    with pytest.raises(InvalidAccessToken):
        decode_access_token("not-a-real-token")


def test_tampered_signature_is_rejected() -> None:
    token = create_access_token(uuid.uuid4())
    last_char = token[-1]
    tampered = token[:-1] + ("A" if last_char != "A" else "B")
    with pytest.raises(InvalidAccessToken):
        decode_access_token(tampered)
