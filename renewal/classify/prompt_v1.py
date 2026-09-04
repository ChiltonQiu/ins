"""The document classification prompt.

Coarse and low-stakes on purpose. The label decides routing and display; it
never decides whether a document is stored, searchable, or date-extracted.
That separation is why "unknown" costs almost nothing and a confident wrong
guess costs more than it looks like it should.
"""

VERSION = "classify-v1"

SYSTEM = """\
You label insurance documents by type from the first page.

Answer "unknown" whenever you are not confident. An unknown label is a correct
and useful answer; a confident wrong label is worse than no label at all,
because it sends the document down the wrong path silently.

Choose exactly one of: declarations, endorsement, cancellation_notice,
non_renewal_notice, invoice, id_card, loss_run, inspection_report, quote,
correspondence, unknown.

Answer with JSON only: {"doc_class": "...", "confidence": 0.0}
"""

USER_TEMPLATE = """First page of the document:

{page_text}
"""
