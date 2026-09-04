from sqlalchemy import text

EXPECTED = {
    "ix_client_display_name_trgm",
    "ix_carrier_display_name_trgm",
    "ix_policy_policy_number",
    "ix_document_uploaded_at_desc",
    "ix_document_text_tsv",
}


def test_the_search_indexes_exist(session):
    found = {
        row[0] for row in session.execute(
            text("SELECT indexname FROM pg_indexes WHERE schemaname = 'public'")
        )
    }
    assert EXPECTED <= found


def test_the_text_index_is_gin(session):
    definition = session.execute(text(
        "SELECT indexdef FROM pg_indexes WHERE indexname = 'ix_document_text_tsv'"
    )).scalar()
    assert "gin" in definition.lower()
