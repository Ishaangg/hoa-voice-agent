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
        client_factory: Callable[[], httpx.AsyncClient] | None = None,
    ) -> None:
        self._account_sid = account_sid
        self._auth_token = auth_token
        self._from_phone = from_phone
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
