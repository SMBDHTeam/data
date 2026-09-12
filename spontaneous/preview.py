from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID, uuid4

from fastapi import HTTPException


log = logging.getLogger("data.spontaneous.preview")
TOKEN_VERSION = "v1"
DEFAULT_TTL_SECONDS = 20 * 60
MIN_SECRET_BYTES = 32
_EPHEMERAL_SECRET = secrets.token_bytes(32)
_warned_ephemeral_secret = False


def create_preview_token(
    snapshot: dict[str, Any],
    owner_id: int | None,
) -> tuple[UUID, str, datetime]:
    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(seconds=preview_ttl_seconds())
    preview_id = uuid4()
    payload = {
        "version": 1,
        "previewId": str(preview_id),
        "ownerId": owner_id,
        "issuedAt": now.isoformat(),
        "expiresAt": expires_at.isoformat(),
        "snapshot": snapshot,
    }
    encoded = _b64encode(_canonical_json(payload))
    signature = _b64encode(hmac.new(_secret(), encoded.encode("ascii"), hashlib.sha256).digest())
    return preview_id, f"{TOKEN_VERSION}.{encoded}.{signature}", expires_at


def verify_preview_token(
    token: str,
    preview_id: UUID,
    owner_id: int,
) -> dict[str, Any]:
    try:
        version, encoded, supplied_signature = token.split(".", 2)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="SPONTANEOUS_PREVIEW_INVALID") from exc
    if version != TOKEN_VERSION:
        raise HTTPException(status_code=400, detail="SPONTANEOUS_PREVIEW_INVALID")

    expected = _b64encode(hmac.new(_secret(), encoded.encode("ascii"), hashlib.sha256).digest())
    if not hmac.compare_digest(supplied_signature, expected):
        raise HTTPException(status_code=400, detail="SPONTANEOUS_PREVIEW_INVALID")
    try:
        payload = json.loads(_b64decode(encoded))
        token_preview_id = UUID(payload["previewId"])
        expires_at = datetime.fromisoformat(payload["expiresAt"])
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=400, detail="SPONTANEOUS_PREVIEW_INVALID") from exc
    if token_preview_id != preview_id:
        raise HTTPException(status_code=400, detail="SPONTANEOUS_PREVIEW_INVALID")
    if payload.get("ownerId") != owner_id:
        raise HTTPException(status_code=403, detail="SPONTANEOUS_PREVIEW_OWNER_MISMATCH")
    if expires_at <= datetime.now(timezone.utc):
        raise HTTPException(status_code=410, detail="SPONTANEOUS_PREVIEW_EXPIRED")
    snapshot = payload.get("snapshot")
    if not isinstance(snapshot, dict):
        raise HTTPException(status_code=400, detail="SPONTANEOUS_PREVIEW_INVALID")
    return snapshot


def preview_request_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def build_course_snapshot(request, destination, timeline: dict[str, Any]) -> dict[str, Any]:
    course = []
    for stop in timeline["course"]:
        raw = stop.get("raw") or {}
        address = " ".join(
            value.strip()
            for value in (str(raw.get("addr1") or ""), str(raw.get("addr2") or ""))
            if value.strip()
        ) or None
        place_snapshot = {
            "source": "TOUR_API",
            "externalContentId": str(stop.get("contentId") or ""),
            "contentTypeId": str(stop.get("contentTypeId") or "") or None,
            "name": stop.get("name"),
            "category": raw.get("cat3"),
            "address": address,
            "longitude": stop.get("longitude"),
            "latitude": stop.get("latitude"),
            "primaryImageUrl": raw.get("firstimage") or raw.get("firstimage2"),
        }
        course.append(
            {
                **{
                    key: value
                    for key, value in stop.items()
                    if key not in {
                        "raw", "score", "returnTravelMinutes", "inboundMinutes",
                        "arriveAt", "departAt",
                    } and not key.startswith("_")
                },
                "placeSnapshot": place_snapshot,
            }
        )
    return {
        "request": request.model_dump(mode="json"),
        "destination": {
            "destinationId": destination.destination_id,
            "name": destination.name,
        },
        "course": course,
        "returnTravelMinutes": timeline["returnTravelMinutes"],
        "estimatedReturnAt": timeline["estimatedReturnAt"].isoformat(),
        "finalTransit": timeline["finalTransit"],
    }


def public_preview_course(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    result = []
    for item in snapshot["course"]:
        place_snapshot = item["placeSnapshot"]
        private_fields = {
            "placeSnapshot",
            "score",
            "returnTravelMinutes",
            "inboundMinutes",
            "arriveAt",
            "departAt",
        }
        result.append(
            {
                **{
                    key: value
                    for key, value in item.items()
                    if key not in private_fields and not key.startswith("_")
                },
                "place": {
                    "id": None,
                    "name": place_snapshot["name"],
                    "category": place_snapshot.get("contentTypeId"),
                    "categoryLabel": _category_label(place_snapshot.get("contentTypeId")),
                    "address": place_snapshot.get("address"),
                    "longitude": place_snapshot["longitude"],
                    "latitude": place_snapshot["latitude"],
                    "primaryImageUrl": place_snapshot.get("primaryImageUrl"),
                    "operatingInfo": None,
                },
            }
        )
    return result


def public_preview_route_lines(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    lines = []
    transits = [item.get("inboundTransit") for item in snapshot["course"]]
    transits.append(snapshot.get("finalTransit"))
    for transit in transits:
        if not transit:
            continue
        for index, line in enumerate(transit.get("route_lines") or [], start=1):
            lines.append(
                {
                    "dayNo": 1,
                    "routeOrder": transit["routeOrder"],
                    "lineOrder": index,
                    **line,
                }
            )
    return lines


def preview_ttl_seconds() -> int:
    raw = os.getenv("SPONTANEOUS_PREVIEW_TTL_SECONDS", str(DEFAULT_TTL_SECONDS))
    try:
        return max(60, min(int(raw), 24 * 60 * 60))
    except ValueError:
        return DEFAULT_TTL_SECONDS


def _secret() -> bytes:
    global _warned_ephemeral_secret
    configured = os.getenv("SPONTANEOUS_PREVIEW_SECRET")
    if configured:
        secret = configured.encode("utf-8")
        if len(secret) < MIN_SECRET_BYTES:
            raise RuntimeError(
                f"SPONTANEOUS_PREVIEW_SECRET must be at least {MIN_SECRET_BYTES} bytes"
            )
        return secret
    if not _warned_ephemeral_secret:
        log.warning(
            "SPONTANEOUS_PREVIEW_SECRET is not configured; using a process-local secret. "
            "Set it consistently on every instance before production use."
        )
        _warned_ephemeral_secret = True
    return _EPHEMERAL_SECRET


def _canonical_json(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _b64decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _category_label(content_type_id: Any) -> str:
    return {
        "12": "관광지",
        "14": "문화시설",
        "15": "축제·공연",
        "28": "레포츠",
        "32": "숙박",
        "38": "쇼핑",
        "39": "음식점",
    }.get(str(content_type_id), "관광지")
