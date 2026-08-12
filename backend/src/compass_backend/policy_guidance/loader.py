"""Markdown -> typed PolicyGuidanceLibrary loader.

The content/nctq-policy/topics/*.md format is regular and authored by hand
(per SSN-236 stopgap). We parse rather than render: every field maps to a
typed Pydantic model, and every cross-record invariant is enforced at load
time so a broken edit fails the FastAPI lifespan loudly rather than serving
hallucinated citations.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from compass_backend.policy_guidance.models import (
    ExemplarPolicy,
    PolicyGuidanceLibrary,
    ResearchRationale,
    Stance,
    TopicGuidance,
)

DEFAULT_CONTENT_DIR = (
    Path(__file__).resolve().parents[3] / "content" / "nctq-policy" / "topics"
)

_FRONTMATTER_RE = re.compile(
    r"\A---\s*\n(?P<frontmatter>.*?\n)---\s*\n(?P<body>.*)\Z",
    re.DOTALL,
)
_H2_RE = re.compile(r"^##\s+(?P<title>.+?)\s*$", re.MULTILINE)
_H3_RE = re.compile(
    r"^###\s+\[(?P<kind>stance|rationale|exemplar):(?P<slug>[a-z0-9][a-z0-9-]*)\]\s+(?P<title>.+?)\s*$",
    re.MULTILINE,
)


class LibraryLoadError(RuntimeError):
    """Raised when the markdown content cannot be loaded as a typed library."""


def load_library(content_dir: Path = DEFAULT_CONTENT_DIR) -> PolicyGuidanceLibrary:
    """Load every *.md file in ``content_dir`` and assemble the typed library.

    Strict: any per-file failure or cross-file uniqueness violation raises
    LibraryLoadError. The caller (FastAPI lifespan) should treat this as
    fatal — better not to boot than to serve broken citations.
    """

    if not content_dir.exists():
        raise LibraryLoadError(
            f"policy guidance content directory not found: {content_dir}"
        )

    topics: dict[str, TopicGuidance] = {}
    for path in sorted(content_dir.glob("*.md")):
        if _is_redirect_file(path):
            # Stub topic that points at another file (e.g. performance-pay
            # documents that the canonical content lives under differentiated-pay).
            # Skip the load; alias merging is a separate concern.
            continue
        topic = parse_topic_file(path)
        if topic.topic_id in topics:
            raise LibraryLoadError(
                f"duplicate topic_id {topic.topic_id!r} (second occurrence in {path.name})"
            )
        topics[topic.topic_id] = topic

    if not topics:
        raise LibraryLoadError(f"no topic files found in {content_dir}")

    try:
        return PolicyGuidanceLibrary.build(topics=topics)
    except (ValueError, ValidationError) as exc:
        raise LibraryLoadError(f"library assembly failed: {exc}") from exc


def _is_redirect_file(path: Path) -> bool:
    """Return True if the topic file's frontmatter has a non-empty redirect_to."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return False
    match = _FRONTMATTER_RE.match(text)
    if not match:
        return False
    try:
        data = yaml.safe_load(match.group("frontmatter")) or {}
    except yaml.YAMLError:
        return False
    return bool(data.get("redirect_to"))


def parse_topic_file(path: Path) -> TopicGuidance:
    """Parse a single topic markdown file into a typed TopicGuidance.

    Raises LibraryLoadError on shape problems; lets ValidationError surface
    when individual records are malformed (caller can catch either).
    """

    text = path.read_text(encoding="utf-8")
    fm_data, body = _split_frontmatter(text, path)

    try:
        topic_id = str(fm_data["topic_id"])
        canonical_topic = str(fm_data["canonical_topic"])
    except KeyError as exc:
        raise LibraryLoadError(
            f"{path.name}: required frontmatter field {exc.args[0]!r} missing"
        ) from exc

    aliases_raw = fm_data.get("aliases") or ()
    aliases = tuple(str(a) for a in aliases_raw)
    canonical_url = fm_data.get("canonical_url") or None

    sections = _split_h2_sections(body)
    topic_brief = sections.get("Topic Brief", "").strip()
    if not topic_brief:
        raise LibraryLoadError(f"{path.name}: missing or empty '## Topic Brief' section")

    stances = _parse_stance_section(sections.get("Stances", ""), topic_id, path)
    rationales = _parse_rationale_section(
        sections.get("Research Rationales", ""), topic_id, path,
    )
    exemplars = _parse_exemplar_section(
        sections.get("Exemplary Policies", ""), topic_id, path,
    )

    try:
        return TopicGuidance(
            topic_id=topic_id,
            canonical_topic=canonical_topic,
            aliases=aliases,
            canonical_url=canonical_url,
            topic_brief=topic_brief,
            stances=stances,
            rationales=rationales,
            exemplars=exemplars,
        )
    except ValidationError as exc:
        raise LibraryLoadError(f"{path.name}: {exc}") from exc


# ---------------------------------------------------------------------------
# Frontmatter + section splitting


def _split_frontmatter(text: str, path: Path) -> tuple[dict[str, Any], str]:
    match = _FRONTMATTER_RE.match(text)
    if not match:
        raise LibraryLoadError(
            f"{path.name}: missing YAML frontmatter delimited by --- lines"
        )
    try:
        data = yaml.safe_load(match.group("frontmatter")) or {}
    except yaml.YAMLError as exc:
        raise LibraryLoadError(f"{path.name}: frontmatter not valid YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise LibraryLoadError(f"{path.name}: frontmatter must be a mapping")
    return data, match.group("body")


def _split_h2_sections(body: str) -> dict[str, str]:
    """Split body text on ``## `` headers into a {title: contents} dict."""
    sections: dict[str, str] = {}
    matches = list(_H2_RE.finditer(body))
    for i, match in enumerate(matches):
        title = match.group("title").strip()
        start = match.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(body)
        sections[title] = body[start:end]
    return sections


# ---------------------------------------------------------------------------
# Per-section item parsers — each splits on ### headers and parses items.


def _split_h3_items(section_body: str) -> list[tuple[str, str, str, str]]:
    """Yield (kind, slug, title, content) for each ``### [kind:slug] Title`` item."""
    items: list[tuple[str, str, str, str]] = []
    matches = list(_H3_RE.finditer(section_body))
    for i, match in enumerate(matches):
        start = match.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(section_body)
        items.append(
            (
                match.group("kind"),
                match.group("slug"),
                match.group("title").strip(),
                section_body[start:end],
            )
        )
    return items


def _parse_metadata_bullets(content: str) -> tuple[dict[str, str], str]:
    """Split a content block into a leading ``- key: value`` bullet list and remaining body."""
    lines = content.splitlines()
    metadata: dict[str, str] = {}
    body_start = 0
    for i, raw in enumerate(lines):
        line = raw.strip()
        if not line:
            # blank line — keep scanning while we may still hit bullets
            if metadata:
                body_start = i + 1
                continue
            continue
        if line.startswith("- "):
            kv = line[2:]
            if ":" not in kv:
                # not a metadata bullet; treat as body
                body_start = i
                break
            key, _, value = kv.partition(":")
            metadata[key.strip()] = value.strip()
            body_start = i + 1
        else:
            body_start = i
            break
    body = "\n".join(lines[body_start:]).strip()
    return metadata, body


def _parse_stance_section(
    section: str, topic_id: str, path: Path,
) -> tuple[Stance, ...]:
    stances: list[Stance] = []
    for kind, slug, title, content in _split_h3_items(section):
        if kind != "stance":
            raise LibraryLoadError(
                f"{path.name}: unexpected '{kind}' item under ## Stances"
            )
        body = content.strip()
        if not body:
            raise LibraryLoadError(
                f"{path.name}: stance '{slug}' has empty body"
            )
        try:
            stances.append(
                Stance(
                    stance_id=f"stance:{slug}",
                    topic_id=topic_id,
                    title=title,
                    body=body,
                )
            )
        except ValidationError as exc:
            raise LibraryLoadError(f"{path.name}: stance:{slug}: {exc}") from exc
    return tuple(stances)


def _parse_rationale_section(
    section: str, topic_id: str, path: Path,
) -> tuple[ResearchRationale, ...]:
    rationales: list[ResearchRationale] = []
    for kind, slug, title, content in _split_h3_items(section):
        if kind != "rationale":
            raise LibraryLoadError(
                f"{path.name}: unexpected '{kind}' item under ## Research Rationales"
            )
        metadata, body = _parse_metadata_bullets(content)
        if not body:
            raise LibraryLoadError(
                f"{path.name}: rationale '{slug}' has empty body"
            )
        source_url_raw = metadata.get("source_url", "").strip()
        if not source_url_raw:
            raise LibraryLoadError(
                f"{path.name}: rationale '{slug}' missing source_url "
                f"(use the Pathfinder homepage placeholder + citation_status: placeholder "
                f"if the real URL hasn't landed yet)"
            )
        try:
            rationales.append(
                ResearchRationale(
                    rationale_id=f"rationale:{slug}",
                    stance_id=metadata.get("stance_id", ""),
                    topic_id=topic_id,
                    title=title,
                    body=body,
                    source_title=metadata.get("source_title", ""),
                    source_url=source_url_raw,  # type: ignore[arg-type]
                    citation_status=metadata.get("citation_status", ""),  # type: ignore[arg-type]
                )
            )
        except ValidationError as exc:
            raise LibraryLoadError(f"{path.name}: rationale:{slug}: {exc}") from exc
    return tuple(rationales)


def _parse_exemplar_section(
    section: str, topic_id: str, path: Path,
) -> tuple[ExemplarPolicy, ...]:
    exemplars: list[ExemplarPolicy] = []
    for kind, slug, title, content in _split_h3_items(section):
        if kind != "exemplar":
            raise LibraryLoadError(
                f"{path.name}: unexpected '{kind}' item under ## Exemplary Policies"
            )
        metadata, body = _parse_metadata_bullets(content)
        if not body:
            raise LibraryLoadError(
                f"{path.name}: exemplar '{slug}' has empty body"
            )
        district_id_raw = metadata.get("district_id", "").strip()
        district_id = int(district_id_raw) if district_id_raw else None
        state_raw = metadata.get("state", "").strip()
        try:
            exemplars.append(
                ExemplarPolicy(
                    exemplar_id=f"exemplar:{slug}",
                    topic_id=topic_id,
                    district=metadata.get("district", title),
                    district_id=district_id,
                    state=state_raw or None,
                    subtopic=metadata.get("subtopic", ""),
                    body=body,
                    source_url=metadata.get("source_url", ""),  # type: ignore[arg-type]
                    citation_status=metadata.get("citation_status", ""),  # type: ignore[arg-type]
                )
            )
        except ValidationError as exc:
            raise LibraryLoadError(f"{path.name}: exemplar:{slug}: {exc}") from exc
    return tuple(exemplars)
