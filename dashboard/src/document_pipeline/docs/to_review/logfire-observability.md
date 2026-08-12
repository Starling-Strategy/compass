# Logfire Observability for AI Pipelines

A comprehensive guide to implementing production-grade observability for AI/LLM applications using Pydantic Logfire.

---

## Why Logfire?

Logfire is built for modern AI applications:

- **OpenTelemetry-native**: Industry-standard traces, spans, and metrics
- **Pydantic integration**: First-class support for PydanticAI agents
- **AI-first features**: Built-in LLM cost tracking, token usage, and model performance
- **SQL queries**: Query your telemetry with familiar SQL syntax
- **Zero-config auto-instrumentation**: Automatic tracing for PydanticAI, httpx, FastAPI

---

## Quick Start

### Basic Setup

```python
import os
import logfire

# Configure Logfire at application startup
logfire.configure(
    service_name="my-ai-service",
    environment=os.getenv("ENV", "development"),
    send_to_logfire="if-token-present",  # Graceful degradation
)

# Enable auto-instrumentation
logfire.instrument_pydantic_ai()  # Traces all PydanticAI agent calls
logfire.instrument_httpx()        # Traces HTTP requests
```

### Environment Variables

| Variable | Purpose | Required |
|----------|---------|----------|
| `LOGFIRE_TOKEN` | Write token for sending data | Yes (for production) |
| `ENV` | Environment name (development/production) | Recommended |
| `LOGFIRE_CONSOLE` | Enable/disable console output | Optional |

---

## The Bootstrap Pattern

**Problem:** Many libraries read API keys from `os.environ` at import time, not via constructor parameters.

**Solution:** Bootstrap secrets to `os.environ` BEFORE importing those libraries.

```python
# bootstrap.py
import os

def bootstrap_for_logfire():
    """Must be called BEFORE importing logfire."""
    token = get_secret_from_vault("LOGFIRE_TOKEN")  # Your secret management
    if token:
        os.environ["LOGFIRE_TOKEN"] = token

# In your entry point:
from bootstrap import bootstrap_for_logfire
bootstrap_for_logfire()  # FIRST

import logfire  # NOW safe to import
logfire.configure(...)
```

---

## Centralized Configuration Module

Create a single module for consistent setup across all services:

```python
# observability.py
"""
Centralized Logfire configuration.

Usage:
    from observability import setup_observability
    setup_observability("my-service")
"""
import os

def setup_observability(service_name: str, **kwargs) -> None:
    """Configure Logfire with standard settings."""
    # 1. Bootstrap secrets
    bootstrap_for_logfire()

    # 2. Import logfire (now safe)
    import logfire

    # 3. Configure
    logfire.configure(
        service_name=service_name,
        environment=os.getenv("ENV", "development"),
        send_to_logfire="if-token-present",
        console=os.getenv("ENV") != "production",
        **kwargs,
    )

    # 4. Standard instrumentations
    logfire.instrument_pydantic_ai()
    logfire.instrument_httpx()
```

---

## Span Design for AI Pipelines

### Naming Conventions

Use hierarchical names that reflect the operation:

```
PIPELINE SPANS (top-level, long-running):
├── run_docpipe_pipeline
└── run_enrichment_batch

PHASE SPANS (within pipeline):
├── download_document
├── extract_text
└── enrich_document

OPERATION SPANS (specific tasks):
├── docling_convert
├── gemini_enrich
└── write_to_silver
```

### Standard Attributes

Add consistent context to all spans:

```python
import logfire

# Document pipeline context
with logfire.span("enrich_document",
    doc_id=doc_id,
    district_id=district_id,
    text_length=len(full_text),
):
    # ... enrichment logic
```

### LLM Metrics (Auto-Captured)

When using `logfire.instrument_pydantic_ai()`, these are captured automatically:

| Attribute | Description |
|-----------|-------------|
| `input_tokens` | Tokens sent to model |
| `output_tokens` | Tokens generated |
| `cache_read_tokens` | Tokens from cache (Gemini) |
| `cache_write_tokens` | Tokens written to cache |
| `cache_hit_rate` | Ratio of cached tokens |
| `model_name` | Model used for generation |

### Manual Token Tracking

For custom scenarios or verification:

```python
result = await agent.run(prompt)
usage = result.usage()

logfire.info(
    "LLM call complete",
    input_tokens=usage.input_tokens,
    output_tokens=usage.output_tokens,
    cache_hit_rate=usage.cache_read_tokens / usage.input_tokens if usage.input_tokens else 0,
)
```

---

## Dashboard Design Patterns

### 1. Executive Summary (Stakeholders)

High-level business metrics:

| Widget | What It Shows |
|--------|---------------|
| Total Requests (7d) | Volume and trends |
| Success Rate | % of successful completions |
| Estimated Cost | Token usage × pricing |
| Error Rate | System health indicator |

```sql
-- Success Rate Over Time
SELECT
  DATE_TRUNC('day', start_timestamp) as day,
  COUNT(CASE WHEN NOT is_exception THEN 1 END) * 100.0 / COUNT(*) as success_rate
FROM records
WHERE service_name = 'my-service'
  AND span_name = 'run_pipeline'
GROUP BY day
ORDER BY day;
```

### 2. Operational Health (Engineers)

Rapid triage and issue identification:

| Widget | What It Shows |
|--------|---------------|
| Error Rate Time Series | Trends and spikes |
| P95 Latency | Performance degradation |
| Recent Errors Table | Quick debugging |
| Throughput | Requests/minute |

```sql
-- P95 Latency by Span
SELECT
  span_name,
  PERCENTILE_CONT(0.95) WITHIN GROUP (ORDER BY duration) as p95_seconds,
  COUNT(*) as count
FROM records
WHERE service_name = 'my-service'
  AND kind = 'span'
GROUP BY span_name
ORDER BY p95_seconds DESC;
```

### 3. AI Deep Dive (Data Scientists)

Model performance and optimization:

| Widget | What It Shows |
|--------|---------------|
| Token Usage Over Time | Cost trends |
| Cache Hit Rate | Efficiency of caching |
| Model Comparison | Latency/cost by model |
| Quality Scores | Output quality metrics |

```sql
-- Token Usage and Estimated Cost
SELECT
  DATE_TRUNC('hour', start_timestamp) as hour,
  SUM((attributes->>'input_tokens')::int) as input_tokens,
  SUM((attributes->>'output_tokens')::int) as output_tokens,
  -- Adjust pricing for your model
  SUM(
    (attributes->>'input_tokens')::int * 0.075 / 1000000 +
    (attributes->>'output_tokens')::int * 0.30 / 1000000
  ) as est_cost_usd
FROM records
WHERE span_name = 'LLM usage'
GROUP BY hour
ORDER BY hour;
```

---

## AI Provider and Caching Optimization

### PydanticAI Model Providers

When using PydanticAI with Gemini models, you have two provider options:

| Provider | Model String | Concurrency Limit | Best For |
|----------|--------------|-------------------|----------|
| Direct API (`google-gla`) | `google-gla:gemini-2.5-flash` | 500+ | Production batch |
| Pydantic AI Gateway | `gateway/google-vertex:gemini-2.5-flash` | ~10-50 | Development |

**Critical Learning:** The Pydantic AI Gateway has organization-level concurrency limits. High-throughput batch processing will hit 429 errors. Use direct Google API for production.

```python
# Direct API (recommended for batch)
agent = Agent("google-gla:gemini-2.5-flash", ...)

# Gateway (for development/debugging)
agent = Agent("gateway/google-vertex:gemini-2.5-flash", ...)
```

### Gemini Context Caching

Gemini 2.5 models automatically cache identical prompt prefixes:

**Minimum Token Requirements:**
- Gemini Flash: 1,024+ input tokens
- Gemini Pro: 4,096+ input tokens

**Expected Cache Hit Rates (Real-World):**
- Large inputs (>10K tokens): 35-57%
- Small inputs (<1K tokens): 0% (below minimum)
- Overall average: 21-35%

**Why not higher?** Gemini's distributed backend routes concurrent requests to different instances. Each instance has its own cache partition.

**Optimization Pattern:**
```python
# PUT STATIC CONTENT FIRST (cached)
prompt = f"""DOCUMENT CONTENT:
{document.full_text}

---

QUESTION: {question.text}
"""  # Question varies, so it goes LAST
```

### Monitoring Cache Effectiveness

```sql
-- Cache hit rate by hour
SELECT
  DATE_TRUNC('hour', start_timestamp) as hour,
  AVG(CAST(attributes->>'cache_hit_rate' AS FLOAT)) * 100 as cache_pct,
  SUM(CAST(attributes->>'cache_read_tokens' AS INT)) as cached_tokens,
  SUM(CAST(attributes->>'input_tokens' AS INT)) as total_tokens
FROM records
WHERE span_name = 'Extraction usage'
GROUP BY hour
ORDER BY hour DESC
LIMIT 24;

-- Documents below caching threshold
SELECT
  attributes->>'doc_id' as doc_id,
  AVG(CAST(attributes->>'input_tokens' AS INT)) as avg_tokens
FROM records
WHERE span_name = 'Extraction usage'
  AND CAST(attributes->>'cache_hit_rate' AS FLOAT) = 0
GROUP BY doc_id
HAVING AVG(CAST(attributes->>'input_tokens' AS INT)) < 1024;
```

---

## SQL Query Templates

### Error Distribution

```sql
SELECT
  exception_type,
  COUNT(*) as count,
  MAX(start_timestamp) as last_seen
FROM records
WHERE service_name = 'my-service'
  AND is_exception = true
GROUP BY exception_type
ORDER BY count DESC
LIMIT 20;
```

### Cache Effectiveness

```sql
SELECT
  DATE_TRUNC('day', start_timestamp) as day,
  AVG((attributes->>'cache_hit_rate')::float) as avg_cache_hit_rate
FROM records
WHERE span_name = 'extraction_usage'
GROUP BY day
ORDER BY day;
```

### Throughput Over Time

```sql
SELECT
  DATE_TRUNC('hour', start_timestamp) as hour,
  COUNT(*) as requests
FROM records
WHERE service_name = 'my-service'
  AND span_name = 'run_pipeline'
GROUP BY hour
ORDER BY hour;
```

---

## Production Deployment Checklist

### Environment Variables

```bash
# Required
LOGFIRE_TOKEN=your-write-token

# Recommended
ENV=production
LOGFIRE_CONSOLE=false  # Disable console in prod

# Optional
OTEL_SERVICE_NAME=my-service  # Alternative to code config
```

### Azure App Service

1. **Store token in Key Vault:**
   ```bash
   az keyvault secret set --vault-name my-vault \
     --name logfire-token --value "your-token"
   ```

2. **Reference in App Service:**
   ```bash
   az webapp config appsettings set --name my-app \
     --settings LOGFIRE_TOKEN="@Microsoft.KeyVault(SecretUri=https://my-vault.vault.azure.net/secrets/logfire-token)"
   ```

3. **Add environment identifier:**
   ```bash
   az webapp config appsettings set --name my-app \
     --settings ENV=production LOGFIRE_CONSOLE=false
   ```

### AWS / GCP

Similar pattern: Store token in Secrets Manager/Secret Manager, reference via environment variables.

### Performance Optimization

1. **Disable console in production:**
   ```python
   logfire.configure(console=False)  # Reduces I/O overhead
   ```

2. **Use sampling for high-volume:**
   ```python
   # OpenTelemetry sampling (advanced)
   from opentelemetry.sdk.trace.sampling import TraceIdRatioBased
   sampler = TraceIdRatioBased(0.1)  # Sample 10% of traces
   ```

3. **Batch exports (default):**
   Logfire automatically batches spans before sending.

---

## Troubleshooting

### Common Issues

| Problem | Solution |
|---------|----------|
| No data in Logfire | Check `LOGFIRE_TOKEN` is set, use `send_to_logfire="if-token-present"` |
| Import errors | Call bootstrap BEFORE importing logfire |
| Duplicate spans | Ensure `setup_observability()` called only once |
| Missing attributes | Check span is active when setting attributes |
| Console output in prod | Set `LOGFIRE_CONSOLE=false` or `console=False` |

### Debugging

```python
# Verify configuration
import logfire
logfire.info("Test message", key="value")

# Check if token is present
import os
print(f"Token present: {bool(os.environ.get('LOGFIRE_TOKEN'))}")

# Verify instrumentations
# PydanticAI calls should show as nested spans
```

---

## Best Practices Summary

1. **Configure once** - Use centralized setup module
2. **Bootstrap first** - Set env vars before importing logfire
3. **Consistent naming** - Use hierarchical span names
4. **Standard attributes** - Always include context (IDs, metadata)
5. **Graceful degradation** - Use `send_to_logfire="if-token-present"`
6. **Production config** - Disable console, use Key Vault for tokens
7. **Dashboard hierarchy** - Executive → Operational → Deep Dive
8. **Query efficiently** - Filter by service_name, span_name, time range

---

## References

- [Pydantic Logfire Documentation](https://logfire.pydantic.dev/docs/)
- [PydanticAI + Logfire Integration](https://ai.pydantic.dev/logfire/)
- [OpenTelemetry Semantic Conventions for GenAI](https://opentelemetry.io/docs/specs/semconv/gen-ai/)
- [Logfire SQL Reference](https://logfire.pydantic.dev/docs/guides/web-ui/dashboards/)
