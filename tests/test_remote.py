"""API response boundary regressions without external HTTP mocks."""

import pytest

from nopt.remote import decode_response, publication_matches


@pytest.mark.parametrize(
    "raw", [b"", b" ", b"null", b"  null\n", b"not JSON", b"true", b"123", b'"scalar"']
)
def test_successful_response_requires_nonnull_json(raw: bytes) -> None:
    """Reject malformed successful bodies so only HTTP 404 can mean absence."""
    with pytest.raises(ValueError, match="successful API response"):
        decode_response(raw)
    return


def test_successful_json_is_preserved() -> None:
    """Preserve a decoded record for policy checks instead of treating it as missing."""
    decoded = decode_response(b'{"urls": []}')
    assert decoded == {"urls": []}
    return


@pytest.mark.parametrize("digests", [None, [], {}, {"sha256": None}, {"sha256": []}])
def test_malformed_digest_is_a_validation_error(digests: object) -> None:
    """Reject invalid nested digest shapes without an uncaught attribute error."""
    data = {"urls": [{"filename": "release.whl", "digests": digests}]}
    with pytest.raises(ValueError, match="digest metadata"):
        publication_matches(data, {"release.whl": "a" * 64})
    return
