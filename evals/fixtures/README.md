# Fixtures

One JSON file per labelled document. The PDF itself lives in `evals/pdfs/`,
which is gitignored — real client documents never enter the repo.

Redact before labelling: replace names, street addresses, and full VINs with
stable stand-ins, and keep the redaction consistent between the JSON and any
notes. Values that carry no identity (premiums, limits, deductibles, dates,
coverage codes) are labelled as they appear, because those are what the
extractor is scored on.

Aim for 10 to start, 20 before trusting a version comparison.
