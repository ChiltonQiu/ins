# Fixtures

One JSON file per labelled document. The PDF itself lives in `evals/pdfs/`,
which is gitignored — real client documents never enter the repo.

Redact before labelling: replace names, street addresses, and full VINs with
stable stand-ins, and keep the redaction consistent between the JSON and any
notes. Values that carry no identity (premiums, limits, deductibles, dates,
coverage codes) are labelled as they appear, because those are what the
extractor is scored on.

Aim for 10 to start, 20 before trusting a version comparison.

## Keys

| Key | Required | Meaning |
|---|---|---|
| `fixture_id` | yes | Stable id for the fixture, matching the filename stem. |
| `carrier` | yes | Carrier the document came from. |
| `pdf_filename` | yes | Filename inside `evals/pdfs/`. |
| `fields` | yes | Per-field ground truth, keyed by field path. |
| `dates` | no | Every date the document states, as `{"date_value": "YYYY-MM-DD", "date_type": "..."}`. Defaults to `[]`. |
| `doc_class` | no | Expected classification. Defaults to `unknown`, which scores as a declined answer rather than a wrong one. |
| `expected_client` | no | The client the document should resolve to. Defaults to `null`, meaning it should match no existing client. |
| `billing_type` | no | `direct_bill`, `agency_bill`, or `unknown`. Defaults to `unknown`. Date scoring drops `payment_due` for a direct-bill policy, because those dates are not in the documents she receives. |

`expected_client` is a **display name, not an id**. Client ids differ between
databases, so an id in a fixture would be meaningless anywhere but the machine
it was written on; the name is resolved to an id at eval time.

Keys added after the first fixtures were written all default, so an older
fixture file stays valid without a bulk rewrite.
