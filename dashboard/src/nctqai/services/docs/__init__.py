"""Document services for the NCTQ.ai dashboard."""

from .documents import get_ay_options, get_district_options, get_doc_type_options, get_document_detail, get_document_stats, get_documents
from .publications import get_publication_detail, get_publications

__all__ = [
    "get_ay_options",
    "get_doc_type_options",
    "get_document_stats",
    "get_documents",
    "get_document_detail",
    "get_district_options",
    "get_publications",
    "get_publication_detail",
]
