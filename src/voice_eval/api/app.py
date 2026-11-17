"""FastAPI application entry point."""

from fastapi import FastAPI

from voice_eval.config import get_settings


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(title=settings.app_name)

    @app.get("/health/live", tags=["health"])
    async def liveness() -> dict[str, str]:
        """Report that the API process is running; dependencies are not probed here."""

        return {"status": "ok"}

    return app


app = create_app()
