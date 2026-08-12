"""Governed district-name normalization rules.

District-name normalization (case-fold, punctuation strip, suffix strip)
was previously hardcoded as a 12-entry tuple + two regexes inside
`catalog/resolution.py`. This module lifts the rules to a typed Pydantic
model with explicit order, type, and audit metadata. The same rules are
seeded to `compass.district_normalization_rules` (migration 033) so they
are governance-visible alongside `compass.catalog_aliases` and
`compass.renderer_notes`.

`DEFAULT_DISTRICT_NORMALIZATION_RULES` is the runtime authority for now;
the migration table is the auditable governance surface. The
`test_district_normalization_rules_in_sync_with_migration` test asserts
they cannot drift. A future PR can switch the runtime to load from the
DB at startup; this PR establishes the typed contract and locks
code/DB sync.

Doctrine: per CLAUDE.md "No prose-dispatch below the planner", the
hardcoded tuple was a `deletable` boundary leak — strings doing
authority work in code rather than governed data. This module + the
migration close that leak.
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


DistrictNormalizationRuleType = Literal[
    "lowercase",
    "replace",
    "regex_strip",
    "whitespace_collapse",
    "suffix_strip",
]


class CatalogIntegrityError(RuntimeError):
    """Raised when governed catalog data is missing or inconsistent."""


class DistrictNormalizationRule(BaseModel):
    """One ordered rule that contributes to district-name normalization."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    rule_order: int = Field(ge=1)
    rule_type: DistrictNormalizationRuleType
    pattern: str | None = None
    replacement: str | None = None
    notes: str = ""
    review_status: Literal["approved", "candidate", "rejected"] = "approved"
    active: bool = True

    @model_validator(mode="after")
    def _validate_rule_shape(self) -> "DistrictNormalizationRule":
        if self.rule_type == "lowercase" or self.rule_type == "whitespace_collapse":
            if self.pattern or self.replacement:
                raise ValueError(
                    f"rule_type={self.rule_type!r} takes no pattern or replacement"
                )
        elif self.rule_type == "replace":
            if self.pattern is None or self.replacement is None:
                raise ValueError(
                    "rule_type='replace' requires pattern and replacement"
                )
        elif self.rule_type == "regex_strip":
            if not self.pattern:
                raise ValueError("rule_type='regex_strip' requires pattern")
            if self.replacement is not None and self.replacement != " ":
                # regex_strip historically replaces matches with a single space;
                # accept omitted replacement as the implicit default.
                raise ValueError(
                    "rule_type='regex_strip' replacement must be ' ' or omitted"
                )
        elif self.rule_type == "suffix_strip":
            if not self.pattern:
                raise ValueError("rule_type='suffix_strip' requires pattern (the suffix)")
            if self.replacement is not None:
                raise ValueError(
                    "rule_type='suffix_strip' takes no replacement"
                )
        return self


# Application order matches the prior hardcoded implementation in
# `resolution.py::normalize_district_name_for_resolution`. Longer suffixes
# are stripped before shorter ones so multi-word suffixes match before
# their single-word constituents.
DEFAULT_DISTRICT_NORMALIZATION_RULES: tuple[DistrictNormalizationRule, ...] = (
    DistrictNormalizationRule(
        rule_order=1,
        rule_type="lowercase",
        notes="Lower-case the input before any matching.",
    ),
    DistrictNormalizationRule(
        rule_order=2,
        rule_type="replace",
        pattern="&",
        replacement=" and ",
        notes="Expand ampersand to the conjunction so 'X & Y' normalizes like 'X and Y'.",
    ),
    DistrictNormalizationRule(
        rule_order=3,
        rule_type="regex_strip",
        pattern=r"[^a-z0-9\s]+",
        replacement=" ",
        notes="Strip punctuation, leaving alphanumerics and whitespace.",
    ),
    DistrictNormalizationRule(
        rule_order=4,
        rule_type="whitespace_collapse",
        notes="Collapse runs of whitespace to a single space and trim.",
    ),
    DistrictNormalizationRule(
        rule_order=5,
        rule_type="suffix_strip",
        pattern="consolidated independent school district",
    ),
    DistrictNormalizationRule(
        rule_order=6,
        rule_type="suffix_strip",
        pattern="independent school district",
    ),
    DistrictNormalizationRule(
        rule_order=7,
        rule_type="suffix_strip",
        pattern="isd",
    ),
    DistrictNormalizationRule(
        rule_order=8,
        rule_type="suffix_strip",
        pattern="public school district",
    ),
    DistrictNormalizationRule(
        rule_order=9,
        rule_type="suffix_strip",
        pattern="unified school district",
    ),
    DistrictNormalizationRule(
        rule_order=10,
        rule_type="suffix_strip",
        pattern="unified schools",
    ),
    DistrictNormalizationRule(
        rule_order=11,
        rule_type="suffix_strip",
        pattern="unified",
    ),
    DistrictNormalizationRule(
        rule_order=12,
        rule_type="suffix_strip",
        pattern="school district",
    ),
    DistrictNormalizationRule(
        rule_order=13,
        rule_type="suffix_strip",
        pattern="public schools",
    ),
    DistrictNormalizationRule(
        rule_order=14,
        rule_type="suffix_strip",
        pattern="public school system",
    ),
    DistrictNormalizationRule(
        rule_order=15,
        rule_type="suffix_strip",
        pattern="schools",
    ),
    DistrictNormalizationRule(
        rule_order=16,
        rule_type="suffix_strip",
        pattern="district",
    ),
)


_WHITESPACE_COLLAPSE_RE = re.compile(r"\s+")


class DistrictNameNormalizer:
    """Apply a governed sequence of rules to normalize district names."""

    def __init__(self, rules: tuple[DistrictNormalizationRule, ...]) -> None:
        active = tuple(rule for rule in rules if rule.active)
        if not active:
            raise CatalogIntegrityError(
                "district normalization rules are empty; cannot normalize names"
            )
        # Sort by rule_order so the caller does not have to.
        self._rules: tuple[DistrictNormalizationRule, ...] = tuple(
            sorted(active, key=lambda rule: rule.rule_order)
        )
        # Pre-compile any regex_strip rule patterns once.
        self._compiled_regex_strips: dict[int, re.Pattern[str]] = {
            rule.rule_order: re.compile(rule.pattern or "")
            for rule in self._rules
            if rule.rule_type == "regex_strip"
        }
        # Pre-split phase: every rule before the first suffix_strip is a
        # one-shot transform; the suffix_strip rules together form an
        # iterative pass (loop until no suffix matches).
        suffix_order_start = next(
            (
                index
                for index, rule in enumerate(self._rules)
                if rule.rule_type == "suffix_strip"
            ),
            len(self._rules),
        )
        self._one_shot_rules = self._rules[:suffix_order_start]
        self._suffix_rules = self._rules[suffix_order_start:]

    @property
    def rules(self) -> tuple[DistrictNormalizationRule, ...]:
        return self._rules

    def __call__(self, value: str) -> str:
        normalized = value
        for rule in self._one_shot_rules:
            normalized = self._apply_one_shot(normalized, rule)
        if not self._suffix_rules:
            return normalized
        changed = True
        while changed:
            changed = False
            for rule in self._suffix_rules:
                suffix = rule.pattern or ""
                if not suffix:
                    continue
                if normalized == suffix:
                    return ""
                if normalized.endswith(f" {suffix}"):
                    normalized = normalized[: -len(suffix)].strip()
                    changed = True
                    break
        return normalized

    def _apply_one_shot(
        self, value: str, rule: DistrictNormalizationRule
    ) -> str:
        if rule.rule_type == "lowercase":
            return value.lower()
        if rule.rule_type == "replace":
            assert rule.pattern is not None
            assert rule.replacement is not None
            return value.replace(rule.pattern, rule.replacement)
        if rule.rule_type == "regex_strip":
            compiled = self._compiled_regex_strips[rule.rule_order]
            return compiled.sub(" ", value)
        if rule.rule_type == "whitespace_collapse":
            return _WHITESPACE_COLLAPSE_RE.sub(" ", value).strip()
        # suffix_strip falls through here only if mis-classified; the caller
        # branches above never reach this path.
        raise CatalogIntegrityError(
            f"unsupported district normalization rule_type: {rule.rule_type}"
        )


_DEFAULT_NORMALIZER = DistrictNameNormalizer(DEFAULT_DISTRICT_NORMALIZATION_RULES)


def normalize_district_name_for_resolution(value: str) -> str:
    """Normalize a district name using the governed default rule set."""

    return _DEFAULT_NORMALIZER(value)
