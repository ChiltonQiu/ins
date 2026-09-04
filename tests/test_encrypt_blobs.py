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

def test_a_non_blob_file_is_left_untouched(tmp_path):
    """Only files named {64 hex}.{ext} are blobs. Anything else under the root
    -- a README, a .gitkeep, a stray index file -- must not be overwritten
    with ciphertext of its own bytes, and must not be counted as sealed."""
    readme = tmp_path / "README.md"
    readme.write_bytes(b"this directory holds sealed insurance documents")
    store = BlobStore(tmp_path)
    store.put(b"%PDF-1.7 legacy")
    key = load_key(generate_key())

    sealed, skipped = seal_store(tmp_path, key)

    assert sealed == 1
    assert readme.read_bytes() == b"this directory holds sealed insurance documents"


def test_a_blob_stored_with_ext_tmp_is_still_sealed(tmp_path):
    """A blob's filename can legitimately end in .tmp (BlobStore.put allows
    ext='tmp'). It must not be mistaken for the script's own scratch file and
    silently skipped."""
    store = BlobStore(tmp_path)
    digest = store.put(b"%PDF-1.7 legacy", ext="tmp")
    key = load_key(generate_key())

    sealed, skipped = seal_store(tmp_path, key)

    assert (sealed, skipped) == (1, 0)
    assert is_sealed(store.path_for(digest, ext="tmp").read_bytes())


def test_mixed_store_counts_sealed_and_already_sealed_separately(tmp_path):
    key = load_key(generate_key())
    plain_store = BlobStore(tmp_path)
    sealed_store = BlobStore(tmp_path, key=key)
    sealed_store.put(b"%PDF-1.7 already sealed")
    plain_store.put(b"%PDF-1.7 still plaintext")

    assert seal_store(tmp_path, key) == (1, 1)


def test_an_empty_store_returns_zero_zero_and_does_not_raise(tmp_path):
    key = load_key(generate_key())
    assert seal_store(tmp_path, key) == (0, 0)
