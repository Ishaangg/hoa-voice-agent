# HOA Voice Agent - Setup Guide

This guide starts the HOA support agent locally, tests it in the browser, and optionally
connects a Twilio phone number so people can call it.

## What this project does

- Creates or recognizes a caller profile from the caller's phone number.
- Collects and corrects caller name and unit number conversationally.
- Uses a community knowledge base for approved HOA facts such as parking and pool hours.
- Creates maintenance work orders and records call activity in SQLite.
- Provides an operations dashboard at `/operations`.
- Supports browser WebRTC testing and incoming Twilio Media Stream calls.

## Prerequisites

- Python 3.12 or 3.13.
- A NVIDIA API key for the hosted LLM service.
- A Cartesia API key for streaming voice output.
- Optional: a Twilio account and Voice-capable phone number for real calls.
- Optional: [ngrok](https://ngrok.com/) or another public HTTPS/WSS tunnel for local Twilio testing.

## 1. Install the application

Open PowerShell in the repository folder:

```powershell
cd "C:\path\to\Voice Agent"
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -e ".[dev,voice]"
```

If PowerShell blocks virtual-environment activation, run this once in the current terminal:

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
```

## 2. Configure environment variables

Copy the example file and edit the new `.env` file:

```powershell
Copy-Item .env.example .env
```

Minimum browser-voice configuration:

```env
DATABASE_URL=sqlite+aiosqlite:///./hoa_voice_agent.db
NVIDIA_API_KEY=replace-with-your-key
VOICE_STT_PROVIDER=deepgram
DEEPGRAM_API_KEY=replace-with-your-key
DEEPGRAM_STT_MODEL=nova-3
# Use `multi` for Hindi-English callers; set `hi` for a Hindi-only line.
DEEPGRAM_STT_LANGUAGE=multi
CARTESIA_API_KEY=replace-with-your-key
CARTESIA_TTS_MODEL=sonic-3.5
CARTESIA_TTS_ENGLISH_VOICE=9626c31c-bec5-4cca-baa8-f8ba9e84c8bc
# Arushi is a professional Hinglish voice from Cartesia's voice library.
CARTESIA_TTS_HINDI_VOICE=95d51f79-c397-46f9-b49a-23763d3eaa2d
CARTESIA_TTS_SPEED=1.0
CARTESIA_TTS_EMOTION=calm
# Choose the spoken language before starting a voice session. It remains fixed
# throughout the call; a transcript can never change the TTS voice.
VOICE_DEFAULT_LANGUAGE=en
VOICE_TURN_STOP_SECS=0.45
VOICE_LLM_MAX_TOKENS=32
```

Deepgram provides the real-time speech-to-text service, so voice recognition does not use your
local GPU, CUDA, or a downloaded Whisper model. Cartesia provides the streaming speech output;
the LLM still requires `NVIDIA_API_KEY` unless you configure a different LLM provider.

To start a Hindi call, set `VOICE_DEFAULT_LANGUAGE=hi` and restart the voice runtime before
the caller connects. To return to English, set it back to `en` before the next session. The
language is intentionally fixed for a live call to keep voice output stable.

Never commit `.env`. It contains API and Twilio credentials.

## 3. Run the API and operations dashboard

In one PowerShell terminal:

```powershell
.\.venv\Scripts\Activate.ps1
uvicorn hoa_voice_agent.api:app --reload --port 8000
```

Open these URLs:

- API health and interactive docs: `http://localhost:8000/docs`
- Operations dashboard: `http://localhost:8000/operations`

The SQLite database is created at `hoa_voice_agent.db` by default. It contains caller
profiles, conversations, transcript turns, work orders, workflow events, profile history,
and approved community knowledge.

## 4. Test the voice agent in a browser

In a second terminal:

```powershell
.\.venv\Scripts\Activate.ps1
python -m hoa_voice_agent.voice.bot -t webrtc
```

Open `http://localhost:7860/client/`, allow microphone access, then click **Connect**.

The Playground is a development test page. If it becomes stuck after Disconnect, refresh the
page before making another browser test session. Real phone calls do not use this page.

## 5. Configure Twilio SMS (optional)

To send real confirmation texts after a work order:

```env
SMS_MODE=twilio
TWILIO_ACCOUNT_SID=ACxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
TWILIO_AUTH_TOKEN=replace-with-your-auth-token
TWILIO_FROM_PHONE=+1xxxxxxxxxx
```

US application-to-person SMS requires A2P 10DLC registration in Twilio for production use.
Until registration is complete, SMS delivery may be restricted.

## 6. Configure incoming Twilio phone calls

### Start the Twilio phone runtime

The browser and phone runtime cannot share port `7860` at the same time. Stop the browser
runtime first, then run:

```powershell
.\.venv\Scripts\Activate.ps1
python -m hoa_voice_agent.voice.bot -t twilio --host 0.0.0.0
```

### Create a public tunnel for local development

In another terminal:

```powershell
ngrok http 7860
```

Copy the public `https://...` address that ngrok displays. Add its values to `.env`:

```env
TWILIO_VOICE_WEBHOOK_URL=https://your-public-domain/twilio/voice
TWILIO_VOICE_STREAM_URL=wss://your-public-domain/ws
TWILIO_VALIDATE_WEBHOOKS=true
```

Restart the Twilio phone runtime after changing `.env`.

### Configure the Twilio number

In Twilio Console:

1. Go to **Phone Numbers → Manage → Active Numbers**.
2. Select the Voice-capable number.
3. Under **Voice Configuration**, set **A call comes in** to **Webhook**.
4. Set the method to **HTTP POST**.
5. Set the URL to `https://your-public-domain/twilio/voice`.
6. Save the configuration.

When somebody calls the Twilio number, Twilio sends a signed webhook request. The app validates
it, returns TwiML, then accepts Twilio's signed secure WebSocket media stream. The actual caller
number is used as the caller-profile key.

## 7. Verify a phone call

1. Call the Twilio number from a permitted phone.
2. The agent should greet the caller and request name and unit if the number is new.
3. Ask a community question such as “What are the parking rules?”
4. Report an issue such as “There is a water leak in the lobby.”
5. Check `http://localhost:8000/operations` for the caller, transcript, and work order.

## Twilio trial and international-call limitations

Twilio trial accounts can restrict calls to verified numbers and the account's sign-up country.
Some Twilio numbers are labelled **domestic calls only**, which can block callers from another
country. Upgrade the account and confirm the number supports the required inbound calling regions
before presenting it as a public HOA support number.

For international use, review **Voice → Settings → Geo Permissions** in Twilio and enable only
the regions required by the business. A geographic setting does not override a number whose
carrier capability is domestic-only.

## Run tests

```powershell
.\.venv\Scripts\Activate.ps1
pytest
ruff check .
```

## Production checklist

- Replace SQLite with PostgreSQL.
- Host behind a stable HTTPS domain instead of a temporary ngrok address.
- Keep webhook and Media Stream signature validation enabled.
- Add staff authentication before exposing `/operations`.
- Complete A2P 10DLC registration before relying on US SMS confirmations.
- Implement the real Twilio on-call transfer adapter before treating emergency escalation as a
  live handoff.
- Configure an emergency address and accept Twilio's emergency-calling terms if the number will
  place emergency calls.
