"""Shared SQL filter constants for the Metric Calculator services.

These are used across all MC service files to ensure consistent filtering
on source = 'piedpiper' and active high-priority questions.
"""

# Source filter — bare for single-table UPDATEs (JOINed queries use v_latest_suggested_answers view which bakes in source filter)
SOURCE_FILTER_BARE = "source = 'piedpiper'"

# Question filter — only active, high-priority questions
Q_FILTER = "q.guidance_is_active = true AND q.q_priority = 'High'"

# District universe filter — only show in-universe districts
# Requires JOIN silver.districts sd ON sd.district_id = <alias>.district_id
D_FILTER = "sd.is_in_universe = true"

# Salary schedule questions — low accuracy (6-26%), flag for manual review
SALARY_Q_IDS = frozenset({
    89, 90, 91, 92, 93, 94, 95, 96, 97, 98, 99, 100, 101, 102,
    112, 113, 116, 123,
})
