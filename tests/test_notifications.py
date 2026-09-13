import httpx
import pytest

from app.notifications import NotificationHub


def make_hub(settings, handler=None) -> NotificationHub:
    transport = httpx.MockTransport(handler) if handler else None
    return NotificationHub(settings, transport=transport)


def test_not_configured_by_default(settings):
    hub = make_hub(settings)
    assert hub.configured() is False


def test_configured_once_url_token_and_a_device_are_set(settings):
    settings.ha_url = "http://homeassistant.local:8123"
    settings.ha_token = "secret"
    settings.ha_notify_devices = [{"id": "iphone", "label": "iPhone", "service": "mobile_app_max_iphone"}]
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
async def test_notify_without_any_device_fails_without_hitting_network(settings):
    settings.ha_url = "http://homeassistant.local:8123"
    settings.ha_token = "secret"

    def handler(request):
        raise AssertionError("should not make a network call with no device configured")

    hub = make_hub(settings, handler)
    record = await hub.notify("port-scanner", "hello")
    assert record.ok is False
    assert "device" in record.error


@pytest.mark.asyncio
async def test_notify_success_posts_expected_payload(settings):
    settings.ha_url = "http://homeassistant.local:8123"
    settings.ha_token = "secret-token"
    settings.ha_notify_devices = [{"id": "iphone", "label": "iPhone", "service": "mobile_app_max_iphone"}]

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
    assert record.devices == ["iPhone"]
    assert seen["url"] == "http://homeassistant.local:8123/api/services/notify/mobile_app_max_iphone"
    assert seen["auth"] == "Bearer secret-token"
    import json as _json
    body = _json.loads(seen["body"])
    assert body["message"] == "scan finished"
    assert body["title"] == "Port Scanner"
    assert body["data"] == {"tag": "scan", "priority": "high"}


@pytest.mark.asyncio
async def test_notify_fans_out_to_every_configured_device_by_default(settings):
    settings.ha_url = "http://homeassistant.local:8123"
    settings.ha_token = "secret-token"
    settings.ha_notify_devices = [
        {"id": "iphone", "label": "iPhone", "service": "mobile_app_max_iphone"},
        {"id": "ipad", "label": "iPad", "service": "mobile_app_max_ipad"},
    ]

    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, json={"ok": True})

    hub = make_hub(settings, handler)
    record = await hub.notify("port-scanner", "scan finished")

    assert record.ok is True
    assert sorted(record.devices) == ["iPad", "iPhone"]
    assert len(seen) == 2


@pytest.mark.asyncio
async def test_notify_uses_default_devices_when_neither_call_nor_tool_names_one(settings):
    settings.ha_url = "http://homeassistant.local:8123"
    settings.ha_token = "secret-token"
    settings.ha_notify_devices = [
        {"id": "iphone", "label": "iPhone", "service": "mobile_app_max_iphone"},
        {"id": "ipad", "label": "iPad", "service": "mobile_app_max_ipad"},
    ]
    settings.ha_default_device_ids = ["ipad"]

    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, json={"ok": True})

    hub = make_hub(settings, handler)
    record = await hub.notify("port-scanner", "scan finished")

    assert record.devices == ["iPad"]
    assert seen == ["http://homeassistant.local:8123/api/services/notify/mobile_app_max_ipad"]


@pytest.mark.asyncio
async def test_notify_tool_devices_win_over_default_but_lose_to_an_explicit_request(settings):
    settings.ha_url = "http://homeassistant.local:8123"
    settings.ha_token = "secret-token"
    settings.ha_notify_devices = [
        {"id": "iphone", "label": "iPhone", "service": "mobile_app_max_iphone"},
        {"id": "ipad", "label": "iPad", "service": "mobile_app_max_ipad"},
    ]
    settings.ha_default_device_ids = ["ipad"]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True})

    hub = make_hub(settings, handler)

    via_tool = await hub.notify("port-scanner", "scan finished", tool_devices=["iphone"])
    assert via_tool.devices == ["iPhone"]

    via_request = await hub.notify(
        "port-scanner", "scan finished", devices=["iphone"], tool_devices=["ipad"],
    )
    assert via_request.devices == ["iPhone"]


@pytest.mark.asyncio
async def test_notify_records_error_on_bad_status(settings):
    settings.ha_url = "http://homeassistant.local:8123"
    settings.ha_token = "bad-token"
    settings.ha_notify_devices = [{"id": "iphone", "label": "iPhone", "service": "mobile_app_max_iphone"}]

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
    settings.ha_notify_devices = [{"id": "iphone", "label": "iPhone", "service": "mobile_app_max_iphone"}]

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
