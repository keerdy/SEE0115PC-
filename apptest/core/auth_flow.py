from __future__ import annotations

from typing import Any

from apptest.clients.p2p_client import P2PClient
from apptest.core.logging_utils import get_logger


def authenticate_device(
    p2p_client: P2PClient,
    device_id: str,
    default_secret_key: str,
    app_version: str,
    auth_wait_timeout_seconds: int,
) -> dict[str, Any]:
    logger = get_logger("pocket_app_automation.auth")
    logger.info("starting authentication device_id=%s app_version=%s", device_id, app_version)
    challenge_response = p2p_client.get_challenge()
    challenge_response.raise_for_status()
    challenge_json = challenge_response.json()
    challenge_data = challenge_json.get("data", {})
    if not isinstance(challenge_data, dict):
        raise ValueError(f"device challenge response missing 'data' field: {challenge_json}")

    auth_session_id = challenge_data.get("auth_session_id")
    challenge_code = challenge_data.get("challenge_code")
    if not auth_session_id or not challenge_code:
        raise ValueError(
            f"device challenge response missing required fields: {challenge_data}"
        )
    resolved_device_id = challenge_data.get("device_id") or device_id
    logger.info(
        "challenge received auth_session_id=%s challenge_code=%s resolved_device_id=%s",
        auth_session_id,
        challenge_code,
        resolved_device_id,
    )

    auth_event = p2p_client.wait_for_auth_confirmation(auth_session_id, auth_wait_timeout_seconds)
    if auth_event.event != "auth_confirmed":
        logger.error("authorization failed event=%s raw=%s", auth_event.event, auth_event.raw)
        raise AssertionError(f"Authorization not confirmed: {auth_event.raw}")
    logger.info("authorization confirmed auth_session_id=%s", auth_session_id)

    auth_response = p2p_client.post_auth(
        auth_session_id=auth_session_id,
        device_id=resolved_device_id,
        challenge_code=challenge_code,
        default_secret_key=default_secret_key,
        app_version=app_version,
    )
    auth_response.raise_for_status()
    auth_json = auth_response.json()
    auth_data = auth_json.get("data")
    if not isinstance(auth_data, dict):
        raise ValueError(f"auth response missing 'data' field: {auth_json}")
    token = auth_data.get("session_token")
    if not token:
        raise ValueError(f"auth response missing 'session_token': {auth_data}")
    logger.info("authentication succeeded auth_session_id=%s token_received=%s", auth_session_id, bool(token))

    return {
        "challenge_response": challenge_response,
        "challenge_json": challenge_json,
        "auth_event": auth_event,
        "auth_response": auth_response,
        "auth_json": auth_json,
        "token": token,
        "device_id": resolved_device_id,
        "auth_session_id": auth_session_id,
        "challenge_code": challenge_code,
    }
