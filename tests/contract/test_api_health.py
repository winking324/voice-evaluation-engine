import asyncio

from httpx import ASGITransport, AsyncClient, Response

from voice_eval.api.app import create_app


def test_liveness_endpoint_reports_process_health() -> None:
    async def get_health_response() -> Response:
        transport = ASGITransport(app=create_app())
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.get("/health/live")

    response = asyncio.run(get_health_response())

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
