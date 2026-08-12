"""Tests for the Compass citation link-health crawler helpers."""

from scripts.compass_link_health.crawl import (
    classify_source_type,
    extract_citation_urls,
    summarize_results,
)


def test_extract_citation_urls_from_turn_snapshot_payload() -> None:
    payload = {
        "fresh_compass_turn_snapshot": {
            "result": {
                "citations": [
                    {
                        "title": "NCES Common Core of Data",
                        "url": "https://nces.ed.gov/ccd/elsi/",
                    },
                    {
                        "title": "Alpha Contract",
                        "source_url": "https://example.org/alpha.pdf",
                    },
                    {
                        "title": "duplicate",
                        "url": "https://example.org/alpha.pdf",
                    },
                ]
            }
        }
    }

    urls = extract_citation_urls(payload)

    assert [record.url for record in urls] == [
        "https://nces.ed.gov/ccd/elsi/",
        "https://example.org/alpha.pdf",
    ]


def test_classify_source_type_uses_canonical_url_patterns() -> None:
    assert classify_source_type("https://nces.ed.gov/ccd/elsi/") == "nces"
    assert classify_source_type("https://example.org/contract.pdf") == "internal_pdf"
    assert (
        classify_source_type("https://teacherquality.nctq.org/contract-database/district/x")
        == "pathfinder"
    )
    assert classify_source_type("https://district.example.org/hr") == "district_website"


def test_summarize_results_reports_per_source_type_rates() -> None:
    summary = summarize_results(
        [
            {
                "url": "https://nces.ed.gov/ccd/elsi/",
                "source_type": "nces",
                "status": "ok",
                "status_code": 200,
            },
            {
                "url": "https://nces.ed.gov/missing",
                "source_type": "nces",
                "status": "http_4xx",
                "status_code": 404,
            },
            {
                "url": "https://district.example.org/hr",
                "source_type": "district_website",
                "status": "error",
                "error": "timeout",
            },
        ]
    )

    assert summary["total_urls"] == 3
    assert summary["by_source_type"]["nces"]["total"] == 2
    assert summary["by_source_type"]["nces"]["ok_2xx"] == 1
    assert summary["by_source_type"]["nces"]["pass_rate"] == 0.5
    assert summary["by_source_type"]["district_website"]["errors"] == 1
