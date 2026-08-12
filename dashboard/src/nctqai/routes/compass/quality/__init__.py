"""Compass Quality Scorecard routes aggregator.

Registers:
    GET  /compass/quality/scorecard               — hero (9 DimensionRows)
    GET  /compass/quality/scorecard/<dim_slug>    — dimension drill-down
    GET  /compass/quality/conversations/<session_id> — conversation with verdicts
    GET  /compass/quality/reports                 — human-review reports queue
    POST /compass/quality/reports/<report_id>/status — per-row triage status change
"""


def register_quality_routes(rt):
    """Register all Compass Quality routes via the FastHTML router."""
    from nctqai.routes.compass.quality.scorecard import register as reg_scorecard
    from nctqai.routes.compass.quality.conversations import register as reg_conversations
    from nctqai.routes.compass.quality.reports import register as reg_reports

    reg_scorecard(rt)
    reg_conversations(rt)
    reg_reports(rt)
