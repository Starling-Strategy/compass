"""Real-time cost tracking for Gemini API calls.

Thread-safe singleton that accumulates input/output token counts and
computes running cost based on published per-model pricing.
"""
import logging
import threading
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

# Pricing per 1M tokens (source: Google AI pricing, March 2026)
_PRICING: dict[str, tuple[float, float, float]] = {
    # (input_per_1M, output_per_1M, cached_input_per_1M)
    "gemini-2.0-flash-lite": (0.075, 0.30, 0.075),
    "gemini-2.0-flash-lite-001": (0.075, 0.30, 0.075),
    "gemini-2.0-flash": (0.10, 0.40, 0.025),
    "gemini-2.5-flash-lite": (0.075, 0.30, 0.075),
    "gemini-2.5-flash": (0.15, 0.60, 0.0375),
}


@dataclass
class CostTracker:
    """Accumulates token usage across API calls and computes cost."""

    model: str = "gemini-2.0-flash-lite"
    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0
    call_count: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    @property
    def _rates(self) -> tuple[float, float, float]:
        return _PRICING.get(self.model, (0.075, 0.30, 0.075))

    @property
    def input_cost(self) -> float:
        uncached = self.input_tokens - self.cached_tokens
        return uncached * self._rates[0] / 1_000_000

    @property
    def cached_cost(self) -> float:
        return self.cached_tokens * self._rates[2] / 1_000_000

    @property
    def output_cost(self) -> float:
        return self.output_tokens * self._rates[1] / 1_000_000

    @property
    def total_cost(self) -> float:
        return self.input_cost + self.cached_cost + self.output_cost

    def record(self, response) -> None:
        """Extract token counts from a google-genai response and accumulate."""
        meta = getattr(response, "usage_metadata", None)
        if meta is None:
            return
        inp = getattr(meta, "prompt_token_count", 0) or 0
        out = getattr(meta, "candidates_token_count", 0) or 0
        cached = getattr(meta, "cached_content_token_count", 0) or 0
        with self._lock:
            self.input_tokens += inp
            self.output_tokens += out
            self.cached_tokens += cached
            self.call_count += 1

    def summary(self) -> str:
        parts = [
            f"Calls: {self.call_count:,}",
            f"Input: {self.input_tokens:,} tokens (${self.input_cost:.4f})",
        ]
        if self.cached_tokens > 0:
            parts.append(f"Cached: {self.cached_tokens:,} tokens (${self.cached_cost:.4f})")
        parts.extend([
            f"Output: {self.output_tokens:,} tokens (${self.output_cost:.4f})",
            f"Total: ${self.total_cost:.4f}",
        ])
        return " | ".join(parts)

    def reset(self) -> None:
        with self._lock:
            self.input_tokens = 0
            self.output_tokens = 0
            self.cached_tokens = 0
            self.call_count = 0


# Module-level singleton
_tracker: CostTracker | None = None


def get_tracker(model: str = "gemini-2.0-flash-lite") -> CostTracker:
    global _tracker
    if _tracker is None or _tracker.model != model:
        _tracker = CostTracker(model=model)
    return _tracker
