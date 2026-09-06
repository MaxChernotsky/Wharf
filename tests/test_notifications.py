import httpx
import pytest

from app.notifications import NotificationHub


def make_hub(settings, handler=None) -> NotificationHub:
    transport = httpx.MockTransport(handler) if handler else None
    return NotificationHub(settings, transport=transport)


def test_not_configured_by_default(settings):
    hub = make_hub(settings)
    assert hub.configured() is False


def test_configured_once_url_token_and_service_are_set(settings):
    settings.ha_url = "http://homeassistant.local:8123"
    settings.ha_token = "secret"
    settings.ha_notify_service = "mobile_app_max_iphone"
    hub = make_hub(settings)
    assert hub.configured() is True


@pytest.mark.asyncio
async def test_notify_without_config_fails_without_hitting_network(settings):
    def handler(request):
        raise AssertionError("should not make a network call when unconfigured")

    hub = make_hub(settings, handler)
    record = await hub.notify("port-scanner", "hello")
    assert record.ok is False
    assert "not configured" in record.error or "aren't configured" in record.error


@pytest.mark.asyncio
async def test_notify_success_posts_expected_payload(settings):
    settings.ha_url = "http://homeassistant.local:8123"
    settings.ha_token = "secret-token"
    settings.ha_notify_service = "mobile_app_max_iphone"

    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        seen["body"] = request.read()
        return httpx.Response(200, json={"ok": True})

    hub = make_hub(settings, handler)
    record = await hub.notify(
        "port-scanner", "scan finished", title="Port Scanner", priority="high", data={"tag": "scan"},
    )

    assert record.ok is True
    assert record.error is None
    assert seen["url"] == "http://homeassistant.local:8123/api/services/notify/mobile_app_max_iphone"
    assert seen["auth"] == "Bearer secret-token"
    import json as _json
    body = _json.loads(seen["body"])
    assert body["message"] == "scan finished"
    assert body["title"] == "Port Scanner"
    assert body["data"] == {"tag": "scan", "priority": "high"}


@pytest.mark.asyncio
async def test_notify_records_error_on_bad_status(settings):
    settings.ha_url = "http://homeassistant.local:8123"
    settings.ha_token = "bad-token"
    settings.ha_notify_service = "mobile_app_max_iphone"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text="unauthorized")

    hub = make_hub(settings, handler)
    record = await hub.notify("port-scanner", "hello")
    assert record.ok is False
    assert "401" in record.error


@pytest.mark.asyncio
async def test_history_persists_across_hub_instances(settings):
    settings.ha_url = "http://homeassistant.local:8123"
    settings.ha_token = "secret"
    settings.ha_notify_service = "mobile_app_max_iphone"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True})

    hub = make_hub(settings, handler)
    await hub.notify("port-scanner", "first")
    await hub.notify("other-tool", "second")

    reloaded = make_hub(settings, handler)
    all_records = reloaded.recent(limit=10)
    assert [r.message for r in all_records] == ["second", "first"]  # most recent first

    only_scanner = reloaded.recent(limit=10, tool_id="port-scanner")
    assert [r.message for r in only_scanner] == ["first"]
