"""Modal dots component — agreement visualization.

Shows filled/empty circles representing vote agreement across k-depth predictions.
"""

from fasthtml.common import Span


def ModalDots(agreement: int | None = None, total: int = 15, *, agreement_pct: float | None = None):
    """Render filled/empty circles showing vote agreement.

    Args:
        agreement: Number of predictions that agree (mutually exclusive with agreement_pct).
        total: Total number of predictions (default 15 for k=2..16).
        agreement_pct: Agreement percentage (0-100). If provided, agreement count is computed.
    """
    if agreement_pct is not None:
        agreement = round(agreement_pct / 100 * total)
    elif agreement is None:
        agreement = 0
    dots = []
    for i in range(total):
        cls = "modal-dot filled" if i < agreement else "modal-dot"
        dots.append(Span(cls=cls))
    label = Span(
        f"{agreement}/{total}",
        cls="modal-dots-label",
    )
    return Span(*dots, label, cls="modal-dots", aria_label=f"Agreement: {agreement} out of {total}")
