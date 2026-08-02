import json
from collections.abc import Callable

import httpx

from hoa_voice_agent.adapters.protocols import MessagingAdapter


class TwilioMessageError(Exception):
    """A Twilio error with the provider's safe diagnostic code and message."""


class TwilioMessagingAdapter(MessagingAdapter):
    """Send resident confirmations through Twilio's Messages REST API."""

    def __init__(
        self,
        *,
        account_sid: str,
        auth_token: str,
        from_phone: str,
        whatsapp_from: str | None = None,
        whatsapp_content_sid: str | None = None,
        client_factory: Callable[[], httpx.AsyncClient] | None = None,
    ) -> None:
        self._account_sid = account_sid
        self._auth_token = auth_token
        self._from_phone = from_phone
        self._whatsapp_from = whatsapp_from
        self._whatsapp_content_sid = whatsapp_content_sid
        self._client_factory = client_factory or (lambda: httpx.AsyncClient(timeout=10.0))

    async def send_sms(
        self,
        phone: str,
        message: str,
        *,
        idempotency_key: str | None = None,
    ) -> str:
        async with self._client_factory() as client:
            response = await client.post(
                f"https://api.twilio.com/2010-04-01/Accounts/{self._account_sid}/Messages.json",
                auth=httpx.BasicAuth(self._account_sid, self._auth_token),
                data={"To": phone, "From": self._from_phone, "Body": message},
                headers={"Idempotency-Key": idempotency_key} if idempotency_key else None,
            )
            if response.is_error:
                try:
                    error = response.json()
                except ValueError:
                    error = {}
                code = error.get("code", "unknown")
                message = error.get("message", response.reason_phrase)
                raise TwilioMessageError(f"Twilio SMS error {code}: {message}")
        sid = response.json().get("sid")
        if not isinstance(sid, str) or not sid:
            raise ValueError("Twilio returned no message SID")
        return f"twilio:{sid}"

    async def send_whatsapp(
        self,
        phone: str,
        message: str,
        *,
        idempotency_key: str | None = None,
    ) -> str:
        if not self._whatsapp_from:
            raise TwilioMessageError("Twilio WhatsApp sender is not configured")
        normalized_phone = phone.removeprefix("whatsapp:")
        data = {
            "To": f"whatsapp:{normalized_phone}",
            "From": self._whatsapp_from,
        }
        if self._whatsapp_content_sid:
            data["ContentSid"] = self._whatsapp_content_sid
            data["ContentVariables"] = json.dumps({"1": message})
        else:
            data["Body"] = message
        async with self._client_factory() as client:
            response = await client.post(
                f"https://api.twilio.com/2010-04-01/Accounts/{self._account_sid}/Messages.json",
                auth=httpx.BasicAuth(self._account_sid, self._auth_token),
                data=data,
                headers={"Idempotency-Key": idempotency_key} if idempotency_key else None,
            )
            if response.is_error:
                try:
                    error = response.json()
                except ValueError:
                    error = {}
                code = error.get("code", "unknown")
                message = error.get("message", response.reason_phrase)
                raise TwilioMessageError(f"Twilio WhatsApp error {code}: {message}")
        sid = response.json().get("sid")
        if not isinstance(sid, str) or not sid:
            raise ValueError("Twilio returned no WhatsApp message SID")
        return f"twilio:{sid}"
