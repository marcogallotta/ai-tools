from __future__ import annotations

import hashlib
import logging

import pytest

import dish_service.application as application_module
from dish_service.command_spec import validate_action_request
from dish_service.file_transport import FetchedFile, _reject_unsafe_address
from dish_tool.errors import DishRuleError
from tests.support.service_scenarios import post as _post, running as _running
from tests.support.thread_teardown import stop_server


RUN_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
REQUEST_ID = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
OTHER_REQUEST_ID = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"

FILE_REF = {
    "id": "file-123",
    "name": "receipt.txt",
    "mime_type": "text/plain",
    "download_link": "https://files.example.invalid/signed/abc",
}
CONTENT = b"hello gate a"
SHA256 = hashlib.sha256(CONTENT).hexdigest()


def _payload(*, request_id=REQUEST_ID, expected_sha256=SHA256, expected_bytes=len(CONTENT), file_ref=None):
    return {
        "client": {"run_id": RUN_ID, "request_id": request_id},
        "arguments": {"expected_sha256": expected_sha256, "expected_bytes": expected_bytes},
        "openaiFileIdRefs": [file_ref if file_ref is not None else dict(FILE_REF)],
    }


# --- validate_action_request unit coverage (no server, no network) ---


def test_validate_rejects_missing_openai_file_refs():
    request = {
        "client": {"run_id": RUN_ID, "request_id": REQUEST_ID},
        "arguments": {"expected_sha256": SHA256, "expected_bytes": len(CONTENT)},
    }
    with pytest.raises(DishRuleError) as excinfo:
        validate_action_request("qualify-file-transport", request)
    assert excinfo.value.rule == "openai_file_refs_invalid"


def test_validate_rejects_extra_file_ref_field():
    request = _payload(file_ref={**FILE_REF, "extra": "nope"})
    with pytest.raises(DishRuleError) as excinfo:
        validate_action_request("qualify-file-transport", request)
    assert excinfo.value.rule == "openai_file_refs_invalid"


def test_validate_rejects_uppercase_sha256():
    request = _payload(expected_sha256=SHA256.upper())
    with pytest.raises(DishRuleError) as excinfo:
        validate_action_request("qualify-file-transport", request)
    assert excinfo.value.rule == "argument_value_invalid"


def test_validate_rejects_non_hex_sha256():
    request = _payload(expected_sha256="z" * 64)
    with pytest.raises(DishRuleError) as excinfo:
        validate_action_request("qualify-file-transport", request)
    assert excinfo.value.rule == "argument_value_invalid"


def test_validate_rejects_zero_expected_bytes():
    request = _payload(expected_bytes=0)
    with pytest.raises(DishRuleError) as excinfo:
        validate_action_request("qualify-file-transport", request)
    assert excinfo.value.rule == "argument_value_invalid"


def test_validate_rejects_float_expected_bytes():
    request = _payload(expected_bytes=12.5)
    with pytest.raises(DishRuleError) as excinfo:
        validate_action_request("qualify-file-transport", request)
    assert excinfo.value.rule == "argument_value_invalid"


def test_validate_accepts_well_formed_request():
    request = _payload()
    client, arguments = validate_action_request("qualify-file-transport", request)
    assert client == {"run_id": RUN_ID, "request_id": REQUEST_ID}
    assert arguments["expected_sha256"] == SHA256
    assert arguments["expected_bytes"] == len(CONTENT)
    assert arguments["file"] == FILE_REF


# --- SSRF address rejection (no network) ---


@pytest.mark.parametrize(
    "address",
    ["127.0.0.1", "10.0.0.5", "169.254.1.1", "::1", "0.0.0.0", "224.0.0.1"],
)
def test_reject_unsafe_address_blocks_non_public_ranges(address):
    with pytest.raises(DishRuleError) as excinfo:
        _reject_unsafe_address(address)
    assert excinfo.value.rule == "file_transport_address_forbidden"


def test_reject_unsafe_address_allows_public_range():
    _reject_unsafe_address("93.184.216.34")


# --- End-to-end over HTTP, with fetch_expected_file monkeypatched (hermetic) ---


@pytest.fixture
def running_service(tmp_path):
    instance, backend, server, thread, url = _running(
        tmp_path, action_client_id="implementation-action"
    )
    yield instance, url
    stop_server(server, thread)


def _patch_fetch(monkeypatch, *, sha256=SHA256, byte_count=len(CONTENT), error=None):
    def _fake(download_link, *, expected_bytes):
        if error is not None:
            raise error
        return FetchedFile(sha256=sha256, byte_count=byte_count)

    monkeypatch.setattr(application_module, "fetch_expected_file", _fake)


def test_successful_qualification_returns_receipt(running_service, monkeypatch):
    _instance, url = running_service
    _patch_fetch(monkeypatch)
    status, payload = _post(
        url, "/v1/action/qualify-file-transport", token="action-secret", payload=_payload()
    )
    assert status == 200
    assert payload["ok"] is True
    assert payload["data"]["sha256"] == SHA256
    assert payload["data"]["byte_count"] == len(CONTENT)
    assert payload["data"]["request_id"] == REQUEST_ID
    [receipt] = payload["openaiFileResponse"]
    assert receipt["name"] == "dish-action-gate-a-receipt.json"
    assert receipt["mime_type"] == "application/json"
    assert "download_link" not in receipt["content"]


def test_missing_file_ref_logs_only_safe_shape(running_service, caplog):
    _instance, url = running_service
    payload = _payload()
    payload.pop("openaiFileIdRefs")
    with caplog.at_level(logging.INFO, logger="dish.service"):
        status, response = _post(
            url,
            "/v1/action/qualify-file-transport",
            token="action-secret",
            payload=payload,
        )
    assert status == 200
    assert response["errors"][0]["rule"] == "openai_file_refs_invalid"
    assert "action_file_transport_received" in caplog.text
    assert "action_file_transport_rejected" in caplog.text
    assert f"run_id={RUN_ID}" in caplog.text
    assert f"request_id={REQUEST_ID}" in caplog.text
    assert "file_refs_type=missing" in caplog.text
    assert "file_refs_count=None" in caplog.text
    assert "rule=openai_file_refs_invalid" in caplog.text
    assert "download_link" not in caplog.text


def test_file_ref_log_never_contains_file_values(running_service, monkeypatch, caplog):
    _instance, url = running_service
    _patch_fetch(monkeypatch)
    with caplog.at_level(logging.INFO, logger="dish.service"):
        status, response = _post(
            url,
            "/v1/action/qualify-file-transport",
            token="action-secret",
            payload=_payload(),
        )
    assert status == 200
    assert response["ok"] is True
    assert "file_refs_type=array" in caplog.text
    assert "file_refs_count=1" in caplog.text
    assert "file_ref_item_type=dict" in caplog.text
    for secret_value in FILE_REF.values():
        assert secret_value not in caplog.text


def test_digest_mismatch_is_validation_failed(running_service, monkeypatch):
    _instance, url = running_service
    _patch_fetch(monkeypatch, sha256="0" * 64)
    status, payload = _post(
        url,
        "/v1/action/qualify-file-transport",
        token="action-secret",
        payload=_payload(request_id=OTHER_REQUEST_ID),
    )
    assert status == 200
    assert payload["ok"] is False
    assert payload["code"] == "VALIDATION_FAILED"
    assert payload["errors"][0]["rule"] == "file_transport_digest_mismatch"


def test_byte_count_mismatch_is_validation_failed(running_service, monkeypatch):
    _instance, url = running_service
    _patch_fetch(monkeypatch, byte_count=len(CONTENT) + 1)
    status, payload = _post(
        url,
        "/v1/action/qualify-file-transport",
        token="action-secret",
        payload=_payload(request_id="dddddddd-dddd-4ddd-8ddd-dddddddddddd"),
    )
    assert status == 200
    assert payload["ok"] is False
    assert payload["code"] == "VALIDATION_FAILED"
    assert payload["errors"][0]["rule"] == "file_transport_bytes_mismatch"


def test_exact_replay_returns_stored_result_without_refetching(running_service, monkeypatch):
    _instance, url = running_service
    calls = []

    def _fake(download_link, *, expected_bytes):
        calls.append(download_link)
        return FetchedFile(sha256=SHA256, byte_count=len(CONTENT))

    monkeypatch.setattr(application_module, "fetch_expected_file", _fake)
    payload = _payload(request_id="eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee")
    first = _post(url, "/v1/action/qualify-file-transport", token="action-secret", payload=payload)
    second = _post(url, "/v1/action/qualify-file-transport", token="action-secret", payload=payload)
    assert first[0] == second[0] == 200
    assert first[1]["data"]["sha256"] == second[1]["data"]["sha256"] == SHA256
    assert second[1]["data"]["request_replayed"] is True
    assert len(calls) == 1


def test_changed_reuse_conflicts(running_service, monkeypatch):
    _instance, url = running_service
    _patch_fetch(monkeypatch)
    request_id = "ffffffff-ffff-4fff-8fff-ffffffffffff"
    first_payload = _payload(request_id=request_id)
    second_payload = _payload(request_id=request_id, expected_bytes=len(CONTENT) + 5)
    first = _post(url, "/v1/action/qualify-file-transport", token="action-secret", payload=first_payload)
    second = _post(url, "/v1/action/qualify-file-transport", token="action-secret", payload=second_payload)
    assert first[0] == 200
    assert first[1]["ok"] is True
    assert second[0] == 200
    assert second[1]["ok"] is False
    assert second[1]["code"] == "CONFLICT"


def test_download_link_is_not_part_of_replay_identity(running_service, monkeypatch):
    """A rotated signed download_link must not change request identity or appear in results."""
    _instance, url = running_service
    _patch_fetch(monkeypatch)
    request_id = "12345678-1234-4123-8123-123456789012"
    first_payload = _payload(request_id=request_id)
    rotated_ref = {**FILE_REF, "download_link": "https://files.example.invalid/signed/rotated"}
    second_payload = _payload(request_id=request_id, file_ref=rotated_ref)
    first = _post(url, "/v1/action/qualify-file-transport", token="action-secret", payload=first_payload)
    second = _post(url, "/v1/action/qualify-file-transport", token="action-secret", payload=second_payload)
    assert first[0] == second[0] == 200
    assert first[1]["ok"] is True
    assert second[1]["ok"] is True
    assert second[1]["data"]["request_replayed"] is True
