import hashlib

import pytest

from chessgrandmaster.b2.storage import LocalObjectStore


def test_local_store_round_trip_and_checksum(tmp_path):
    source = tmp_path / "source.pgn"
    source.write_bytes(b'[Result "*"]\n\n*\n')
    store = LocalObjectStore(tmp_path / "objects")

    stat = store.put_file("raw/abc/original.pgn", source)

    assert stat.key == "raw/abc/original.pgn"
    assert stat.size == source.stat().st_size
    assert stat.sha256 == hashlib.sha256(source.read_bytes()).hexdigest()
    assert store.exists("raw/abc/original.pgn")
    assert store.stat("raw/abc/original.pgn") == stat

    restored = tmp_path / "restored.pgn"
    restored_stat = store.get_file("raw/abc/original.pgn", restored)

    assert restored.read_bytes() == source.read_bytes()
    assert restored_stat == stat
    assert store.list("raw") == ["raw/abc/original.pgn"]


def test_local_store_reuses_exact_bytes_without_replacing_object(tmp_path):
    store = LocalObjectStore(tmp_path / "objects")
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.write_bytes(b"same bytes")
    second.write_bytes(b"same bytes")

    original = store.put_file("immutable/key", first)
    reused = store.put_file("immutable/key", second)

    assert reused == original
    assert store.stat("immutable/key") == original


def test_local_store_refuses_overwrite_with_different_bytes(tmp_path):
    store = LocalObjectStore(tmp_path / "objects")
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.write_bytes(b"a")
    second.write_bytes(b"b")

    store.put_file("immutable/key", first)

    with pytest.raises(ValueError, match="immutable"):
        store.put_file("immutable/key", second)

    assert store.get_file("immutable/key", tmp_path / "restored").sha256 == (
        hashlib.sha256(b"a").hexdigest()
    )


@pytest.mark.parametrize(
    "unsafe_key",
    [
        "../escape",
        "nested/../../escape",
        "/absolute",
        r"nested\file",
        "C:/escape",
    ],
)
def test_local_store_rejects_unsafe_logical_keys(tmp_path, unsafe_key):
    source = tmp_path / "source"
    source.write_bytes(b"content")
    store = LocalObjectStore(tmp_path / "objects")

    with pytest.raises(ValueError):
        store.put_file(unsafe_key, source)

    with pytest.raises(ValueError):
        store.exists(unsafe_key)


def test_local_store_lists_keys_deterministically_by_forward_slash(tmp_path):
    store = LocalObjectStore(tmp_path / "objects")
    for name, content in (("b.pgn", b"b"), ("a.pgn", b"a")):
        source = tmp_path / name
        source.write_bytes(content)
        store.put_file(f"prefix/{name}", source)

    assert store.list("") == ["prefix/a.pgn", "prefix/b.pgn"]
    assert store.list("prefix/") == ["prefix/a.pgn", "prefix/b.pgn"]
    assert store.list("missing/") == []
