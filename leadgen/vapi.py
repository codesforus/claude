"""Minimal Vapi REST client: start an outbound call and read a call back."""

from __future__ import annotations

from typing import Any

import httpx

VAPI_BASE_URL = "https://api.vapi.ai"
_CUSTOMER_NAME_MAX = 40


class VapiError(RuntimeError):
    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


class VapiClient:
    def __init__(
        self,
        api_key: str,
        assistant_id: str,
        phone_number_id: str,
        server_url: str | None = None,
        first_message: str | None = None,
        http: httpx.Client | None = None,
    ):
        if not (api_key and assistant_id and phone_number_id):
            raise VapiError("chybí VAPI_API_KEY, VAPI_ASSISTANT_ID nebo VAPI_PHONE_NUMBER_ID")
        self.assistant_id = assistant_id
        self.phone_number_id = phone_number_id
        self.server_url = server_url
        self.first_message = first_message
        self.http = http or httpx.Client(
            base_url=VAPI_BASE_URL,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=30,
        )

    def _request(self, method: str, path: str, **kwargs: Any) -> dict:
        try:
            response = self.http.request(method, path, **kwargs)
        except httpx.HTTPError as e:
            raise VapiError(f"Vapi nedostupné: {e}") from e
        if response.status_code >= 400:
            raise VapiError(
                f"Vapi {method} {path} -> {response.status_code}: {response.text[:500]}",
                status_code=response.status_code,
            )
        return response.json()

    def create_call(self, phone: str, customer_name: str, variables: dict[str, str]) -> dict:
        """Start an outbound call. `variables` fill {{placeholders}} in the assistant prompt."""
        overrides: dict[str, Any] = {"variableValues": variables}
        if self.server_url:
            overrides["server"] = {"url": self.server_url}
        if self.first_message:
            overrides["firstMessage"] = self.first_message
        body = {
            "assistantId": self.assistant_id,
            "phoneNumberId": self.phone_number_id,
            "customer": {"number": phone, "name": customer_name[:_CUSTOMER_NAME_MAX]},
            "assistantOverrides": overrides,
        }
        return self._request("POST", "/call", json=body)

    def get_call(self, call_id: str) -> dict:
        return self._request("GET", f"/call/{call_id}")
