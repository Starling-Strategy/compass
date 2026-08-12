"""US state-name → 2-letter abbreviation lookup and typed-string state splitting."""

from __future__ import annotations

STATE_NAME_TO_ABBREVIATION: dict[str, str] = {
    "alabama": "AL",
    "alaska": "AK",
    "arizona": "AZ",
    "arkansas": "AR",
    "california": "CA",
    "colorado": "CO",
    "connecticut": "CT",
    "delaware": "DE",
    "district of columbia": "DC",
    "florida": "FL",
    "georgia": "GA",
    "hawaii": "HI",
    "idaho": "ID",
    "illinois": "IL",
    "indiana": "IN",
    "iowa": "IA",
    "kansas": "KS",
    "kentucky": "KY",
    "louisiana": "LA",
    "maine": "ME",
    "maryland": "MD",
    "massachusetts": "MA",
    "michigan": "MI",
    "minnesota": "MN",
    "mississippi": "MS",
    "missouri": "MO",
    "montana": "MT",
    "nebraska": "NE",
    "nevada": "NV",
    "new hampshire": "NH",
    "new jersey": "NJ",
    "new mexico": "NM",
    "new york": "NY",
    "north carolina": "NC",
    "north dakota": "ND",
    "ohio": "OH",
    "oklahoma": "OK",
    "oregon": "OR",
    "pennsylvania": "PA",
    "rhode island": "RI",
    "south carolina": "SC",
    "south dakota": "SD",
    "tennessee": "TN",
    "texas": "TX",
    "utah": "UT",
    "vermont": "VT",
    "virginia": "VA",
    "washington": "WA",
    "west virginia": "WV",
    "wisconsin": "WI",
    "wyoming": "WY",
}


def normalize_state(state: str | None) -> str:
    """Normalize state values for deterministic comparisons.

    Returns the 2-letter abbreviation for any known full name (case-insensitive),
    or the value upper-cased when unrecognized. Empty / None becomes "".
    """

    value = (state or "").strip()
    return STATE_NAME_TO_ABBREVIATION.get(value.casefold(), value.upper())


# Reverse map: 2-letter abbreviation → Title-Case full name (e.g. "OH" → "Ohio").
_ABBREVIATION_TO_STATE_NAME: dict[str, str] = {
    abbr: name.title() for name, abbr in STATE_NAME_TO_ABBREVIATION.items()
}


def state_full_name(state: str | None) -> str:
    """Return the Title-Case full state name for an abbreviation or name.

    "OH" → "Ohio", "ohio" → "Ohio". Unknown / empty values pass through
    upper-cased (abbreviation-shaped) or title-cased (longer), so a caller
    always gets a usable label. The single authority for abbrev → name.
    """

    value = (state or "").strip()
    if not value:
        return ""
    abbr = STATE_NAME_TO_ABBREVIATION.get(value.casefold(), value.upper())
    return _ABBREVIATION_TO_STATE_NAME.get(abbr, value.title() if len(value) > 2 else value.upper())


# Tokens (case-insensitive) that name a recognized US state: every full name
# key and every 2-letter abbreviation value from the reference table.
_RECOGNIZED_STATE_TOKENS: frozenset[str] = frozenset(
    {name for name in STATE_NAME_TO_ABBREVIATION}
    | {abbr.casefold() for abbr in STATE_NAME_TO_ABBREVIATION.values()}
)


def split_state_suffix(text: str) -> tuple[str, set[str]]:
    """Split a trailing US-state qualifier off a typed district string.

    Handles ``"Name ST"`` and ``"Name, ST"`` (and full state names). The suffix
    is stripped only when BOTH hold:

    - the trailing token is a recognized US state (abbreviation or full name),
    - the remainder is non-empty.

    A bare state (``"ME"``, ``"West Virginia"``) is therefore returned
    unchanged — there is no name left to anchor on. The state is returned
    normalized to its 2-letter form.

    Returns ``(remainder, states)`` where ``states`` is empty when no qualifier
    was split.
    """

    stripped = text.strip()
    if not stripped:
        return stripped, set()

    # A bare state — single token or full multi-word name — has no district
    # name left to anchor on; return it unchanged.
    if stripped.casefold() in _RECOGNIZED_STATE_TOKENS:
        return stripped, set()

    # Comma form: "Portland, ME" — the suffix after the last comma.
    if "," in stripped:
        head, _, tail = stripped.rpartition(",")
        head = head.strip()
        tail = tail.strip()
        if head and tail.casefold() in _RECOGNIZED_STATE_TOKENS:
            return head, {normalize_state(tail)}
        return stripped, set()

    # Whitespace form: try the last TWO words as a multi-word state name
    # before the last single word, so "Charleston West Virginia" splits to
    # ("Charleston", WV) rather than ("Charleston West", VA) — West Virginia
    # is the one US state whose final token alone also names a state.
    parts = stripped.split()
    if len(parts) >= 3:
        last_two = " ".join(parts[-2:])
        if last_two.casefold() in _RECOGNIZED_STATE_TOKENS:
            return " ".join(parts[:-2]).strip(), {normalize_state(last_two)}
    if len(parts) >= 2:
        last = parts[-1]
        if last.casefold() in _RECOGNIZED_STATE_TOKENS:
            return " ".join(parts[:-1]).strip(), {normalize_state(last)}
    return stripped, set()
