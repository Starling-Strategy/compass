#!/bin/sh
# Entrypoint script for Compass API.
# Production (Azure) connects to PostgreSQL directly via NAT Gateway.
# No proxychains, no Tailscale — see ai_instructions/PRODUCTION_CONFIG.md.

exec uvicorn compass_backend.main:app --host 0.0.0.0 --port 8000
