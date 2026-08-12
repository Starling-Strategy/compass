"""Vespa application package definition using PyVespa.

Defines the 'district_doc' schema with:
- BM25 full-text search on full_text and doc_summary
- ANN (HNSW) on pre-computed chunk_embeddings and doc_embedding
- Hybrid ranking that combines BM25 + semantic scores

All embeddings are pre-computed client-side via gemini-embedding-001
(768-dim, Matryoshka), fed as tensors — no embedder component in Vespa.
"""

from vespa.package import (
    ApplicationPackage,
    Document as VespaDocument,
    Field,
    FieldSet,
    HNSW,
    RankProfile,
    Schema,
    SecondPhaseRanking,
)


def build_application_package(app_name: str = "nctq") -> ApplicationPackage:
    """Build the Vespa application package for district document search."""

    doc = VespaDocument(
        fields=[
            # Identity / filtering
            Field(name="doc_id", type="string", indexing=["summary", "attribute"]),
            Field(name="district_id", type="int", indexing=["summary", "attribute"]),
            Field(name="district_name", type="string", indexing=["summary", "attribute"]),
            Field(name="ay_ids", type="array<int>", indexing=["summary", "attribute"]),
            Field(name="doc_type", type="string", indexing=["summary", "attribute"]),
            Field(name="doc_name", type="string", indexing=["summary", "attribute"]),

            # Document-level content
            Field(
                name="doc_summary",
                type="string",
                indexing=["summary", "index"],
                index="enable-bm25",
            ),
            Field(
                name="full_text",
                type="string",
                indexing=["summary", "index"],
                index="enable-bm25",
            ),

            # Chunk-level content (parallel arrays)
            Field(name="chunks", type="array<string>", indexing=["summary", "attribute"]),
            Field(name="chunk_summaries", type="array<string>", indexing=["summary", "attribute"]),
            Field(name="chunk_headings", type="array<string>", indexing=["summary", "attribute"]),

            # Pre-computed embeddings
            Field(
                name="chunk_embeddings",
                type="tensor<float>(chunk{}, x[768])",
                indexing=["attribute", "index"],
                ann=HNSW(distance_metric="angular"),
            ),
            Field(
                name="doc_embedding",
                type="tensor<float>(x[768])",
                indexing=["attribute", "index"],
                ann=HNSW(distance_metric="angular"),
            ),
        ],
    )

    schema = Schema(
        name="district_doc",
        document=doc,
        fieldsets=[
            FieldSet(name="default", fields=["full_text", "doc_summary"]),
        ],
        rank_profiles=[
            _hybrid_rank_profile(),
            _bm25_only_rank_profile(),
            _semantic_only_rank_profile(),
        ],
    )

    return ApplicationPackage(name=app_name, schema=[schema])


def _hybrid_rank_profile() -> RankProfile:
    """Combined BM25 + semantic ranking.

    First-phase: fast BM25 on full_text + doc_summary.
    Second-phase: re-rank top 100 with chunk + doc closeness blended in.
    """
    return RankProfile(
        name="hybrid",
        inputs=[("query(q_embedding)", "tensor<float>(x[768])")],
        first_phase="bm25(full_text) + bm25(doc_summary)",
        second_phase=SecondPhaseRanking(
            expression=(
                "0.3 * (bm25(full_text) + bm25(doc_summary)) "
                "+ 0.5 * closeness(field, chunk_embeddings) "
                "+ 0.2 * closeness(field, doc_embedding)"
            ),
            rerank_count=100,
        ),
    )


def _bm25_only_rank_profile() -> RankProfile:
    """Pure text matching — useful for debugging."""
    return RankProfile(
        name="bm25_only",
        first_phase="bm25(full_text) + bm25(doc_summary)",
    )


def _semantic_only_rank_profile() -> RankProfile:
    """Pure semantic ranking — useful for debugging."""
    return RankProfile(
        name="semantic_only",
        inputs=[("query(q_embedding)", "tensor<float>(x[768])")],
        first_phase=(
            "0.7 * closeness(field, chunk_embeddings) "
            "+ 0.3 * closeness(field, doc_embedding)"
        ),
    )
