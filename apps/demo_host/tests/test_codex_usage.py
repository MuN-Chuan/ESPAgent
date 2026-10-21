from __future__ import annotations

from espagent_demo.codex_usage import (
    _safe_error,
    normalize_embedded_usage_response,
    normalize_rate_limit_snapshot,
    normalize_rate_limits,
)


def test_rate_limit_payload_is_normalized_for_ui_and_esp32() -> None:
    result = normalize_rate_limits(
        {
            "rateLimitsByLimitId": {
                "codex": {
                    "limitId": "codex",
                    "limitName": "Codex",
                    "planType": "plus",
                    "primary": {"usedPercent": 18, "resetsAt": 123, "windowDurationMins": 300},
                    "secondary": {"usedPercent": 64, "resetsAt": 456, "windowDurationMins": 10080},
                    "credits": {"balance": 12.5, "unlimited": False},
                }
            }
        }
    )

    assert result["plan_type"] == "plus"
    assert result["primary"] == {
        "used_percent": 18,
        "remaining_percent": 82,
        "resets_at": 123,
        "window_duration_minutes": 300,
    }
    assert result["secondary"]["remaining_percent"] == 36
    assert result["quota"] == result["secondary"]
    assert result["credits"]["balance"] == 12.5


def test_rate_limit_percentages_are_clamped() -> None:
    result = normalize_rate_limit_snapshot(
        {"primary": {"usedPercent": 130}, "secondary": {"usedPercent": -5}}
    )

    assert result["primary"]["remaining_percent"] == 0
    assert result["secondary"]["remaining_percent"] == 100


def test_new_plan_response_embedded_in_cli_error_is_recovered_without_identity() -> None:
    result = normalize_embedded_usage_response(
        'decode failed; body={"user_id":"secret-user","email":"secret@example.com",'
        '"plan_type":"prolite","rate_limit":{"primary_window":{"used_percent":25,'
        '"limit_window_seconds":604800,"reset_at":1784781197},"secondary_window":null},'
        '"credits":{"balance":"0","unlimited":false,"has_credits":false}}'
    )

    assert result == {
        "plan_type": "prolite",
        "limit_id": "codex",
        "limit_name": "Codex",
        "primary": {
            "used_percent": 25,
            "remaining_percent": 75,
            "resets_at": 1784781197,
            "window_duration_minutes": 10080,
        },
        "secondary": None,
        "quota": {
            "used_percent": 25,
            "remaining_percent": 75,
            "resets_at": 1784781197,
            "window_duration_minutes": 10080,
        },
        "credits": {"balance": "0", "unlimited": False, "has_credits": False},
    }
    assert "secret" not in str(result)


def test_usage_errors_are_sanitized_before_being_published() -> None:
    error = RuntimeError('failed https://chatgpt.com/backend-api/wham/usage body={"email":"secret@example.com"}')

    message = _safe_error(error)

    assert "secret@example.com" not in message
    assert "body=" not in message
