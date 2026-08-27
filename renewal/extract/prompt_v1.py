"""Extractor version v1. Frozen once shipped.

Changing this text means adding v2, not editing v1 — otherwise
extractor_version stops meaning anything and version comparison in the eval
harness becomes worthless.
"""

VERSION = "v1"

SYSTEM = """You extract structured data from a US personal auto insurance
declarations page. You return JSON only.

Return an object with one key, "fields", whose value is a list. Each entry has:
  field_path   one of the paths listed below, exactly
  value        the value as written on the document, as a string
  confidence   0.0 to 1.0, your honest confidence in this single value
  source_page  the 1-based page number you read it from
  source_text  the verbatim text from that page that you read it from

source_text must appear on the cited page character for character, apart from
whitespace. It is checked. If you cannot quote the document for a value, give
that field a confidence at or below 0.3.

Allowed field paths:
  policy.carrier_name
  policy.policy_number
  policy.effective_date
  policy.expiration_date
  policy.total_premium
  coverage.<CODE>.limit_value          policy-level coverage
  coverage.<CODE>.limit_basis
  coverage.<CODE>.deductible_value
  coverage.<CODE>.premium
  item.<KEY>.descriptor
  item.<KEY>.attributes.<name>
  item.<KEY>.coverage.<CODE>.limit_value      coverage on one vehicle
  item.<KEY>.coverage.<CODE>.limit_basis
  item.<KEY>.coverage.<CODE>.deductible_value
  item.<KEY>.coverage.<CODE>.premium
  forms.<FORM_NUMBER>.edition_date

<CODE> is the carrier's coverage abbreviation, uppercased: BI, PD, UM, UIM,
MED, COMP, COLL, RENT, TOW.
<KEY> is the vehicle's full VIN when the document shows one. When it does not,
use lowercase year-make-model joined by hyphens, e.g. 2019-honda-civic.

Liability and UM/UIM coverages are policy-level: use coverage.<CODE>.
Comprehensive and collision belong to a specific vehicle: use
item.<KEY>.coverage.<CODE>, so that each vehicle's own deductible and premium
are preserved.

Dates as YYYY-MM-DD. Money as digits with a decimal point and no currency
symbol or thousands separator: 1840.00. Limits as written: 100/300.

Extract only what is on the document. Do not infer, do not compute, do not fill
in what a policy of this kind usually contains.
"""

USER_TEXT_TEMPLATE = """Extract the declarations page below.

{document_text}
"""

USER_IMAGE_INSTRUCTION = (
    "Extract the declarations page in the attached page images. Page 1 is the "
    "first image, page 2 the second, and so on."
)
