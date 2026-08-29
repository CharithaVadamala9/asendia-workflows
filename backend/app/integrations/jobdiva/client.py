"""Low-level JobDiva HTTP client.

Handles the three things the API makes awkward: authentication (whose header format is
not actually specified), token refresh (whose TTL is undocumented), and date encoding
(which is not ISO-8601).

Everything above this layer works with normalized models — see `adapter.py`.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Any, Literal

import httpx

from app.config import Settings

log = logging.getLogger(__name__)

# JobDiva's BI endpoints want MM/dd/yyyy HH:mm:ss, not ISO-8601.
JD_DATE_FORMAT = "%m/%d/%Y %H:%M:%S"


def fmt_date(dt: datetime) -> str:
    return dt.strftime(JD_DATE_FORMAT)


class JobDivaError(RuntimeError):
    pass


class JobDivaAuthError(JobDivaError):
    pass


class JobDivaRateLimitError(JobDivaError):
    """Quota exhausted for one endpoint.

    JobDiva meters per endpoint, per tenant, and returns 429 "Request Limit Exceeded".
    Other endpoints keep working, so this is worth distinguishing: the right response
    is to fall back to a different endpoint, not to abandon the integration.
    """


class CallCounter:
    """Counts outbound requests so throughput claims can be measured, not estimated."""

    def __init__(self) -> None:
        self.total = 0
        self.by_path: dict[str, int] = {}

    def record(self, path: str) -> None:
        self.total += 1
        self.by_path[path] = self.by_path.get(path, 0) + 1

    def reset(self) -> None:
        self.total = 0
        self.by_path.clear()


COUNTER = CallCounter()


class JobDivaClient:
    """Authenticated HTTP client for the JobDiva V2 API."""

    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None):
        self.settings = settings
        self.base_url = settings.jobdiva_base_url.rstrip("/")
        self._client = client or httpx.AsyncClient(timeout=30.0)
        self._token: str | None = None
        self._refresh_token: str | None = None
        # The spec declares an apiKey header with no Bearer prefix, but does not say
        # so explicitly. "auto" resolves this on the first authenticated call and
        # then sticks; the discovery spike reports which one won.
        self._scheme: Literal["raw", "bearer", "auto"] = settings.jobdiva_auth_scheme
        self._lock = asyncio.Lock()

    async def aclose(self) -> None:
        await self._client.aclose()

    # -- auth ---------------------------------------------------------------

    async def authenticate(self) -> str:
        """Log in and cache the token.

        Uses POST /apiv2/v2/login rather than GET /apiv2/v2/authenticate: the GET
        carries the password in the query string, where it lands in access and proxy
        logs. Note the field casing differs between the two — the body form is
        camelCase.
        """
        if not all(
            [
                self.settings.jobdiva_client_id,
                self.settings.jobdiva_username,
                self.settings.jobdiva_password,
            ]
        ):
            raise JobDivaAuthError(
                "JobDiva credentials missing — set JOBDIVA_CLIENT_ID, "
                "JOBDIVA_USERNAME and JOBDIVA_PASSWORD in backend/.env"
            )

        payload = {
            "clientId": self.settings.jobdiva_client_id,
            "userName": self.settings.jobdiva_username,
            "password": self.settings.jobdiva_password,
        }
        resp = await self._client.post(f"{self.base_url}/apiv2/v2/login", json=payload)

        # JobDiva returns 500 (not 401) for bad credentials, so status alone cannot
        # distinguish "wrong password" from "server broken" — match on the message.
        if resp.status_code >= 400:
            detail = _extract_message(resp)
            if "username" in detail.lower() or "password" in detail.lower():
                raise JobDivaAuthError(f"JobDiva rejected the credentials: {detail}")
            raise JobDivaError(f"authenticate failed ({resp.status_code}): {detail}")

        data = resp.json()
        if isinstance(data, str):  # legacy endpoints return a bare token string
            self._token = data
        else:
            self._token = data.get("token")
            self._refresh_token = data.get("refreshtoken")
        if not self._token:
            raise JobDivaAuthError(f"no token in authenticate response: {data!r}")
        return self._token

    async def _ensure_token(self) -> str:
        async with self._lock:
            if self._token is None:
                await self.authenticate()
            return self._token  # type: ignore[return-value]

    def _auth_headers(self, scheme: str) -> dict[str, str]:
        value = self._token if scheme == "raw" else f"Bearer {self._token}"
        return {"Authorization": value or ""}

    @property
    def resolved_scheme(self) -> str:
        """Which auth header format actually worked. Reported by the spike."""
        return self._scheme

    # -- requests -----------------------------------------------------------

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: Any | None = None,
        _retry: bool = True,
    ) -> Any:
        """Make an authenticated request, refreshing the token on 401.

        The token TTL is undocumented, so we refresh reactively on 401 rather than
        pre-emptively on a timer.
        """
        await self._ensure_token()
        COUNTER.record(path)
        url = f"{self.base_url}{path}"

        schemes = ["raw", "bearer"] if self._scheme == "auto" else [self._scheme]
        resp: httpx.Response | None = None

        for scheme in schemes:
            resp = await self._client.request(
                method,
                url,
                params=_encode_params(params),
                json=json,
                headers=self._auth_headers(scheme),
            )
            if resp.status_code != 401:
                if self._scheme == "auto":
                    self._scheme = scheme  # type: ignore[assignment]
                    log.info("JobDiva auth scheme resolved to %r", scheme)
                break

        assert resp is not None
        if resp.status_code == 401 and _retry:
            log.info("JobDiva token rejected — re-authenticating once")
            self._token = None
            await self.authenticate()
            return await self.request(
                method, path, params=params, json=json, _retry=False
            )

        if resp.status_code == 429:
            raise JobDivaRateLimitError(
                f"{path} quota exhausted ({_extract_message(resp)}). Limits are "
                f"per-endpoint, so other endpoints may still be available."
            )

        if resp.status_code >= 400:
            raise JobDivaError(
                f"{method} {path} failed ({resp.status_code}): {_extract_message(resp)}"
            )

        if not resp.content:
            return None
        try:
            return resp.json()
        except ValueError:
            return resp.text

    async def get(self, path: str, **params: Any) -> Any:
        return await self.request("GET", path, params=params)

    async def post(
        self, path: str, *, params: dict | None = None, json: Any | None = None
    ) -> Any:
        return await self.request("POST", path, params=params, json=json)


def _encode_params(params: dict[str, Any] | None) -> dict[str, Any] | None:
    """Drop empty params and flatten lists.

    The spec declares array params without a collectionFormat, so the encoding is
    genuinely ambiguous. httpx repeats the key for a list (`?ids=1&ids=2`), which is
    the Swagger default (`multi`); the spike confirms whether JobDiva accepts it.
    """
    if not params:
        return None
    return {k: v for k, v in params.items() if v is not None and v != []}


def _extract_message(resp: httpx.Response) -> str:
    try:
        body = resp.json()
    except ValueError:
        return resp.text[:300]
    if isinstance(body, dict):
        return str(body.get("message") or body.get("error") or body)[:300]
    return str(body)[:300]
