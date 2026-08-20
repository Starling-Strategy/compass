# Policy Advisor API

The Compass chat engine: a Python / FastAPI service that turns a user's
question into a grounded, cited answer over NCTQ's reviewed district-policy
data. This is the production API that serves the Compass chat experience.

How it works, end to end, is documented in
[docs/02-product-and-answer-flow.md](../docs/02-product-and-answer-flow.md).
Where this code came from and how it maps to production is recorded in
[PROVENANCE.md](../PROVENANCE.md).

## Layout

| Path | Contents |
| --- | --- |
| `src/compass_backend/` | The application package (run with `PYTHONPATH=src`) |
| `content/` | Policy content bundled into the image |
| `scripts/entrypoint.sh` | Container entrypoint |
| `pyproject.toml`, `uv.lock` | Pinned dependencies (installed with `uv sync --frozen`) |
| `Dockerfile` | The production image build |

## Running

The service is deployed as a Docker container:

```bash
docker build -t compass-api .
docker run -p 8000:8000 --env-file .env compass-api
```

Configuration (database connection, model gateway key, observability token)
is supplied at runtime as environment variables — see
`src/compass_backend/config.py` for the settings the application reads.
