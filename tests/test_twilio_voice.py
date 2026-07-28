import base64
import hashlib
import hmac

from hoa_voice_agent.voice.twilio_voice import (
    is_valid_twilio_request,
    is_valid_twilio_websocket_request,
    twiml_for_media_stream,
)


def test_twilio_webhook_signature_is_validated() -> None:
    auth_token = "secret"
    webhook_url = "https://example.ngrok.app/twilio/voice"
    form_values = {"CallSid": "CA123", "From": "+15550102000", "To": "+15550101000"}
    payload = webhook_url + "".join(f"{key}{form_values[key]}" for key in sorted(form_values))
    signature = base64.b64encode(
        hmac.new(auth_token.encode(), payload.encode(), hashlib.sha1).digest()
    ).decode()

    assert is_valid_twilio_request(
        auth_token=auth_token,
        url=webhook_url,
        form_values=form_values,
        signature=signature,
    )
    assert not is_valid_twilio_request(
        auth_token=auth_token,
        url=webhook_url,
        form_values=form_values,
        signature="not-a-twilio-signature",
    )


def test_twiml_adds_the_real_caller_to_the_media_stream() -> None:
    twiml = twiml_for_media_stream(
        stream_url="wss://example.ngrok.app/ws",
        from_number="+15550102000",
        to_number="+15550101000",
    )

    assert '<Stream url="wss://example.ngrok.app/ws">' in twiml
    assert 'name="from_number" value="+15550102000"' in twiml
    assert 'name="to_number" value="+15550101000"' in twiml


def test_twilio_websocket_signature_accepts_twilio_trailing_slash_variant() -> None:
    auth_token = "secret"
    stream_url = "wss://example.ngrok.app/ws"
    signature = base64.b64encode(
        hmac.new(auth_token.encode(), f"{stream_url}/".encode(), hashlib.sha1).digest()
    ).decode()

    assert is_valid_twilio_websocket_request(
        auth_token=auth_token,
        stream_url=stream_url,
        signature=signature,
    )
