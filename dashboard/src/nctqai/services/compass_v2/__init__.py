"""Data services for the Compass v2 Control Tower.

These wrap Logfire queries and DB reads so route files stay thin. Each
service module exposes a small number of functions returning dataclasses or
dicts ready for the component layer.
"""
