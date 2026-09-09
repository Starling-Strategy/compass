"""Single prediction via google-genai structured output.

PiedPiper equivalent: PredictionPipeline._predict_single()
Uses google-genai v1.65+ response_schema (accepts Pydantic models) and
response_mime_type="application/json" for structured output without
PydanticAI's extra prompt scaffolding.
"""
import asyncio
import json as _json
import logging
import os
import re
from contextlib import nullcontext as _nullcontext

try:
    import logfire
except ImportError:
    logfire = None

from typing import Literal

from pydantic import Field, create_model
from prediction_pipeline.config import Config
from prediction_pipeline.models import (
    Chunk, Citation, INA_VARIANTS, PredictionOutput, PredictionRun,
    PredictionStrategy, QuestionContext, QuestionType, ResolvedCitation,
)

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """You are an expert policy analyst for the National Council on Teacher Quality (NCTQ).
You answer questions about school district policies using provided evidence passages.

KEY RULES:
1. Read ALL evidence passages carefully — especially the Summary lines. Answers are often stated directly in summaries.
2. Base your answer ONLY on the provided evidence. Do not infer or extrapolate.
3. Match valid_options exactly when provided (case-sensitive).
4. For numeric questions, provide raw numbers without formatting (94444 not $94,444).
5. For multi-select checkboxes, provide all that apply as a comma-separated list. Only select options that are DIRECTLY and EXPLICITLY supported by the evidence. Do not select options based on inference or "likely" applicability.
6. Only answer "INA" if you have read every passage and the information is genuinely absent — but see the INA vs "No" convention below. NOTE: "Unclear or vague" (when available in valid_options) is DIFFERENT from INA. Use "Unclear or vague" when the contract discusses the topic but the language is ambiguous. Use INA only when the topic is entirely absent.

INA vs "No" CONVENTION (CRITICAL):
- "INA" = the policy/contract DOES NOT ADDRESS the topic at all. There is no mention of the practice, benefit, or requirement. The information is simply absent from all evidence.
- "No" = the policy/contract EXPLICITLY ADDRESSES the topic and states it does not exist, is not offered, or is not applicable. For example, "The district does not offer performance pay" = "No". But if performance pay is never mentioned = INA.
- When evidence discusses RELATED topics but never mentions the specific practice asked about, the answer is INA, not "No". For example, if the contract discusses evaluation but never mentions performance pay bonuses, the answer for a performance pay question is INA.
- For numeric questions: if the specific number asked about is not explicitly stated or directly calculable from stated numbers, answer INA. Do not estimate or derive values from partial information.
- CRITICAL: A topic being MENTIONED in passing does not mean a policy EXISTS. Only answer with a specific value when the evidence contains an explicit policy definition, rule, or requirement. Vague or tangential references (e.g., a word appearing in a heading, general discussion, or a related but different policy) are NOT sufficient — answer INA.
- CRITICAL: "No" requires an EXPLICIT DENIAL in the contract text (e.g., "the district does not offer…"). You CANNOT infer "No" from absence. If the topic simply isn't discussed, answer INA. This applies especially to:
  * Performance pay/bonuses: INA unless the contract explicitly says "no performance pay." If the contract discusses salaries but never mentions performance bonuses at all, that is INA (not "No").
  * Additional pay for high-needs schools / hard-to-staff subjects: INA if the contract never mentions such incentives. "No" only if the contract explicitly addresses and denies them.
  * Third-party evaluators: INA if the evaluation section never discusses third-party roles.
  * Date of first/last day of school: ALWAYS INA. School calendars are operational documents that change annually, not contractual policy. Even if you see a date in the evidence, answer INA.
  * Annual cost-of-living adjustment: INA unless the CBA specifies a percentage or formula. General salary increases or step advancements are not COLAs.
  * "Minimum amount of additional pay for [subject] teachers": If the evidence shows specific dollar amounts for subject-area incentives, report those amounts. Only answer INA if there is NO mention of subject-specific additional pay at all.

SALARY SCHEDULE CONVENTIONS:
- Step-to-years mapping: Step 1 = first-year teacher (0 years prior experience). "After X years of experience" = Step X+1. For example, "after 3 years" = Step 4, "after 5 years" = Step 6, "after 10 years" = Step 11.
- If the required step exceeds the schedule's maximum, use the maximum step value. For example, "after 25 years" with a 20-step schedule = Step 20.
- Minimum years to reach max salary = max_step - 1 (e.g., a 20-step schedule takes 19 years).
- For salary schedule TYPE questions: a traditional schedule uses step-and-lane (years × education) to set base pay. Even if step advancement requires satisfactory evaluations, it remains "Traditional" — the evaluation gates advancement but does not change the schedule structure. Supplemental incentive programs (like ProComp, bonuses, stipends) are add-ons to the base schedule, NOT separate salary schedule types.
- "Master's degree" column: In salary schedules, a teacher with ONLY a master's degree (no additional credits) maps to the BA+36/MA lane. The MA+18, MA+36, MA+54 lanes require credits BEYOND the master's degree.
- "Maximum annual salary a teacher can earn": Read the SINGLE highest dollar value that appears as a cell in the main certified teacher salary schedule table. This is typically the last step in the highest lane (Doctorate or highest education column). STRICT RULES: (1) Do NOT add any supplements, stipends, bonuses, or incentives on top. (2) If multiple salary schedules exist (e.g., "Grandfathered" and "Pay for Performance"), use the one with the higher maximum. (3) Do NOT calculate or sum values — just read the highest cell value directly from the table.
- "Maximum annual base salary for a teacher with a master's degree": find the BA+36/MA column (or equivalent master's lane), last step. Do NOT use MA+18 or higher lanes.
- Lane count: Count the number of distinct column headers in the salary schedule. BA, BA+18, BA+36/MA, MA+18, MA+36, MA+54, Doctorate = 7 lanes.
- Step count: Count the actual number of rows in the salary schedule that have distinct step numbers. Do NOT count sub-headers or blank rows.
- Non-traditional / pay-for-performance schedules: Some districts use performance-based pay rather than traditional step-and-lane. If the salary schedule has "levels" or "tiers" instead of steps, or all experience levels get the same base salary, you can still extract values. "After X years" maps to the entry that best matches that experience level. If the schedule has NO differentiation by years, the same base salary applies regardless of years.
- IMPORTANT: When a salary schedule table is present in the evidence, you MUST extract values from it. Do NOT answer INA for salary questions when a salary table is visible in the evidence passages.

OBSERVATION CONVENTIONS:
- "Minimum number of informal observations": If the contract explicitly discusses informal observations but does not specify a minimum number, the answer is 0 (no minimum is mandated). If the contract does not mention informal observations AT ALL for that teacher group (tenured/non-tenured), the answer is INA.
- Observation duration: Check for duration specifications like "at least 15 minutes." If found, map to the valid option that best describes it (e.g., "at least 15 minutes" for an informal observation = "Less than 30 minutes" if that is a valid option).

COLLABORATION CONVENTIONS:
- "Permits" vs "Requires" collaboration: If the contract allows teachers to collaborate (e.g., PLCs, common planning time) but does not MANDATE it as part of the evaluation, the answer is "Permitted." It is "Required" ONLY if collaboration is an explicit, mandatory component of the evaluation itself.

LANE MOVEMENT / WHAT IS NEEDED TO MOVE LANES:
- Only select options that the salary schedule or CBA explicitly describes as mechanisms for lane advancement. Typical options: "Degree" (earning a higher degree), "Degree credits" (accumulating graduate credits), "Professional learning or professional development" (PD hours/credits). Do NOT include options unless the CBA explicitly states that mechanism triggers a lane change.

INEFFECTIVE RATING / FORMAL HEARING:
- "Automatically trigger a formal dismissal hearing" means the policy REQUIRES a formal hearing as an AUTOMATIC consequence of the rating, with no intervening steps. If the policy describes a progressive improvement plan (PIP/TIP) that may EVENTUALLY lead to dismissal if not met, the answer is "No" — the hearing is not automatic. If the policy never addresses consequences of ineffective ratings, the answer is INA.

CODING GUIDANCE RULES:
- Follow the coding guidance's definitions carefully. Pay attention to what each answer option requires.
- When valid_options include "Other" and the coding guidance maps "lack of reference" or "issue not addressed" to "Other", select "Other" unless the evidence contains explicit, unambiguous evidence for a specific option.

EVALUATION FEEDBACK METHOD: The question asks HOW the teacher receives their evaluation results. Written reports, online evaluation forms, or documents delivered to the teacher = "Written feedback only." A "required conference" means a SEPARATE mandatory meeting that IS the feedback delivery mechanism itself (e.g., "evaluation feedback shall be provided via a required conference"). An end-of-year evaluation review meeting where the evaluator discusses the written report with the teacher BEFORE FINALIZING it is a process step in the evaluation, NOT a feedback delivery conference — the feedback IS the written report.

THIRD-PARTY EVALUATORS: A third-party evaluator must be (1) someone NOT defined as an "Evaluator" in the policy/agreement, AND (2) someone whose formal ratings or scored feedback are part of the official evaluation. Anyone listed in the definition of "Evaluators" (principals, APs, team leaders, peer observers, etc.) is a FORMAL evaluator, not third-party. Content experts who provide advisory support, dispute resolution, or "third-party guidance" for disagreements are NOT third-party evaluators — they do not rate or score teachers. If the only references are to advisory/support roles, the answer is "Other" (issue not addressed)."""

def _ay_label(ay_id: int) -> str:
    """Convert ay_id integer to human-readable AY label.
    ay_id=25 -> 2024-2025, ay_id=24 -> 2023-2024, etc.
    """
    end_year = 2000 + ay_id
    return f"{end_year - 1}-{end_year}"


def _salary_year_patch(ay_id: int) -> str:
    ay = _ay_label(ay_id)
    end_year = 2000 + ay_id
    start_year = end_year - 1
    return f"""
CRITICAL — SALARY SCHEDULE YEAR SELECTION:
You are answering for Academic Year {ay}. When multiple salary schedules are present:
1. ALWAYS use the most recent schedule (prefer '{start_year}' or '{end_year}' in the filename/title).
2. NEVER read values from older schedules unless no current-year schedule exists.
3. If a document filename contains an old year, skip it and look for the current-year version.
4. State which schedule year you are reading from in your reasoning.

READING SALARY TABLES — NAVIGATING ROWS AND COLUMNS:
- For MAXIMUM salary: find the LAST row (highest step number) in the HIGHEST lane column (typically Doctorate or the rightmost education column). Read that single cell value.
- For maximum salary with a master's degree: find the LAST row in the BA+36/MA column (or equivalent master's lane). Do NOT use MA+18 or higher lanes.
- When a table has many rows and columns, state the exact step number and lane name you are reading from. Double-check you are in the correct column before reporting the value.
- Do NOT average, sum, or interpolate values. Read the exact cell value from the table."""

_DATE_OVERRIDE = """
SCHOOL CALENDAR DATES — MANDATORY RULE:
Questions about "first day of school" or "last day of school" (Q196, Q197) ALWAYS get INA.
School calendars are operational documents that change annually — they are NOT contractual policy.
Even if you see a specific date in the evidence, you MUST answer INA for these questions.
Do NOT extract calendar dates. This rule overrides all other instructions."""

_SALARY_QS = {89, 90, 91, 92, 93, 94, 95, 96, 97, 98, 99, 100, 101, 102, 112, 113, 116, 123}
_DATE_QS = {196, 197}

# INA verification prompt — used by verify_ina_async()
_INA_VERIFY_PROMPT = """You are an NCTQ policy analyst doing a FINAL CHECK on a question that was initially marked INA.

CRITICAL MISSION: The previous analysis said INA (information not available). You must determine if that's really true.

Re-read every evidence passage carefully. Look for:
- Any relevant numbers, values, or policy statements
- Salary tables (even partial ones)
- Any mention of the topic, even indirect

If you find ANYTHING useful, extract it and provide an answer.
INA is only correct if the topic is truly 100% absent from every single passage.
When in doubt, prefer a specific answer over INA.

Respond with JSON: {"answer": "your answer", "confidence": 0.0-1.0, "reasoning": "brief explanation"}"""


def resolve_system_prompt(q_id: int, q_ans_type: str, ay_id: int = 25) -> str:
    """Question-type routing: apply targeted prompt patches per question."""
    if q_id in _DATE_QS:
        return SYSTEM_PROMPT + _DATE_OVERRIDE
    if q_id in _SALARY_QS or q_ans_type == "Numeric":
        return SYSTEM_PROMPT + _salary_year_patch(ay_id)
    return SYSTEM_PROMPT


def resolve_top_k(q_ans_type: str, k_default: int) -> int:
    """Adaptive k: multi-select gets k=12, everything else uses k_default."""
    if q_ans_type == "Multi-select checkboxes":
        return min(k_default, 12)
    return k_default


_SALARY_KEYWORDS = re.compile(
    r"salary|compensation|pay\s*scale|wage|stipend|earning",
    re.IGNORECASE,
)
_EVAL_KEYWORDS = re.compile(
    r"evaluat|observation|rating|ineffective|proficient|rubric",
    re.IGNORECASE,
)


def classify_question(question: QuestionContext) -> QuestionType:
    """Classify a question into a QuestionType for adaptive strategy selection."""
    if question.q_id in _DATE_QS:
        return QuestionType.DATE
    if question.q_id in _SALARY_QS:
        return QuestionType.SALARY
    if question.q_ans_type == "Numeric" and _SALARY_KEYWORDS.search(question.q_text):
        return QuestionType.SALARY
    if question.q_ans_type == "Numeric":
        return QuestionType.NUMERIC
    ans_lower = question.q_ans_type.lower()
    if "multi" in ans_lower or "checkbox" in ans_lower:
        return QuestionType.MULTISELECT
    if "select" in ans_lower or "drop" in ans_lower:
        if _EVAL_KEYWORDS.search(question.q_text):
            return QuestionType.EVAL
        return QuestionType.SELECT
    return QuestionType.DEFAULT


def resolve_strategy(question: QuestionContext, config: Config) -> PredictionStrategy:
    """Build a PredictionStrategy from question classification + config defaults."""
    qtype = classify_question(question)

    if qtype == QuestionType.DATE:
        return PredictionStrategy(
            question_type=qtype, k_min=1, k_max=1,
            temperature=0.0, ina_threshold=1, always_verify_ina=False,
        )
    if qtype == QuestionType.SALARY:
        return PredictionStrategy(
            question_type=qtype, k_min=config.k_min, k_max=config.k_max,
            temperature=0.3, ina_threshold=1, always_verify_ina=True,
        )
    if qtype == QuestionType.NUMERIC:
        return PredictionStrategy(
            question_type=qtype, k_min=config.k_min, k_max=config.k_max,
            temperature=0.3, ina_threshold=1, always_verify_ina=True,
        )
    if qtype == QuestionType.MULTISELECT:
        return PredictionStrategy(
            question_type=qtype, k_min=config.k_min, k_max=min(config.k_max, 16),
            temperature=0.3, ina_threshold=4, always_verify_ina=False,
        )
    if qtype == QuestionType.EVAL:
        return PredictionStrategy(
            question_type=qtype, k_min=config.k_min, k_max=config.k_max,
            temperature=0.3, ina_threshold=config.ina_threshold, always_verify_ina=False,
        )
    # SELECT and DEFAULT
    return PredictionStrategy(
        question_type=qtype, k_min=config.k_min, k_max=config.k_max,
        temperature=0.3, ina_threshold=config.ina_threshold, always_verify_ina=False,
    )


_genai_client = None
_loop = None

_ina_alts = "|".join(v.replace(" ", r"\s+").replace("/", r"/?") for v in sorted(INA_VARIANTS))
_INA_PATTERNS = re.compile(rf"^({_ina_alts})$", re.IGNORECASE)

_SALARY_HEADER_RE = re.compile(
    r"(bachelor|master|doctorate|BA\+?\d*|MA\+?\d*|BA\s*/\s*MA|"
    r"step|lane|salary\s*schedule|compensation)",
    re.IGNORECASE,
)

_JSON_BLOCK = re.compile(r'\{[^{}]*\}', re.DOTALL)


def _get_genai_client():
    """Lazy singleton for google-genai client with SDK-level retry."""
    global _genai_client
    if _genai_client is None:
        from google import genai
        from google.genai import types
        api_key = os.environ.get("GOOGLE_API_KEY")
        if not api_key:
            raise ValueError("Set GOOGLE_API_KEY in .env")
        _genai_client = genai.Client(
            api_key=api_key,
            http_options=types.HttpOptions(
                timeout=180_000,  # milliseconds
                retry_options=types.HttpRetryOptions(
                    attempts=7,
                    initial_delay=2.0,
                    max_delay=120.0,
                    exp_base=2,
                    jitter=1.0,
                    http_status_codes=[429, 500, 503, 504],
                ),
            ),
        )
    return _genai_client


async def create_question_cache(sys_prompt: str, full_prompt: str, config: Config) -> str:
    """Create explicit Gemini cache for a question's k-diversity predictions.

    Caches the system prompt + full evidence prompt so each k-depth call
    only sends a short instruction (~50 tokens) instead of the full prompt.
    """
    from google.genai import types
    client = _get_genai_client()
    cache = await client.aio.caches.create(
        model=f"models/{config.prediction_model}",
        config=types.CreateCachedContentConfig(
            system_instruction=sys_prompt,
            contents=[types.Content(role="user", parts=[types.Part(text=full_prompt)])],
            ttl=f"{config.caching_ttl}s",
        )
    )
    return cache.name


async def delete_question_cache(cache_name: str) -> None:
    """Delete a question cache (best-effort, errors are non-fatal)."""
    try:
        client = _get_genai_client()
        await client.aio.caches.delete(name=cache_name)
    except Exception as e:
        logger.debug("Cache delete failed (non-fatal): %s", e)


def _try_parse_table(text: str) -> str | None:
    """Parse pipe-delimited salary/schedule tables into compact markdown.


    PDF table extractions often produce massively padded pipe-delimited text.
    This detects table structure and formats it for the model.
    Returns None if text isn't a parseable table.
    """
    if text.count('|') < 10:
        return None
    lines = text.strip().split('\n')
    if len(lines) < 4:
        return None
    try:
        headers = None
        header_idx = None
        abbrev_markers = {'BA', 'BA+18', 'BA+36/MA', 'MA', 'MA+18', 'MA+36', 'MA+54', 'Doctorate'}
        degree_name_re = re.compile(
            r"(bachelor|master|doctorate|BA\+?\d|MA\+?\d|Ed\.?\s*Spec)", re.IGNORECASE
        )
        best_header = None
        best_idx = None
        best_score = 0
        for i, line in enumerate(lines[:20]):
            cells = [c.strip() for c in line.split('|') if c.strip()]
            if not cells or len(cells) < 3:
                continue
            if any(c.startswith('$') for c in cells):
                continue
            if any(c in abbrev_markers for c in cells):
                headers = cells
                header_idx = i
                break
            score = sum(1 for c in cells if degree_name_re.search(c))
            if score > best_score:
                best_score = score
                best_header = cells
                best_idx = i
        if not headers and best_score >= 2:
            headers = best_header
            header_idx = best_idx
        if not headers:
            for i, line in enumerate(lines[:15]):
                cells = [c.strip() for c in line.split('|') if c.strip()]
                if len(cells) >= 3 and sum(1 for c in cells if _SALARY_HEADER_RE.search(c)) >= 2:
                    if not any(c.startswith('$') for c in cells):
                        headers = cells
                        header_idx = i
                        break

        if headers and header_idx is not None:
            data_start = header_idx + 1
            for j in range(header_idx + 1, min(header_idx + 4, len(lines))):
                cells = [c.strip() for c in lines[j].split('|') if c.strip()]
                if cells and (cells[0].replace('.', '').isdigit() or cells[0].startswith('$')):
                    data_start = j
                    break
                if all(c in ('-', '---', '---:') for c in cells):
                    data_start = j + 1
                    break

            rows = []
            for line in lines[data_start:]:
                cells = [c.strip() for c in line.split('|') if c.strip()]
                if not cells:
                    continue
                m = re.match(r'^(\d+)\s+(\$[\d,]+)', cells[0])
                if m:
                    cells = [m.group(1), m.group(2)] + cells[1:]
                if cells[0].replace('.', '').isdigit() or re.match(r'^\d', cells[0]):
                    rows.append(cells)

            if rows:
                n_cols = len(headers) + 1
                out = ["Step | " + " | ".join(headers)]
                out.append("---: | " + " | ".join(["---:"] * len(headers)))
                for row in rows:
                    while len(row) < n_cols:
                        row.append("")
                    out.append(" | ".join(row[:n_cols]))
                return "\n".join(out)

        pipe_lines = [l for l in lines if l.count('|') >= 3]
        if len(pipe_lines) >= 5:
            salary_signal = sum(1 for l in pipe_lines if re.search(r'\$[\d,]+', l))
            if salary_signal >= 3:
                clean = "\n".join(pipe_lines[:40])
                return f"[SALARY TABLE]\n{clean}"

        return None
    except Exception:
        return None


def build_prompt(question: QuestionContext, district_name: str, state: str,
                 chunks: list[Chunk]) -> str:
    max_text_chars = 500_000
    sections = []

    sections.append(f"## District\n{district_name}, {state}")
    q_section = "## Question\n"
    if question.parent_q_text:
        q_section += f"Context: {question.parent_q_text}\n\n"
    q_section += f"**{question.q_text}**\n\nAnswer type: {question.q_ans_type}"
    if question.valid_options:
        q_section += f"\nValid options: {', '.join(question.valid_options)}"
    if question.coding_guidance:
        guidance = re.sub(
            r'\*{0,2}V\d+\.\d+\s+INFERENCE:?\*{0,2}\s*.*?(?=\n\n|\Z)',
            '', question.coding_guidance, flags=re.DOTALL
        ).strip()
        if guidance:
            q_section += f"\n\nCoding guidance: {guidance}"
    if question.ai_coding_notes:
        q_section += f"\n\n## Decision Criteria\n{question.ai_coding_notes}"
    if question.synthesis_logic:
        q_section += f"\n\nSynthesis logic: {question.synthesis_logic}"
    focus_parts = []
    if question.focus_terms:
        focus_parts.append(f"Focus terms: {', '.join(question.focus_terms)}")
    if question.target_sections:
        focus_parts.append(f"Target sections: {', '.join(question.target_sections)}")
    if question.terminology_context:
        focus_parts.append(f"Terminology: {question.terminology_context}")
    if focus_parts:
        q_section += "\n" + "\n".join(focus_parts)
    sections.append(q_section)

    if chunks:
        evidence_lines = []
        total_chars = 0
        max_total_chars = 3_500_000
        for i, chunk in enumerate(chunks, 1):
            header_parts = [f"### Passage {i} (from: {chunk.doc_name}, doc_id: {chunk.doc_id})"]
            if chunk.page_number:
                header_parts.append(f"Page: {chunk.page_number}")
            if chunk.section_heading:
                header_parts.append(f"Section: {chunk.section_heading}")
            header = "\n".join(header_parts)
            lines = [header]
            if chunk.summary:
                lines.append(f"Summary: {chunk.summary}")
            text = chunk.text
            parsed = _try_parse_table(text)
            if parsed:
                lines.append(f"Content:\n{parsed}")
            elif text.strip():
                remaining_budget = max_total_chars - total_chars
                per_chunk_limit = min(max_text_chars, remaining_budget)
                if per_chunk_limit <= 0:
                    lines.append("[... budget exhausted, passage omitted]")
                elif len(text) > per_chunk_limit:
                    text = text[:per_chunk_limit] + "\n[... truncated]"
                    lines.append(f"Content:\n{text}")
                else:
                    lines.append(f"Content:\n{text}")
            passage_text = "\n".join(lines)
            total_chars += len(passage_text)
            evidence_lines.append(passage_text)
        sections.append("## Evidence Passages\nRead each passage carefully. The answer is often stated in the Summary lines.\n\n" + "\n\n---\n\n".join(evidence_lines))
    else:
        sections.append("## Evidence Passages\nNo documents were retrieved for this question.")

    sections.append(f"## Your Task\nAnswer the question: **{question.q_text}**\nBase your answer on the evidence passages above.")
    return "\n\n".join(sections)


_GARBAGE_RE = re.compile(r'^[\s`{}\[\]<>]*$|^```')

# Unwrap serialized list/array values: ['INA'] or ["INA"] → INA
_BRACKETED_RE = re.compile(r"""^\[['"]?(.+?)['"]?\]$""")

# LLM refusal strings → INA
_REFUSAL_RE = re.compile(
    r"^(I\s+(apologize|am sorry|cannot|can[\u2019']t|don[\u2019']t have|do not have)"
    r"|I[\u2019']m\s+unable"
    r"|the (provided|available) (documents?|text|passages?|evidence) (do|does) not"
    r"|unfortunately|no .{0,30} (found|available|provided))",
    re.IGNORECASE,
)

# N/A with explanation → INA  (e.g. "N/A - district doesn't offer…")
_NA_PREFIX_RE = re.compile(r"^n/?a\s*[-—–:.]", re.IGNORECASE)

# Markdown heading prefix: "## Final Answer:" or "**Answer:**" → strip
_MD_HEADING_RE = re.compile(r"^(?:#{1,3}\s+)?(?:\*{1,2})?(?:final\s+)?answer\s*:?\s*(?:\*{1,2})?\s*", re.IGNORECASE)


def _validate_answer(answer: str, question: QuestionContext) -> str:
    answer = answer.strip()

    # Strip markdown heading prefixes: "## Final Answer: Yes" → "Yes"
    answer = _MD_HEADING_RE.sub("", answer).strip()

    # Unwrap serialized lists: ['INA'] → INA
    m = _BRACKETED_RE.match(answer)
    if m:
        answer = m.group(1).strip()

    if _INA_PATTERNS.match(answer):
        return "INA"
    if _GARBAGE_RE.match(answer):
        logger.warning("Garbage answer detected, treating as INA: %r", answer[:50])
        return "INA"

    # LLM refusal strings → INA
    if _REFUSAL_RE.match(answer):
        logger.warning("Refusal string detected, treating as INA: %r", answer[:80])
        return "INA"

    # N/A with explanation → INA
    if _NA_PREFIX_RE.match(answer):
        logger.warning("N/A prefix detected, treating as INA: %r", answer[:80])
        return "INA"

    answer = re.sub(r'^\$', '', answer).replace(',', '')
    if answer.endswith('%'):
        answer = answer[:-1].strip()
    if not question.valid_options:
        return answer

    opts_lower = {opt.lower(): opt for opt in question.valid_options}

    if answer.lower() in opts_lower:
        return opts_lower[answer.lower()]

    matched = []
    remaining = answer
    for opt in sorted(question.valid_options, key=len, reverse=True):
        if opt.lower() in remaining.lower():
            matched.append(opt)
            remaining = re.sub(re.escape(opt), '', remaining, count=1, flags=re.IGNORECASE).strip()

    if matched:
        return ", ".join(matched)

    return answer


def normalize_multiselect_answer(answer: str, valid_options: list[str] | None = None) -> str:
    """For multi-select answers: if any part doesn't match a valid option, append 'Other'.

    Only applies when 'Other' is a valid option and not already in the answer.
    """
    if not valid_options or not answer:
        return answer

    # Never modify INA answers
    if answer.strip().upper() == "INA":
        return answer

    options_lower = {o.strip().lower() for o in valid_options}
    if "other" not in options_lower:
        return answer  # "Other" isn't a valid option for this question

    parts = [p.strip() for p in answer.split(",")]
    has_other = any(p.lower() == "other" for p in parts)
    if has_other:
        return answer  # already includes Other

    # Check if all answer parts match a valid option
    all_match = all(
        any(p.lower() == vo for vo in options_lower)
        for p in parts
    )
    if not all_match:
        parts.append("Other")
        return ", ".join(parts)

    return answer


def build_output_type(k: int):
    """Create a PredictionOutput variant with Literal-constrained document_index.

    Puts valid indices [1..k] into the JSON schema enum so Gemini
    cannot hallucinate an out-of-range document index.
    """
    valid = tuple(str(i) for i in range(1, k + 1))
    DynCitation = create_model(
        "Citation",
        document_index=(Literal[valid], Field(description="Document number from Evidence Passages (1-indexed)")),
        quote=(str, Field(description="Verbatim quote from the document supporting your answer")),
    )
    return create_model(
        "PredictionOutput",
        __base__=PredictionOutput,
        key_citations=(list[DynCitation], Field(
            default_factory=list,
            description="Up to 3 citations with document_index and verbatim quote",
        )),
    )


def resolve_citations(citations: list, chunks: list[Chunk]) -> list[ResolvedCitation]:
    """Map Gemini's Citation (index + quote) to ResolvedCitation with chunk metadata."""
    resolved = []
    for cite in citations:
        idx = int(cite.document_index) - 1
        if 0 <= idx < len(chunks):
            c = chunks[idx]
            resolved.append(ResolvedCitation(
                document_index=int(cite.document_index),
                doc_id=c.doc_id,
                doc_name=c.doc_name,
                quote=cite.quote,
                page_number=c.page_number,
                section_heading=c.section_heading,
                relevance_score=c.score,
            ))
    return resolved


def _parse_lite_response(raw: str, question: QuestionContext) -> tuple[str, float, str]:
    """Parse JSON response from direct google-genai calls.

    Tries JSON first (stripping markdown fences), then falls back to
    heuristic text parsing. Returns (answer, confidence, reasoning).
    """
    clean = raw.strip()
    if clean.startswith("```"):
        clean = re.sub(r"^```(?:json)?\s*", "", clean)
        clean = re.sub(r"\s*```(?:\s*\n.*)?$", "", clean, flags=re.DOTALL)

    for candidate in [clean, raw]:
        try:
            data = _json.loads(candidate.strip())
            if not isinstance(data, dict):
                # Gemini returned a bare primitive (str/int/float) — use as answer
                return str(data), 0.5, ""
            answer = str(data.get("answer", "INA"))
            confidence = float(data.get("confidence", 0.5))
            reasoning = str(data.get("reasoning", ""))
            return answer, min(max(confidence, 0.0), 1.0), reasoning
        except (ValueError, KeyError, TypeError):
            pass

    # Repair missing commas — Gemini sometimes emits {"key": "val" "key2": ...}
    repaired = re.sub(r'(?<=["\d\]}])\s+(?=")', ', ', clean)
    if repaired != clean:
        try:
            data = _json.loads(repaired)
            if not isinstance(data, dict):
                return str(data), 0.5, ""
            answer = str(data.get("answer", "INA"))
            confidence = float(data.get("confidence", 0.5))
            reasoning = str(data.get("reasoning", ""))
            return answer, min(max(confidence, 0.0), 1.0), reasoning
        except (ValueError, KeyError, TypeError):
            pass

    m = _JSON_BLOCK.search(raw)
    if m:
        try:
            data = _json.loads(m.group())
            if not isinstance(data, dict):
                return str(data), 0.5, ""
            answer = str(data.get("answer", "INA"))
            confidence = float(data.get("confidence", 0.5))
            reasoning = str(data.get("reasoning", ""))
            return answer, min(max(confidence, 0.0), 1.0), reasoning
        except (ValueError, KeyError, TypeError):
            pass

    # Extract fields from truncated/malformed JSON via regex
    ans_m = re.search(r'"answer"\s*:\s*"([^"]*)"', raw)
    if ans_m:
        answer = ans_m.group(1)
        conf_m = re.search(r'"confidence"\s*:\s*(\d+\.?\d*)', raw)
        confidence = float(conf_m.group(1)) if conf_m else 0.5
        reason_m = re.search(r'"reasoning"\s*:\s*"([^"]*)', raw)
        reasoning = reason_m.group(1) if reason_m else ""
        return answer, min(max(confidence, 0.0), 1.0), reasoning

    # Fallback: return raw text as answer
    return raw.strip().split("\n")[0][:100], 0.5, raw.strip()


def _record_usage(response, config: Config) -> None:
    """Record cost tracking and emit Logfire usage metrics for a genai response."""
    try:
        from prediction_pipeline.cost_tracker import get_tracker
        get_tracker(config.prediction_model).record(response)
    except Exception:
        pass
    if logfire and hasattr(response, 'usage_metadata') and response.usage_metadata:
        um = response.usage_metadata
        logfire.info("genai_usage",
            gen_ai_model=config.prediction_model,
            model_version=config.model_version,
            **{"gen_ai.usage.input_tokens": getattr(um, 'prompt_token_count', 0),
               "gen_ai.usage.output_tokens": getattr(um, 'candidates_token_count', 0),
               "gen_ai.usage.cached_tokens": getattr(um, 'cached_content_token_count', 0)},
        )


async def _genai_call(sys_prompt: str, user_prompt: str, config: Config,
                      temperature: float = 0.1, max_tokens: int = 512) -> str | None:
    """Single google-genai call. SDK handles retry via HttpRetryOptions."""
    from google.genai import types
    client = _get_genai_client()
    gen_config = types.GenerateContentConfig(
        system_instruction=sys_prompt,
        temperature=temperature,
        max_output_tokens=max_tokens,
    )
    try:
        response = await client.aio.models.generate_content(
            model=config.prediction_model,
            contents=user_prompt,
            config=gen_config,
        )
        _record_usage(response, config)
        if response.text is None:
            logger.warning("genai call returned None text (content filter or empty response)")
            return None
        return response.text.strip()
    except Exception as e:
        logger.warning("genai call failed: %s", e)
        return None


async def predict_async(question, district_id, ay_id, district_name, state,
                        chunks, config, generation_id, top_k, run_id=None,
                        system_prompt_override=None, temperature=None,
                        cached_content=None) -> PredictionRun:
    with logfire.span("predict", q_id=question.q_id, top_k=top_k, model_version=config.model_version) if logfire else _nullcontext():
        output_type = build_output_type(len(chunks)) if chunks else PredictionOutput

        from google.genai import types
        effective_temp = temperature if temperature is not None else config.prediction_temperature

        if cached_content:
            # Cached path: system prompt + full evidence are in the cache.
            # Send only a short instruction telling the model which passages to use.
            gen_config = types.GenerateContentConfig(
                cached_content=cached_content,
                temperature=effective_temp,
                max_output_tokens=4096,
                response_mime_type="application/json",
                response_schema=output_type,
            )
            contents = f"Answer using only Evidence Passages 1 through {top_k}. Ignore any passages numbered higher than {top_k}."
        else:
            # Standard uncached path
            sys_prompt = system_prompt_override or resolve_system_prompt(question.q_id, question.q_ans_type, ay_id)
            prompt = build_prompt(question, district_name, state, chunks)
            gen_config = types.GenerateContentConfig(
                system_instruction=sys_prompt,
                temperature=effective_temp,
                max_output_tokens=4096,
                response_mime_type="application/json",
                response_schema=output_type,
            )
            contents = prompt

        client = _get_genai_client()
        response = await client.aio.models.generate_content(
            model=config.prediction_model,
            contents=contents,
            config=gen_config,
        )

        _record_usage(response, config)

        # Parse structured output
        raw_text = response.text
        if raw_text is None:
            logger.warning("predict got None response text for Q%s k=%s", question.q_id, top_k)
            return PredictionRun(
                district_id=district_id, ay_id=ay_id, q_id=question.q_id,
                generation_id=generation_id, top_k=top_k,
                predicted_answer="INA", confidence=0.0,
                reasoning="Model returned empty response",
                key_citations_json=[],
                evidence_agreement="no_evidence",
                retrieval_scores=[c.score for c in chunks],
                source=config.source, model_version=config.model_version, run_id=run_id,
            )
        try:
            output = output_type.model_validate_json(raw_text)
        except Exception:
            # Fallback: try _parse_lite_response for malformed JSON
            answer, confidence, reasoning = _parse_lite_response(raw_text, question)
            predicted = _validate_answer(answer, question)
            return PredictionRun(
                district_id=district_id, ay_id=ay_id, q_id=question.q_id,
                generation_id=generation_id, top_k=top_k,
                predicted_answer=predicted, confidence=confidence,
                reasoning=reasoning,
                key_citations_json=[],
                evidence_agreement="unknown",
                retrieval_scores=[c.score for c in chunks],
                source=config.source, model_version=config.model_version, run_id=run_id,
            )

        predicted = _validate_answer(output.predicted_answer, question)
        resolved = resolve_citations(output.key_citations, chunks)
        return PredictionRun(
            district_id=district_id, ay_id=ay_id, q_id=question.q_id,
            generation_id=generation_id, top_k=top_k,
            predicted_answer=predicted, confidence=output.confidence,
            reasoning=output.reasoning,
            key_citations_json=[rc.model_dump() for rc in resolved],
            evidence_agreement=output.evidence_agreement,
            retrieval_scores=[c.score for c in chunks],
            source=config.source, model_version=config.model_version, run_id=run_id,
        )


async def verify_ina_async(
    question: QuestionContext,
    district_name: str,
    state: str,
    chunks: list[Chunk],
    config: Config,
) -> tuple[str, float, str] | None:
    """Second-pass INA verification using direct google-genai.

    Called when synthesis returns INA with high entropy (split vote).
    Uses a targeted anti-INA prompt to recover false-positive INAs.
    Returns (answer, confidence, reasoning) if a non-INA answer is found, None otherwise.
    """
    user_prompt = build_prompt(question, district_name, state, chunks)
    raw = await _genai_call(_INA_VERIFY_PROMPT, user_prompt, config, temperature=0.1)
    if not raw:
        return None
    try:
        answer, confidence, reasoning = _parse_lite_response(raw, question)
        predicted = _validate_answer(answer, question)
        if predicted.upper() != "INA" and confidence >= 0.5:
            logger.info("  INA verify: Q%s found '%s' (conf=%.2f)", question.q_id, predicted, confidence)
            return predicted, confidence, reasoning
    except Exception:
        logger.debug("  INA verify failed for Q%s (non-fatal)", question.q_id)
    return None


_OPTION_EVAL_PROMPT = """You are an NCTQ policy analyst. Given evidence passages about a school district,
determine whether the evidence supports this SPECIFIC answer option.

Respond ONLY with JSON: {"applies": true/false, "evidence": "brief quote or reasoning"}

Rules:
- Only answer true if the evidence EXPLICITLY and DIRECTLY supports this option.
- Do NOT infer or extrapolate. Vague or tangential references are NOT sufficient.
- If the option is not clearly supported, answer false."""


async def predict_multiselect_enum(
    question: QuestionContext,
    district_name: str,
    state: str,
    chunks: list[Chunk],
    config: Config,
) -> str | None:
    """Per-option independent evaluation for multi-select questions.

    Fires parallel calls for each valid option (excluding INA). Returns
    comma-separated selected options, or None if none apply.
    """
    options = [opt for opt in question.valid_options if opt.upper() != "INA"]
    if not options:
        return None

    base_prompt = build_prompt(question, district_name, state, chunks)

    async def _eval_option(option: str) -> tuple[str, bool]:
        user_prompt = f"{base_prompt}\n\n---\nDoes the evidence support this specific option: \"{option}\"?"
        raw = await _genai_call(_OPTION_EVAL_PROMPT, user_prompt, config, temperature=0.0, max_tokens=256)
        if not raw:
            return option, False
        try:
            data = _json.loads(raw.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip())
            return option, bool(data.get("applies", False))
        except (ValueError, KeyError, TypeError):
            return option, False

    results = await asyncio.gather(*[_eval_option(opt) for opt in options])
    selected = [opt for opt, applies in results if applies]

    if selected:
        logger.info("  Multiselect enum: Q%s selected %s", question.q_id, selected)
        return ", ".join(selected)
    return None


def predict(question, district_id, ay_id, district_name, state,
            chunks, config, generation_id, top_k, run_id=None,
            system_prompt_override=None) -> PredictionRun:
    global _loop
    if _loop is None or _loop.is_closed():
        _loop = asyncio.new_event_loop()
    return _loop.run_until_complete(
        predict_async(question, district_id, ay_id, district_name, state,
                      chunks, config, generation_id, top_k, run_id,
                      system_prompt_override)
    )
