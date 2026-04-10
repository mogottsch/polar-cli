from __future__ import annotations

import base64
from collections.abc import Mapping
from datetime import date, timedelta
from typing import Any
from urllib.parse import urlencode, urljoin

import httpx

from .models import Config, TransactionBundle


class PolarApiError(RuntimeError):
    pass


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
            f"{self.config.base_url.rstrip('/')}/v3/oauth2/token",
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": redirect_uri or self.config.redirect_uri,
            },
            headers={"Authorization": f"Basic {basic_auth}"},
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

    def open_transaction(self, polar_user_id: str, resource_path: str, list_key: str) -> TransactionBundle:
        open_response = self._client.post(
            f"{self.config.base_url.rstrip('/')}/v3/users/{polar_user_id}/{resource_path}-transactions",
            headers=self._bearer_headers(),
        )
        open_payload = self._json_or_raise(open_response, f"{resource_path} transaction open failed")
        transaction_url = self._extract_url(open_response, open_payload)
        if not transaction_url:
            raise PolarApiError(f"{resource_path} transaction did not provide a transaction URL")

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

    def get_sleep(self, polar_user_id: str, since_days: int | None = None) -> list[dict[str, Any]]:
        return self._get_collection(polar_user_id, "sleep", since_days)

    def get_nightly_recharge(self, polar_user_id: str, since_days: int | None = None) -> list[dict[str, Any]]:
        return self._get_collection(polar_user_id, "nightly-recharge", since_days)

    def close(self) -> None:
        self._client.close()

    def _get_collection(self, polar_user_id: str, resource: str, since_days: int | None) -> list[dict[str, Any]]:
        params: dict[str, str] = {}
        if since_days is not None:
            params["from"] = (date.today() - timedelta(days=since_days)).isoformat()
        response = self._client.get(
            f"{self.config.base_url.rstrip('/')}/v3/users/{polar_user_id}/{resource}",
            params=params,
            headers=self._bearer_headers(),
        )
        payload = self._json_or_raise(response, f"{resource} fetch failed")
        if isinstance(payload, list):
            return payload
        if isinstance(payload, Mapping):
            for key in (resource, resource.replace("-", "_"), "items"):
                value = payload.get(key)
                if isinstance(value, list):
                    return value
        raise PolarApiError(f"Unexpected {resource} response shape")

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
            raise PolarApiError(f"{message}: HTTP {status_code} {reason}") from exc
        if not response.content:
            return {}
        return response.json()

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
