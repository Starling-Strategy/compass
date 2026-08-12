"""Logfire observability setup. Call setup() once at entry point."""
import logging
import os

logger = logging.getLogger(__name__)
LOGFIRE_AVAILABLE = False


def setup(service_name: str = "predictor", console: bool = False) -> bool:
    """Configure Logfire for the prediction pipeline.

    Args:
        service_name: The service name shown in Logfire UI.
        console: Whether to enable console output (useful for local dev).

    Returns:
        True if Logfire was configured successfully.
    """
    global LOGFIRE_AVAILABLE

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
            pass
        LOGFIRE_AVAILABLE = True
        logger.info(f"Logfire configured for {service_name}")
        return True
    except ImportError:
        logger.debug("logfire not installed")
        return False
    except Exception as e:
        logger.warning(f"Logfire setup failed: {e}")
        return False
