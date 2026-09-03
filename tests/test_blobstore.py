import hashlib

import pytest

from renewal.blobstore import BlobNotFound, BlobStore
from renewal.crypto import generate_key, is_sealed, load_key

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


def test_hash_is_of_the_plaintext_so_dedup_is_unchanged(tmp_path):
    """The same document stored with and without a key gets the same digest."""
    plain = BlobStore(tmp_path / "a")
    sealed = BlobStore(tmp_path / "b", key=load_key(generate_key()))
    assert plain.put(b"%PDF-1.7 doc") == sealed.put(b"%PDF-1.7 doc")


def test_bytes_on_disk_are_sealed_when_a_key_is_set(tmp_path):
    store = BlobStore(tmp_path, key=load_key(generate_key()))
    digest = store.put(b"%PDF-1.7 doc")
    on_disk = store.path_for(digest).read_bytes()
    assert is_sealed(on_disk)
    assert b"%PDF-1.7 doc" not in on_disk


def test_get_returns_the_plaintext(tmp_path):
    store = BlobStore(tmp_path, key=load_key(generate_key()))
    digest = store.put(b"%PDF-1.7 doc")
    assert store.get(digest) == b"%PDF-1.7 doc"


def test_a_keyed_store_reads_a_blob_written_before_encryption(tmp_path):
    """Existing stores are not sealed yet; reads must keep working during the
    migration window."""
    unkeyed = BlobStore(tmp_path)
    digest = unkeyed.put(b"%PDF-1.7 legacy")
    keyed = BlobStore(tmp_path, key=load_key(generate_key()))
    assert keyed.get(digest) == b"%PDF-1.7 legacy"


def test_extension_selects_a_separate_file(tmp_path):
    store = BlobStore(tmp_path)
    digest = store.put(b"raw mime here", ext="eml")
    assert store.path_for(digest, ext="eml").suffix == ".eml"
    assert store.get(digest, ext="eml") == b"raw mime here"


def test_default_extension_is_pdf_so_existing_paths_are_unchanged(tmp_path):
    store = BlobStore(tmp_path)
    digest = store.put(b"%PDF-1.7")
    assert store.path_for(digest).suffix == ".pdf"


def test_a_bad_extension_is_rejected(tmp_path):
    """The extension reaches a filesystem path, so it is validated, not trusted."""
    store = BlobStore(tmp_path)
    with pytest.raises(ValueError):
        store.path_for("a" * 64, ext="../../etc/passwd")
