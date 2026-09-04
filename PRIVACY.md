# Privacy

This tool processes real client insurance documents. They contain names,
addresses, VINs, and sometimes dates of birth.

## Where documents go depends on PROVIDER

This tool sends declarations pages to a model for extraction and for drafting
the client explanation. Documents with a text layer are sent as text; scanned
documents are sent as page images. **Which model, and therefore whether the
document leaves this machine, is set by `PROVIDER` in `.env`.**

Third-party providers — the document is transmitted to a company outside your
control, subject to that company's terms and retention policy:

| `PROVIDER` | Documents are sent to |
|---|---|
| `anthropic` | Anthropic |
| `openai` | OpenAI |
| `grok` | xAI |
| `huggingface` | HuggingFace, and the inference provider it routes to |

Local by default — nothing leaves the host unless `LLM_BASE_URL` redirects it:

| `PROVIDER` | Documents are sent to |
|---|---|
| `ollama` | a model running on this machine |
| `custom` | whatever `LLM_BASE_URL` points at — local if that is a local address, third-party if it is not |

`LLM_BASE_URL` overrides the address for every provider **except**
`anthropic`, which always reaches Anthropic's own API. So for any other value
above, `PROVIDER` names the default destination rather than a guarantee, and
`LLM_BASE_URL` can point a nominally local provider at a remote host. Read both
before answering the question for a running install.

To answer this for a specific install, read `PROVIDER` in its `.env`. Extractions
recorded since this change also carry the provider in `extraction.model_id`, so
the question can be answered retrospectively for those; rows written before it
carry a model name only and cannot be attributed from the database alone.

## What is stored, and where

This tool now holds the whole book, not two files at a time. Everything below
is stored for every document that comes in:

- The original bytes, on the local filesystem, named by their sha256 hash.
- The full text of every page.
- Every date found, with the exact source text it was read from and the page
  it was read on.
- Inbound message bodies, where mail intake is in use.
- Extracted values, corrections, comparisons, and drafts, in a local
  PostgreSQL database.

Nothing is deleted or overwritten, and nothing expires. Records are retained
indefinitely, which is deliberate: state record-retention rules and E&O defense
both depend on the file being complete. Removing a document, its text, or its
dates is a manual act.

## OCR runs on this machine

Scanned pages are read by Tesseract on the host. **No page image is ever
transmitted to a model provider for text extraction**, whatever `PROVIDER` is
set to. The provider table above still governs structured field extraction,
drafting, and the date-typing pass.

## What the model sees for dates

The first `DATE_PAGES` pages of text of **every** document are sent to
`DATE_MODEL`. This is a larger exposure than structured extraction, which only
ever ran on documents selected for a renewal comparison — date extraction runs
on the whole archive, including documents nobody asked a question about.

`DATE_PAGES` is the control. Lowering it narrows what leaves the host;
`PROVIDER=ollama` keeps it on the machine entirely.

## Encryption at rest

Setting `BLOB_ENCRYPTION_KEY` seals stored documents with AES-GCM. Generate one
with:

```bash
python -c "from renewal.crypto import generate_key; print(generate_key())"
```

What this protects against, stated plainly: a stolen backup, a copied blob
directory, a disk that leaves the building. **It does not protect against a
compromised host.** The key lives in `.env` beside the data it encrypts, so
anyone who can read the blob directory on a running machine can usually read
the key too. Back the key up somewhere the blob backups are not, or the backup
and its key travel together and the encryption buys nothing.

If `BLOB_ENCRYPTION_KEY` is unset, documents are stored unencrypted and the
application logs a warning naming the directory at startup.

## The calendar subscription link

The `.ics` feed URL contains a token and nothing else. Anyone holding that URL
can read every client name and every deadline in the book, with no sign-in.
It is stored in the calendar settings of every device subscribed to it.

Regenerate it from `/settings` whenever it may have been shared, forwarded, or
left on a device no longer in use. Regenerating breaks every existing
subscription immediately; they have to be re-added.

## What is never done

- No document is ever sent to a client automatically. Every outbound explanation
  is a draft that a licensed human reads and edits.
- Extracted content is never written to application logs. Logs contain document
  ids and blob hashes only.
- Real PDFs and the blob store are excluded from version control.
