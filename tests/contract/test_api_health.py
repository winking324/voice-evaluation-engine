import asyncio
from pathlib import Path

from httpx import ASGITransport, AsyncClient, Response

from voice_eval.api.app import create_app
from voice_eval.config import Settings


def test_liveness_endpoint_reports_process_health() -> None:
    async def get_health_response() -> Response:
        transport = ASGITransport(app=create_app())
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.get("/health/live")

    response = asyncio.run(get_health_response())

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_configured_static_frontend_is_served_with_api_routes_intact(tmp_path: Path) -> None:
    async def get_responses() -> tuple[Response, Response]:
        web_dist = tmp_path / "web-dist"
        web_dist.mkdir()
        (web_dist / "index.html").write_text("<main>Voice Eval UI</main>", encoding="utf-8")
        app = create_app(
            settings=Settings(
                _env_file=None,
                data_root=tmp_path / "data",
                database_path=tmp_path / "data/runs/test.sqlite3",
                web_dist_dir=web_dist,
            )
        )
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.get("/"), await client.get("/health/live")

    frontend, health = asyncio.run(get_responses())

    assert frontend.status_code == 200
    assert "Voice Eval UI" in frontend.text
    assert health.status_code == 200
