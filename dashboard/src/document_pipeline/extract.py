"""Extract text from documents using IBM Docling.

Supports PDF, DOCX, PPTX, XLSX, HTML, Images with OCR.
"""

import hashlib
import logging
import time
from contextlib import nullcontext as _nullcontext
from pathlib import Path

try:
    import logfire
except ImportError:
    logfire = None

from docling.datamodel.base_models import InputFormat
from docling.datamodel.pipeline_options import (
    AcceleratorDevice,
    PdfPipelineOptions,
    TableFormerMode,
)
from docling.document_converter import DocumentConverter, PdfFormatOption

from document_pipeline.models import Document

logger = logging.getLogger(__name__)

# Singleton converter — heavy ML models, create once
_converter: DocumentConverter | None = None


def _get_converter(use_ocr: bool = True) -> DocumentConverter:
    """Get or create the Docling converter singleton."""
    global _converter
    if _converter is not None:
        return _converter

    pipeline_options = PdfPipelineOptions()
    pipeline_options.do_ocr = use_ocr
    pipeline_options.do_table_structure = True
    pipeline_options.table_structure_options.mode = TableFormerMode.ACCURATE

    # Hardware acceleration
    import torch

    if torch.backends.mps.is_available():
        pipeline_options.accelerator_options.device = AcceleratorDevice.MPS
        logger.info("Using Apple Silicon (MPS) acceleration")
    elif torch.cuda.is_available():
        pipeline_options.accelerator_options.device = AcceleratorDevice.CUDA
        logger.info("Using CUDA acceleration")
    else:
        pipeline_options.accelerator_options.device = AcceleratorDevice.CPU
        logger.info("Using CPU processing")

    _converter = DocumentConverter(
        format_options={
            InputFormat.PDF: PdfFormatOption(pipeline_options=pipeline_options),
            InputFormat.IMAGE: PdfFormatOption(pipeline_options=pipeline_options),
        }
    )
    return _converter


def extract(doc: Document, file_path: Path, use_ocr: bool = True) -> Document:
    """Extract text from a downloaded file and update the Document.

    Args:
        doc: The Document to update.
        file_path: Path to the downloaded file.
        use_ocr: Enable OCR for scanned documents.

    Returns:
        The same Document with extraction fields populated.
    """
    start = time.time()
    try:
        with logfire.span(
            "extract_text",
            src_id=doc.src_id,
            district_id=doc.district_id,
            file_path=str(file_path),
        ) if logfire else _nullcontext():
            converter = _get_converter(use_ocr)
            result = converter.convert(file_path)
            markdown = result.document.export_to_markdown()

            page_count = 0
            if hasattr(result.document, "pages"):
                page_count = len(result.document.pages)

            doc.full_text = markdown
            doc.text_length = len(markdown)
            doc.text_hash = hashlib.sha256(markdown.encode()).hexdigest()
            doc.page_count = page_count
            doc.extraction_status = "success"

            elapsed = int((time.time() - start) * 1000)
            logger.info(f"Extracted {doc.src_id}: {doc.text_length:,} chars in {elapsed}ms")

    except Exception as e:
        error_str = str(e)
        doc.extraction_status = _classify_extraction_error(error_str, doc.src_name or "")
        doc.extraction_error = error_str[:500]
        elapsed = int((time.time() - start) * 1000)
        logger.error(f"Extraction failed for {doc.src_id} [{doc.extraction_status}]: {e} ({elapsed}ms)")

    return doc


def _classify_extraction_error(error: str, src_name: str) -> str:
    """Map an extraction exception to a specific disposition status.

    Status values:
        corrupted          - File is malformed or invalid PDF
        format_unsupported - File type not handled by extractor
        blocked            - 403 Forbidden (site blocked crawler)
        dead_link          - 404 / DNS failure (URL gone)
        timeout            - Connection or read timeout (retriable)
        test_record        - Known test artifact, not real content
        failed             - Unknown / uncategorised error
    """
    e = error.lower()
    name = src_name.lower()

    # Test records first — named pattern takes priority
    if "abc_test_district" in name or "_test_" in name:
        return "test_record"

    if "not valid" in e or "invalid" in e:
        return "corrupted"
    if "file format not allowed" in e:
        return "format_unsupported"
    if "403" in e or "forbidden" in e:
        return "blocked"
    if "404" in e or "not found" in e or "does not exist" in e or "nodename" in e:
        return "dead_link"
    if "timed out" in e or "timeout" in e or "time out" in e:
        return "timeout"

    return "failed"
