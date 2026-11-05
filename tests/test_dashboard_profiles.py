import asyncio

import dashboard_server
from starlette.middleware.cors import CORSMiddleware


def test_profile_registry_exposes_local_aliases_without_account_secrets(monkeypatch):
    monkeypatch.setenv("BOT_SECONDARY_AVAILABLE", "false")

    response = asyncio.run(dashboard_server.api_profiles())

    assert response == {
        "profiles": [
            {"id": "primary", "label": "Primary", "origin": "http://127.0.0.1:8765", "available": True},
            {"id": "secondary", "label": "Secondary", "origin": "http://127.0.0.1:8766", "available": False},
        ]
    }
    assert "ALPACA_API_KEY" not in str(response)
    assert "account_number" not in str(response)


def test_profile_registry_marks_configured_secondary_available(monkeypatch):
    monkeypatch.setenv("BOT_SECONDARY_AVAILABLE", "true")

    response = asyncio.run(dashboard_server.api_profiles())

    assert response["profiles"][1]["available"] is True


def test_dashboard_cors_allows_only_loopback_dashboard_origins():
    cors = next(middleware for middleware in dashboard_server.app.user_middleware
                if middleware.cls is CORSMiddleware)

    assert set(cors.kwargs["allow_origins"]) == {
        "http://127.0.0.1:8765", "http://localhost:8765",
        "http://127.0.0.1:8766", "http://localhost:8766",
    }
    assert cors.kwargs["allow_methods"] == ["GET"]
    assert cors.kwargs["allow_credentials"] is False


def test_dashboard_rejects_non_loopback_host_header():
    middleware = dashboard_server.LoopbackHostMiddleware(lambda scope, receive, send: None)
    sent = []

    async def capture(message):
        sent.append(message)

    asyncio.run(middleware(
        {
            "type": "http",
            "server": ("127.0.0.1", 8765),
            "headers": [(b"host", b"attacker.example:8765")],
        },
        None,
        capture,
    ))

    assert sent[0]["status"] == 400


def test_dashboard_accepts_loopback_host_on_bound_port():
    called = []

    async def downstream(scope, receive, send):
        called.append(True)

    middleware = dashboard_server.LoopbackHostMiddleware(downstream)
    scope = {
        "type": "http",
        "server": ("127.0.0.1", 8766),
        "headers": [(b"host", b"localhost:8766")],
    }

    asyncio.run(middleware(scope, None, None))

    assert called == [True]
