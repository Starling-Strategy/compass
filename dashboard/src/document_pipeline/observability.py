"""Logfire observability setup for docpipe.

Call `setup()` once at startup. All other modules just `import logfire` and use spans.
If Logfire is not installed or LOGFIRE_TOKEN is not set, everything degrades gracefully.
"""

import logging
import os

logger = logging.getLogger(__name__)

LOGFIRE_AVAILABLE = False


def setup(service_name: str = "docpipe", console: bool = False) -> bool:
    """Configure Logfire for the pipeline or dashboard.

    Args:
        service_name: The service name shown in Logfire UI.
        console: Whether to enable console output (useful for local dev).

    Returns:
        True if Logfire was configured successfully.
    """
    global LOGFIRE_AVAILABLE

    # Load .env so LOGFIRE_TOKEN is available
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass

    if not os.environ.get("LOGFIRE_TOKEN"):
        logger.debug("LOGFIRE_TOKEN not set — observability disabled")
        return False

    try:
        import logfire

        # console=False disables terminal output; None uses defaults
        console_arg = None if console else False

        logfire.configure(
            service_name=service_name,
            environment=os.getenv("ENV", "development"),
            send_to_logfire="if-token-present",
            console=console_arg,
        )
        logfire.instrument_httpx()

        try:
            logfire.instrument_pydantic_ai()
        except Exception:
            pass  # PydanticAI instrumentation optional

        LOGFIRE_AVAILABLE = True
        logger.info(f"Logfire configured for {service_name}")
        return True

    except ImportError:
        logger.debug("logfire package not installed — observability disabled")
        return False
    except Exception as e:
        logger.warning(f"Logfire setup failed: {e}")
        return False
