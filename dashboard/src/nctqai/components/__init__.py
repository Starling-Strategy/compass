"""Reusable UI components for the NCTQ.ai dashboard."""

from .ay_select import AYSelect
from .badges import (
    AYAlignmentBadge,
    ConfidenceScoreBadge,
    DocumentTypeBadge,
    ExtractionStatusBadge,
    HoldBadge,
    INABadge,
    QIdBadge,
    q_label,
    QualityFlagBadge,
    ReadabilityBadge,
    StatusBadge,
    TypeAlignmentBadge,
)
from .kpi_card import KpiCard, KpiRow
from .modal_dots import ModalDots
from .nav import SECTIONS, SubNav, TopNav
from .progress_bar import ProgressBar
from .review_actions import HoldButton, HoldStatus, ReviewActionButtons, ReviewStatus

# Shared CSS class strings
TABLE_CLS = "uk-table uk-table-hover uk-table-divider uk-table-small uk-table-middle"

__all__ = [
    # Filters
    "AYSelect",
    # Navigation
    "TopNav",
    "SubNav",
    "SECTIONS",
    # KPI cards
    "KpiCard",
    "KpiRow",
    # Prediction / MC badges
    "StatusBadge",
    "INABadge",
    "QIdBadge",
    "q_label",
    # MC components
    "ModalDots",
    "ProgressBar",
    # Document badges
    "ReadabilityBadge",
    "ExtractionStatusBadge",
    "DocumentTypeBadge",
    "AYAlignmentBadge",
    "TypeAlignmentBadge",
    "ConfidenceScoreBadge",
    "QualityFlagBadge",
    # Review actions
    "ReviewActionButtons",
    "ReviewStatus",
    "HoldBadge",
    "HoldStatus",
    "HoldButton",
    # Shared CSS class strings
    "TABLE_CLS",
]
