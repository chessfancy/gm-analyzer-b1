from chessgrandmaster.b2.json_codec import (
    canonical_json_bytes,
    canonical_json_sha256,
)


def test_canonical_json_is_order_independent():
    left = {"b": 2, "a": {"y": 2, "x": 1}}
    right = {"a": {"x": 1, "y": 2}, "b": 2}

    assert canonical_json_bytes(left) == canonical_json_bytes(right)
    assert canonical_json_sha256(left) == canonical_json_sha256(right)


def test_canonical_json_is_utf8_and_compact():
    payload = {"name": "Nguyễn", "value": 1}

    assert canonical_json_bytes(payload) == (
        '{"name":"Nguyễn","value":1}'.encode("utf-8")
    )


def test_canonical_json_rejects_non_finite_numbers():
    try:
        canonical_json_bytes({"value": float("nan")})
    except ValueError:
        pass
    else:
        raise AssertionError("non-finite JSON values must be rejected")
