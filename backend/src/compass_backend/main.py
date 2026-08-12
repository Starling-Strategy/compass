"""Development entry point for the fresh Compass API shell."""

from .api.app import create_app_from_settings
from .config import settings

app = create_app_from_settings()


def main() -> None:
    """Run the API shell locally."""

    import uvicorn

    uvicorn.run(
        "compass_backend.main:app",
        host=settings.host,
        port=settings.port,
        reload=settings.debug,
    )


if __name__ == "__main__":
    main()
