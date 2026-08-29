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

`LLM_BASE_URL` overrides the address for **any** provider, so a `PROVIDER` value
above describes the default destination, not a guarantee. Read `LLM_BASE_URL`
alongside `PROVIDER` before answering the question for a running install.

To answer this for a specific install, read `PROVIDER` in its `.env`. Extractions
recorded since this change also carry the provider in `extraction.model_id`, so
the question can be answered retrospectively for those; rows written before it
carry a model name only and cannot be attributed from the database alone.

## What is stored, and where

- Original PDFs are stored on the local filesystem, named by their sha256 hash.
- Extracted values, corrections, comparisons, and drafts are stored in a local
  PostgreSQL database.
- Nothing is deleted or overwritten. Records are retained indefinitely, which is
  deliberate: state record-retention rules and E&O defense both depend on the
  file being complete.

## What is never done

- No document is ever sent to a client automatically. Every outbound explanation
  is a draft that a licensed human reads and edits.
- Extracted content is never written to application logs. Logs contain document
  ids and blob hashes only.
- Real PDFs and the blob store are excluded from version control.
