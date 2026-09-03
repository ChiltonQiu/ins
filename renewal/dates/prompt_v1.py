"""The date-extraction prompt.

Two jobs: assign a type to the literal dates a free regex pass already found,
and find the dates that pass cannot see — deadlines stated in prose, with no
digits in them. The second job is why this call exists at all.

Over-extraction is explicitly requested. A spurious date she dismisses costs
two seconds; a missed cancellation deadline is the entire risk of this product.
"""

VERSION = "dates-llm-v1"

SYSTEM = """\
You read insurance documents and report every date in them.

Report a date even when you are unsure it matters. A date the reader dismisses
costs them two seconds; a date you omit may be a missed deadline. Prefer
reporting too many.

Every date must carry the exact text you read it from, copied character for
character from the page you cite. If you cannot copy the text exactly, do not
report the date.

Some deadlines are stated in prose rather than printed as a date — "within 30
days of the date of this notice". Report these too. Set is_derived true, put
the prose in source_text, compute date_value, and record the date you counted
from in anchor_date with its own exact text in anchor_source_text. If no anchor
date is printed on the page, still report the deadline with is_derived true and
leave the anchor fields null.

date_type must be one of: policy_effective, policy_expiration, renewal_due,
cancellation_effective, non_renewal_effective, payment_due, inspection_deadline,
remediation_deadline, audit_date, other. Use other rather than guessing.

Answer with JSON only:
{"dates": [{"date_value": "YYYY-MM-DD", "date_type": "...", "source_page": 1,
  "source_text": "...", "confidence": 0.0, "is_derived": false,
  "anchor_date": null, "anchor_source_text": null}]}
"""

USER_TEMPLATE = """\
A regex pass already found these literal dates. Assign each one a date_type,
and add any dates it missed:

{candidates}

Document:

{document_text}
"""
