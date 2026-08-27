import hashlib

from renewal.blobstore import BlobNotFound, BlobStore
import pytest

PDF = b"%PDF-1.7 fake bytes"


def test_put_returns_sha256_hex(tmp_path):
    store = BlobStore(tmp_path)
    assert store.put(PDF) == hashlib.sha256(PDF).hexdigest()


def test_put_writes_two_level_sharded_path(tmp_path):
    store = BlobStore(tmp_path)
    digest = store.put(PDF)
    expected = tmp_path / digest[:2] / digest[2:4] / f"{digest}.pdf"
    assert expected.is_file()


def test_put_is_idempotent_and_does_not_rewrite(tmp_path):
    store = BlobStore(tmp_path)
    digest = store.put(PDF)
    before = store.path_for(digest).stat().st_mtime_ns
    assert store.put(PDF) == digest
    assert store.path_for(digest).stat().st_mtime_ns == before


def test_get_round_trips(tmp_path):
    store = BlobStore(tmp_path)
    assert store.get(store.put(PDF)) == PDF


def test_get_unknown_hash_raises(tmp_path):
    with pytest.raises(BlobNotFound):
        BlobStore(tmp_path).get("0" * 64)


def test_path_for_rejects_hash_containing_dotdot_slash(tmp_path):
    with pytest.raises(ValueError):
        BlobStore(tmp_path).path_for("../../etc/passwd")


def test_get_rejects_absolute_path_as_hash(tmp_path):
    with pytest.raises(ValueError):
        BlobStore(tmp_path).get("/etc/passwd")


def test_get_rejects_short_hash(tmp_path):
    with pytest.raises(ValueError):
        BlobStore(tmp_path).get("0" * 63)


def test_get_rejects_non_hex_hash(tmp_path):
    with pytest.raises(ValueError):
        BlobStore(tmp_path).get("g" * 64)


def test_get_still_works_for_valid_hash(tmp_path):
    store = BlobStore(tmp_path)
    digest = store.put(PDF)
    assert store.get(digest) == PDF
