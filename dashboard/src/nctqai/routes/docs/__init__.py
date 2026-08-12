"""Document section routes for the NCTQ.ai dashboard.

Routes:
- /docs               — District documents list with KPIs and filters
- /docs/{src_id}      — Document detail page
- /docs/publications   — NCTQ publications list
- /docs/publications/{pub_id} — Publication detail
"""


def register_docs_routes(rt):
    """Register all document section routes.

    Args:
        rt: FastHTML route handler from fast_app().
    """
    from . import document_detail, documents, publications

    # Register static paths BEFORE parameterized paths to avoid
    # /docs/publications being captured by /docs/{src_id}.
    documents.register_routes(rt)
    publications.register_routes(rt)
    document_detail.register_routes(rt)
