# Privacy

This tool processes real client insurance documents. They contain names,
addresses, VINs, and sometimes dates of birth.

## Documents are sent to a third-party model API

Declarations pages uploaded to this tool are transmitted to Anthropic's API for
extraction and for drafting the client explanation. Documents with a text layer
are sent as text; scanned documents are sent as page images. This is the single
most important thing to disclose to an agency before they use the tool.

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
