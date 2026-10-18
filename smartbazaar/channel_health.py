"""Read-only, bounded Meta channel credential checks.

The probe deliberately returns no provider identifiers, URLs, tokens, or response data.
"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum

import httpx


class ChannelHealthState(StrEnum):
    AVAILABLE = "available"
    EXPIRING = "expiring"
    AUTH_REJECTED = "auth_rejected"
    UNAVAILABLE = "unavailable"
    CONFIGURATION_REQUIRED = "configuration_required"


class ChannelHealthReason(StrEnum):
    OK = "ok"
    EXPIRING_SOON = "expiring_soon"
    CONFIGURATION_MISSING = "configuration_missing"
    INVALID_TOKEN = "invalid_token"
    EXPIRED = "expired"
    APP_MISMATCH = "app_mismatch"
    MISSING_SCOPE = "missing_scope"
    PHONE_MISMATCH = "phone_mismatch"
    ACCOUNT_ACCESS = "account_access"
    HTTP_AUTH = "http_auth"
    HTTP_ERROR = "http_error"
    TRANSPORT_ERROR = "transport_error"
    MALFORMED_RESPONSE = "malformed_response"


@dataclass(frozen=True, slots=True)
class ChannelHealth:
    status: ChannelHealthState
    checked_at: datetime
    expires_at: datetime | None
    reason: ChannelHealthReason


Clock = Callable[[], datetime]


class ChannelHealthProbe:
    """Checks token metadata and the configured phone node without changing Meta state."""

    _REQUIRED_SCOPE = "whatsapp_business_messaging"
    _WARNING_WINDOW = timedelta(hours=24)

    def __init__(
        self,
        access_token: str,
        app_id: str,
        app_secret: str,
        phone_id: str,
        graph_version: str,
        *,
        account_id: str = "",
        transport: httpx.AsyncBaseTransport | None = None,
        clock: Clock | None = None,
    ):
        self._access_token = access_token
        self._app_id = app_id
        self._app_secret = app_secret
        self._phone_id = phone_id
        self._account_id = account_id
        self._clock = clock or (lambda: datetime.now(UTC))
        self._client = httpx.AsyncClient(
            base_url=f"https://graph.facebook.com/{graph_version}/",
            timeout=httpx.Timeout(10.0),
            follow_redirects=False,
            transport=transport,
        )

    async def check(self) -> ChannelHealth:
        checked_at = _utc(self._clock())
        if not all((self._access_token, self._app_id, self._app_secret, self._phone_id)):
            return _health(ChannelHealthState.CONFIGURATION_REQUIRED, checked_at, None, ChannelHealthReason.CONFIGURATION_MISSING)

        try:
            response = await self._client.get(
                "debug_token",
                params={"input_token": self._access_token},
                headers={"Authorization": f"Bearer {self._app_id}|{self._app_secret}"},
            )
        except httpx.HTTPError:
            return _health(ChannelHealthState.UNAVAILABLE, checked_at, None, ChannelHealthReason.TRANSPORT_ERROR)
        if response.status_code in {401, 403}:
            return _health(ChannelHealthState.AUTH_REJECTED, checked_at, None, ChannelHealthReason.HTTP_AUTH)
        if response.status_code != 200:
            return _health(ChannelHealthState.UNAVAILABLE, checked_at, None, ChannelHealthReason.HTTP_ERROR)

        parsed = _debug_data(response)
        if parsed is None:
            return _health(ChannelHealthState.UNAVAILABLE, checked_at, None, ChannelHealthReason.MALFORMED_RESPONSE)
        is_valid, returned_app_id, scopes, expires_at = parsed
        if not is_valid:
            return _health(ChannelHealthState.AUTH_REJECTED, checked_at, expires_at, ChannelHealthReason.INVALID_TOKEN)
        if returned_app_id != self._app_id:
            return _health(ChannelHealthState.UNAVAILABLE, checked_at, expires_at, ChannelHealthReason.APP_MISMATCH)
        if self._REQUIRED_SCOPE not in scopes:
            return _health(ChannelHealthState.UNAVAILABLE, checked_at, expires_at, ChannelHealthReason.MISSING_SCOPE)
        if expires_at is not None and expires_at <= checked_at:
            return _health(ChannelHealthState.AUTH_REJECTED, checked_at, expires_at, ChannelHealthReason.EXPIRED)

        try:
            phone = await self._client.get(
                self._phone_id,
                params={"fields": "id"},
                headers={"Authorization": f"Bearer {self._access_token}"},
            )
        except httpx.HTTPError:
            return _health(ChannelHealthState.UNAVAILABLE, checked_at, expires_at, ChannelHealthReason.TRANSPORT_ERROR)
        if phone.status_code in {401, 403}:
            return _health(ChannelHealthState.AUTH_REJECTED, checked_at, expires_at, ChannelHealthReason.HTTP_AUTH)
        if phone.status_code != 200:
            return _health(ChannelHealthState.UNAVAILABLE, checked_at, expires_at, ChannelHealthReason.HTTP_ERROR)
        if _phone_id(phone) != self._phone_id:
            return _health(ChannelHealthState.UNAVAILABLE, checked_at, expires_at, ChannelHealthReason.PHONE_MISMATCH)
        if self._account_id:
            account_status = await self._check_account(checked_at, expires_at)
            if account_status is not None:
                return account_status
        if expires_at is not None and expires_at - checked_at <= self._WARNING_WINDOW:
            return _health(ChannelHealthState.EXPIRING, checked_at, expires_at, ChannelHealthReason.EXPIRING_SOON)
        return _health(ChannelHealthState.AVAILABLE, checked_at, expires_at, ChannelHealthReason.OK)

    async def close(self) -> None:
        await self._client.aclose()

    async def _check_account(
        self, checked_at: datetime, expires_at: datetime | None
    ) -> ChannelHealth | None:
        try:
            account = await self._client.get(
                self._account_id,
                params={"fields": "id"},
                headers={"Authorization": f"Bearer {self._access_token}"},
            )
        except httpx.HTTPError:
            return _health(
                ChannelHealthState.UNAVAILABLE,
                checked_at,
                expires_at,
                ChannelHealthReason.ACCOUNT_ACCESS,
            )
        if account.status_code != 200 or _phone_id(account) != self._account_id:
            return _health(
                ChannelHealthState.UNAVAILABLE,
                checked_at,
                expires_at,
                ChannelHealthReason.ACCOUNT_ACCESS,
            )
        return None


def _debug_data(response: httpx.Response) -> tuple[bool, str, set[str], datetime | None] | None:
    try:
        body = response.json()
    except ValueError:
        return None
    if not isinstance(body, Mapping) or not isinstance(body.get("data"), Mapping):
        return None
    data = body["data"]
    is_valid, app_id, scopes = data.get("is_valid"), data.get("app_id"), data.get("scopes")
    expires, data_access_expires = data.get("expires_at"), data.get("data_access_expires_at")
    if (
        type(is_valid) is not bool
        or not isinstance(app_id, str)
        or not app_id
        or not isinstance(scopes, list)
        or not all(isinstance(scope, str) for scope in scopes)
        or not _epoch(expires)
        or not _epoch(data_access_expires)
    ):
        return None
    positive_expiries = [epoch for epoch in (expires, data_access_expires) if epoch > 0]
    expiry = min(positive_expiries, default=0)
    return is_valid, app_id, set(scopes), datetime.fromtimestamp(expiry, UTC) if expiry else None


def _phone_id(response: httpx.Response) -> str | None:
    try:
        body = response.json()
    except ValueError:
        return None
    value = body.get("id") if isinstance(body, Mapping) else None
    return value if isinstance(value, str) and value else None


def _epoch(value: object) -> bool:
    return type(value) is int and 0 <= value <= 253402300799


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _health(
    status: ChannelHealthState,
    checked_at: datetime,
    expires_at: datetime | None,
    reason: ChannelHealthReason,
) -> ChannelHealth:
    return ChannelHealth(status=status, checked_at=checked_at, expires_at=expires_at, reason=reason)
