from __future__ import annotations

import base64
from collections.abc import Mapping
from datetime import date, timedelta
from typing import Any
from urllib.parse import urlencode, urljoin

import httpx

from .models import Config, TransactionBundle


class PolarApiError(RuntimeError):
    def __init__(self, message: str, *, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


class PolarClient:
    def __init__(self, config: Config, access_token: str | None = None, http_client: httpx.Client | None = None):
        self.config = config
        self.access_token = access_token
        self._client = http_client or httpx.Client(timeout=30.0)

    def build_authorization_url(self, redirect_uri: str | None = None, oauth_state: str | None = None) -> str:
        params = {
            "response_type": "code",
            "client_id": self.config.client_id,
            "redirect_uri": redirect_uri or self.config.redirect_uri,
        }
        if oauth_state:
            params["state"] = oauth_state
        query = urlencode(params)
        return f"{self.config.auth_base_url.rstrip('/')}/oauth2/authorization?{query}"

    def exchange_code(self, code: str, redirect_uri: str | None = None) -> dict[str, Any]:
        credentials = f"{self.config.client_id}:{self.config.client_secret}".encode("utf-8")
        basic_auth = base64.b64encode(credentials).decode("ascii")
        response = self._client.post(
            "https://polarremote.com/v2/oauth2/token",
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": redirect_uri or self.config.redirect_uri,
            },
            headers={
                "Authorization": f"Basic {basic_auth}",
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json;charset=UTF-8",
            },
        )
        return self._json_or_raise(response, "token exchange failed")

    def register_user(self, member_id: str) -> dict[str, Any]:
        response = self._client.post(
            f"{self.config.base_url.rstrip('/')}/v3/users",
            json={"member-id": member_id},
            headers=self._bearer_headers(),
        )
        return self._json_or_raise(response, "user registration failed")

    def get_user_info(self) -> dict[str, Any]:
        response = self._client.get(
            f"{self.config.base_url.rstrip('/')}/v3/users",
            headers=self._bearer_headers(),
        )
        return self._json_or_raise(response, "user info request failed")

    def get_exercises(self, since_days: int | None = None) -> list[dict[str, Any]]:
        params: dict[str, str] = {
            "samples": "true",
            "zones": "true",
            "route": "true",
        }
        if since_days is not None:
            params["from"] = (date.today() - timedelta(days=since_days)).isoformat()

        try:
            payload = self._get_collection_from_path("/v3/exercises", params=params, error_message="exercise fetch failed")
        except PolarApiError as exc:
            if exc.status_code != 400:
                raise
            fallback_params = dict(params)
            fallback_params.pop("samples", None)
            fallback_params.pop("zones", None)
            fallback_params.pop("route", None)
            payload = self._get_collection_from_path(
                "/v3/exercises",
                params=fallback_params,
                error_message="exercise fetch failed",
            )
        return self._extract_collection_items(payload, "exercises")

    def get_activity(self, since_days: int | None = None) -> list[dict[str, Any]]:
        params: dict[str, str] = {}
        if since_days is not None:
            params["from"] = (date.today() - timedelta(days=since_days)).isoformat()
        payload = self._get_collection_from_path("/v3/users/activity", params=params, error_message="activity fetch failed")
        return self._extract_collection_items(payload, "activities", "activity")

    def get_sleep(self, polar_user_id: str, since_days: int | None = None) -> list[dict[str, Any]]:
        del polar_user_id
        params: dict[str, str] = {}
        if since_days is not None:
            params["from"] = (date.today() - timedelta(days=since_days)).isoformat()
        payload = self._get_collection_from_path("/v3/users/sleep", params=params, error_message="sleep fetch failed")
        return self._extract_collection_items(payload, "nights", "sleep")

    def get_nightly_recharge(self, polar_user_id: str, since_days: int | None = None) -> list[dict[str, Any]]:
        del polar_user_id
        params: dict[str, str] = {}
        if since_days is not None:
            params["from"] = (date.today() - timedelta(days=since_days)).isoformat()
        payload = self._get_collection_from_path(
            "/v3/users/nightly-recharge",
            params=params,
            error_message="nightly-recharge fetch failed",
        )
        return self._extract_collection_items(payload, "recharges", "nightly-recharge", "nightly_recharge")

    def close(self) -> None:
        self._client.close()

    def _get_collection_from_path(
        self,
        path: str,
        *,
        params: dict[str, str] | None = None,
        error_message: str,
    ) -> Any:
        response = self._client.get(
            f"{self.config.base_url.rstrip('/')}{path}",
            params=params or None,
            headers=self._bearer_headers(),
        )
        return self._json_or_raise(response, error_message)

    @staticmethod
    def _extract_collection_items(payload: Any, *keys: str) -> list[dict[str, Any]]:
        if isinstance(payload, list):
            return [item for item in payload if isinstance(item, Mapping)]
        if isinstance(payload, Mapping):
            for key in (*keys, "items"):
                value = payload.get(key)
                if isinstance(value, list):
                    return [item for item in value if isinstance(item, Mapping)]
        joined = ", ".join(keys) or "items"
        raise PolarApiError(f"Unexpected collection response shape while looking for {joined}")

    def open_transaction(self, polar_user_id: str, resource_path: str, list_key: str) -> TransactionBundle:
        open_response = self._client.post(
            f"{self.config.base_url.rstrip('/')}/v3/users/{polar_user_id}/{resource_path}-transactions",
            headers=self._bearer_headers(),
        )
        open_payload = self._json_or_raise(open_response, f"{resource_path} transaction open failed")
        transaction_url = self._extract_url(open_response, open_payload)
        if not transaction_url:
            raise PolarApiError(
                f"{resource_path} transaction did not provide a transaction URL",
                status_code=open_response.status_code,
            )

        listing_response = self._client.get(transaction_url, headers=self._bearer_headers())
        listing_payload = self._json_or_raise(listing_response, f"{resource_path} transaction listing failed")
        item_urls = self._extract_item_urls(listing_payload, list_key)
        commit_url = self._extract_commit_url(listing_payload) or transaction_url
        return TransactionBundle(
            resource=resource_path,
            transaction_url=transaction_url,
            commit_url=commit_url,
            item_urls=item_urls,
            raw_open=open_payload,
            raw_listing=listing_payload,
        )

    def fetch_resource(self, url: str) -> dict[str, Any]:
        response = self._client.get(url, headers=self._bearer_headers())
        return self._json_or_raise(response, f"resource fetch failed for {url}")

    def commit_transaction(self, bundle: TransactionBundle) -> dict[str, Any]:
        response = self._client.put(bundle.commit_url, headers=self._bearer_headers())
        return self._json_or_raise(response, f"{bundle.resource} transaction commit failed")

    def _bearer_headers(self) -> dict[str, str]:
        if not self.access_token:
            raise PolarApiError("Missing access token")
        return {"Authorization": f"Bearer {self.access_token}"}

    @staticmethod
    def _json_or_raise(response: httpx.Response, message: str) -> Any:
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            status_code = exc.response.status_code
            reason = exc.response.reason_phrase or "request failed"
            raise PolarApiError(f"{message}: HTTP {status_code} {reason}", status_code=status_code) from exc
        if not response.content:
            return {}
        try:
            return response.json()
        except ValueError as exc:
            raise PolarApiError(f"{message}: invalid JSON response") from exc

    def _extract_url(self, response: httpx.Response, payload: Mapping[str, Any]) -> str | None:
        location = response.headers.get("Location")
        if location:
            return self._absolute_url(location)
        for key in ("transaction-location", "transaction_location", "url", "href", "resource-uri"):
            value = payload.get(key)
            if isinstance(value, str):
                return self._absolute_url(value)
        return None

    def _extract_commit_url(self, payload: Mapping[str, Any]) -> str | None:
        for key in ("commit-location", "commit_location", "commit-url", "commit_url"):
            value = payload.get(key)
            if isinstance(value, str):
                return self._absolute_url(value)
        return None

    def _extract_item_urls(self, payload: Mapping[str, Any], list_key: str) -> list[str]:
        candidates: list[Any] = []
        for key in (list_key, list_key.replace("-", "_"), "items"):
            value = payload.get(key)
            if isinstance(value, list):
                candidates = value
                break
        item_urls: list[str] = []
        for item in candidates:
            if isinstance(item, str):
                item_urls.append(self._absolute_url(item))
                continue
            if isinstance(item, Mapping):
                for key in ("url", "href", "resource-uri", "resource_uri"):
                    value = item.get(key)
                    if isinstance(value, str):
                        item_urls.append(self._absolute_url(value))
                        break
        return item_urls

    def _absolute_url(self, value: str) -> str:
        if value.startswith("http://") or value.startswith("https://"):
            return value
        return urljoin(f"{self.config.base_url.rstrip('/')}/", value.lstrip("/"))
