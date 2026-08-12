"""Compass v2 Control Tower — admin-facing views retained from the retired v2 track.

Live routes under /compass/v2/*:
- /compass/v2/traces              — turn span-tree explorer via Logfire
"""

from nctqai.routes.compass.v2.traces import register as reg_traces


def register_v2_routes(rt):
    reg_traces(rt)
