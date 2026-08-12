"""Compass section routes — overview, operations, conversations, scenarios, quality.

Live routes:
- /compass/overview                   — Funder-reporting overview (engagement, feedback, top topics)
- /compass/operations                 — Starling-only: cost, tokens, latency
- /compass/data-universe              — Data sources, freshness, daily-check status
- /compass/conversations              — Conversation list with sidebar + detail
- /compass/conversations/{session_id} — Direct link to a specific conversation
- /compass/scenarios                  — Read-only test scenario browser
- /compass/v2/traces                  — span-tree explorer (Logfire-backed)
- /compass/quality/*                  — verdict ledger + scorecard
"""

from nctqai.routes.compass.conversations import register as reg_conversations
from nctqai.routes.compass.data_universe import register as reg_data_universe
from nctqai.routes.compass.operations import register as reg_operations
from nctqai.routes.compass.overview import register as reg_overview
from nctqai.routes.compass.quality import register_quality_routes
from nctqai.routes.compass.scenarios import register as reg_scenarios
from nctqai.routes.compass.v2 import register_v2_routes


def register_compass_routes(rt):
    reg_overview(rt)
    reg_operations(rt)
    reg_data_universe(rt)
    reg_conversations(rt)
    reg_scenarios(rt)
    register_v2_routes(rt)
    register_quality_routes(rt)
