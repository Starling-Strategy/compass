"""Tests for MVP 5 deterministic citation evidence artifacts."""

from compass_backend.artifacts import CitationSource, citation_ref_from_source


def test_source_name_parses_title_document_type_and_page_reference() -> None:
    citation = citation_ref_from_source(
        CitationSource(
            source_name=(
                "Atlanta Public Schools. GA. (2024-2025). Contract. "
                "Employee Handbook. p. 26."
            ),
            source_url="https://example.org/atlanta-contract.pdf",
            document_type=None,
            academic_year="2024 - 2025",
            district_id=10,
            citation_order=1,
        ),
        marker=1,
    )

    assert citation.marker == 1
    assert citation.title == "Atlanta Public Schools Contract, 2024-2025"
    assert citation.document_type == "Contract"
    assert citation.page_number == 26
    assert citation.page_ref == "p. 26"
    assert citation.url == "https://example.org/atlanta-contract.pdf"
    assert citation.academic_year == "2024 - 2025"
    assert citation.district_id == 10
    assert citation.metadata == {"citation_order": 1}


def test_blank_source_name_still_becomes_safe_document_level_citation() -> None:
    citation = citation_ref_from_source(
        CitationSource(
            source_name=" ",
            source_url="https://example.org/source.pdf",
            document_type="Contract",
            academic_year="2024 - 2025",
            district_id=11,
            citation_order=None,
        ),
        marker=2,
    )

    assert citation.marker == 2
    assert citation.title == "example.org"
    assert citation.document_type == "Contract"
    assert citation.page_number is None
    assert citation.page_ref is None
    assert citation.url == "https://example.org/source.pdf"
    assert citation.academic_year == "2024 - 2025"
    assert citation.district_id == 11
    assert citation.metadata == {}


def test_document_type_only_source_uses_document_type_not_generic_source() -> None:
    citation = citation_ref_from_source(
        CitationSource(
            source_name=" ",
            source_url=None,
            document_type="Board Policy",
            academic_year="2024 - 2025",
            district_id=11,
            citation_order=None,
        ),
        marker=2,
    )

    assert citation.title == "Board Policy"


def test_unparseable_source_name_uses_raw_title_without_losing_metadata() -> None:
    citation = citation_ref_from_source(
        CitationSource(
            source_name="Unstructured archive memo",
            source_url="https://example.org/archive-memo.pdf",
            document_type="Other",
            academic_year="2023 - 2024",
            district_id=12,
            citation_order=3,
        ),
        marker=3,
    )

    assert citation.marker == 3
    assert citation.title == "Unstructured archive memo"
    assert citation.document_type == "Other"
    assert citation.page_number is None
    assert citation.page_ref is None
    assert citation.metadata == {"citation_order": 3}
