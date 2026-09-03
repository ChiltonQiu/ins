# renewal

Insurance renewal comparison and record layer.

## System dependencies

Beyond the Python dependencies in `pyproject.toml`:

| Dependency | Needed for | Install |
|---|---|---|
| PostgreSQL 15+ | Everything. `pgcrypto` and `pg_trgm` are created by migrations. | `pacman -S postgresql` / `apt install postgresql` |
| Tesseract | OCR of scanned pages during text extraction. | `pacman -S tesseract tesseract-data-eng` / `apt install tesseract-ocr` |

Without Tesseract a scanned document is still stored, hashed, and linked to a
client, but it has no text layer to extract and no OCR fallback — so it is
searchable only by filename, and no dates are pulled from it. The ingest path
raises rather than recording a scanned page as legitimately blank.

OCR runs locally. No scanned page is transmitted to a model provider for text
extraction, whatever `PROVIDER` is set to.

## Setup

```bash
python -m venv .venv && .venv/bin/pip install -e '.[dev]'
cp .env.example .env    # then fill in the values
.venv/bin/alembic upgrade head
```

## Tests

```bash
.venv/bin/pytest              # excludes tests marked `eval`
.venv/bin/pytest -m eval      # hits the real model provider API
```

Tests need a PostgreSQL database; the default is `renewal_test`, overridable
with `TEST_DATABASE_URL`.
