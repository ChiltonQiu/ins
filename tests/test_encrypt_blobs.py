from renewal.blobstore import BlobStore
from renewal.crypto import generate_key, is_sealed, load_key
from scripts.encrypt_blobs import seal_store


def test_seals_existing_blobs_and_leaves_them_readable(tmp_path):
    store = BlobStore(tmp_path)
    digest = store.put(b"%PDF-1.7 legacy")
    key = load_key(generate_key())

    sealed, skipped = seal_store(tmp_path, key)

    assert (sealed, skipped) == (1, 0)
    assert is_sealed(store.path_for(digest).read_bytes())
    assert BlobStore(tmp_path, key=key).get(digest) == b"%PDF-1.7 legacy"


def test_is_idempotent(tmp_path):
    """Safe to re-run: the magic header says what is already done."""
    store = BlobStore(tmp_path)
    store.put(b"%PDF-1.7 legacy")
    key = load_key(generate_key())
    seal_store(tmp_path, key)
    assert seal_store(tmp_path, key) == (0, 1)


def test_the_digest_still_names_the_file(tmp_path):
    """Sealing must not change any blob's address."""
    store = BlobStore(tmp_path)
    digest = store.put(b"%PDF-1.7 legacy")
    seal_store(tmp_path, load_key(generate_key()))
    assert store.path_for(digest).exists()
