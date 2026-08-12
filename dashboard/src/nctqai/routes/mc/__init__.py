"""Metric Calculator routes for the NCTQ.ai dashboard.

Routes:
- /mc/districts                        — All districts with review progress
- /mc/districts/{id}                   — Single district's policy progress
- /mc/districts/{id}/policies/{dpol}   — Questions grouped by subpolicy
- /mc/districts/{id}/questions/{qid}   — Core question review page
- /mc/api/approve                      — HTMX approve endpoint
- /mc/api/reject                       — HTMX reject endpoint
"""


def register_mc_routes(rt):
    """Register all Metric Calculator routes.

    Args:
        rt: FastHTML route handler from fast_app().
    """
    from nctqai.routes.mc.api import register as reg_api
    from nctqai.routes.mc.batches import register as reg_batches
    from nctqai.routes.mc.district_detail import register as reg_detail
    from nctqai.routes.mc.district_policy import register as reg_district_policy
    from nctqai.routes.mc.districts import register as reg_districts
    from nctqai.routes.mc.policies import register as reg_policies
    from nctqai.routes.mc.question_review import register as reg_review
    from nctqai.routes.mc.questions import register as reg_questions

    reg_districts(rt)
    reg_detail(rt)
    reg_district_policy(rt)
    reg_review(rt)
    reg_questions(rt)
    reg_policies(rt)
    reg_batches(rt)
    reg_api(rt)
