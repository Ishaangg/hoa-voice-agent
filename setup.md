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
- Node.js 20 or newer (for the staff Operations Console build).
- A Groq API key for the hosted Qwen LLM service.
- A Fish Audio API key and voice reference ID for the default low-latency voice output.
- Optional: a Cartesia API key if you want the fallback TTS provider.
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
GROQ_API_KEY=replace-with-your-key
GROQ_LLM_BASE_URL=https://api.groq.com/openai/v1
GROQ_LLM_MODEL=qwen/qwen3.6-27b
VOICE_STT_PROVIDER=deepgram_flux
DEEPGRAM_API_KEY=replace-with-your-key
# These Nova settings are retained for the explicit `VOICE_STT_PROVIDER=deepgram` fallback.
DEEPGRAM_FLUX_STT_MODEL=flux-general-multi
DEEPGRAM_FLUX_LANGUAGE_HINTS=["en","hi"]
VOICE_TTS_PROVIDER=fish
FISH_API_KEY=replace-with-your-fish-key
FISH_TTS_MODEL=s2.1-pro-free
FISH_TTS_REFERENCE_ID=replace-with-your-fish-voice-id
FISH_TTS_LATENCY=low
FISH_TTS_CHUNK_LENGTH=100
FISH_TTS_MIN_CHUNK_LENGTH=0
FISH_TTS_CONDITION_ON_PREVIOUS_CHUNKS=false
FISH_TTS_PRECONNECT=true
FISH_TTS_SAMPLE_RATE=24000
FISH_TTS_SPEED=1.08
FISH_TTS_STYLE=warm, conversational
# Choose the complete voice pipeline before starting a voice session.
# Supported values: en, hi, hinglish.
VOICE_DEFAULT_LANGUAGE=hinglish
# Optional outbound room tone; keep it low so it never competes with speech.
VOICE_AMBIENT_ENABLED=true
VOICE_AMBIENT_VOLUME=0.4
# Smart Turn uses audio plus transcript context instead of a fixed silence timeout.
VOICE_USE_SMART_TURN=true
VOICE_SMART_TURN_STOP_SECS=0.55
VOICE_SMART_TURN_CPU_COUNT=1
VOICE_USE_INTERRUPTION_GATE=true
VOICE_INTERRUPT_MIN_WORDS=2
VOICE_VAD_STOP_SECS=0.2
VOICE_TURN_STOP_SECS=0.65
VOICE_INTERIM_TRANSCRIPT_FALLBACK_SECS=0.55
VOICE_LLM_MAX_TOKENS=32
```

Deepgram provides the real-time speech-to-text service, so voice recognition does not use your
local GPU, CUDA, or a downloaded Whisper model. Fish provides the streaming speech output;
the LLM requires `GROQ_API_KEY`; Qwen is the configured model. NVIDIA is only an
optional legacy STT provider when `VOICE_STT_PROVIDER=nvidia`. Cartesia remains available by
setting `VOICE_TTS_PROVIDER=cartesia` and providing its credentials.

To start a Hinglish call, set `VOICE_DEFAULT_LANGUAGE=hinglish` and restart the voice runtime
before the caller connects. Use `hi` for Hindi or `en` for English. The language adapter keeps
the selected mode fixed for the entire live call.

Never commit `.env`. It contains API and Twilio credentials.

## 3. Build and run the staff Operations Console

Install the React application dependencies and create its production bundle once (run this
again whenever console source files change):

```powershell
npm install --prefix web
npm run build --prefix web
```

Then start FastAPI in one PowerShell terminal:

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
approved community knowledge, staff accounts, sessions, routing contacts, vendor routes,
and immutable case-action history.

For the local pilot tenant, sign in at `/operations` with:

- Operations Admin: `ops@evergreen.demo` / `demo-pass`
- Community Manager: `manager@evergreen.demo` / `demo-pass`
- On-Call Responder: `oncall@evergreen.demo` / `demo-pass`

These accounts seed an Evergreen Community Management tenant with Pine Grove Community.
They are demo credentials only; provision real staff identities and
replace SQLite before a production deployment.

## Production persistence and workers

Production uses PostgreSQL and explicit Alembic migrations. Do not allow the API
or voice process to create tables at startup:

```powershell
$env:DATABASE_URL = "postgresql+asyncpg://hoa:password@postgres/hoa_voice_agent"
$env:DATABASE_AUTO_CREATE = "false"
alembic upgrade head
```

Run the notification outbox separately so database commits and WhatsApp/SMS
delivery cannot diverge:

```powershell
$env:OUTBOX_ENABLED = "true"
python -m hoa_voice_agent.voice.outbox_worker
```

Run the Pipecat media worker in another process/container:

```powershell
python -m hoa_voice_agent.voice.production_worker -t twilio --host 0.0.0.0
```

The media worker is deliberately independent from the API and outbox. In a
managed deployment, put multiple media-worker replicas behind the telephony
ingress or use Pipecat Cloud's dispatcher. Set `VOICE_ENABLE_TRACING=true` when
the OpenTelemetry exporter is configured. Per-call metrics are stored in
`voice_metrics` and surfaced through console analytics.

## 4. Test the voice agent in a browser

In a second terminal:

```powershell
.\.venv\Scripts\Activate.ps1
python -m hoa_voice_agent.voice.bot -t webrtc
```

Open `http://localhost:7860/client/`, allow microphone access, then click **Connect**.

The active profile is now Deepgram Flux. To return to the Nova pipeline, set these
values in `.env` before restarting the process:

```env
VOICE_STT_PROVIDER=deepgram
DEEPGRAM_STT_MODEL=nova-3
DEEPGRAM_STT_LANGUAGE=multi
```

Flux supplies its own turn events, so the application automatically bypasses the
Nova-only local VAD and Deepgram finalization processors for this profile.

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

## 6. Configure WhatsApp vendor dispatch (recommended)

Vendor service requests use WhatsApp first and fall back to SMS if WhatsApp delivery fails.
The vendor's registered phone number is verified against the dispatch before a reply can
change a case. Vendors can reply `ACCEPT`, `DECLINE`, `ON MY WAY`, `QUOTE`, or `COMPLETED`.

In Twilio, create or connect a WhatsApp Business sender and set:

```env
SMS_MODE=twilio
VENDOR_NOTIFICATION_CHANNEL=whatsapp
TWILIO_WHATSAPP_FROM=whatsapp:+1xxxxxxxxxx
# Optional: an approved Content Template SID for business-initiated messages.
TWILIO_WHATSAPP_CONTENT_SID=
TWILIO_WHATSAPP_WEBHOOK_URL=https://your-public-domain/api/v1/webhooks/twilio/whatsapp
TWILIO_VALIDATE_WEBHOOKS=true
```

Create an approved WhatsApp Content Template with one body variable (`{{1}}`) and quick
reply buttons such as:

```text
Button label       Button payload
Accept             ACCEPT
Decline            DECLINE
On my way          ON_MY_WAY
```

Paste the resulting Content SID into `TWILIO_WHATSAPP_CONTENT_SID`. The service still accepts
typed `QUOTE` and `COMPLETED` replies; those can become follow-up buttons in a second template
once the vendor accepts the request. Twilio quick replies are returned to the webhook as button
text/payload, which the application maps to the dispatch state.

If `TWILIO_WHATSAPP_CONTENT_SID` is blank, the adapter sends a normal message body. For
production business-initiated WhatsApp messages, configure an approved Twilio/Meta template.
Twilio posts inbound WhatsApp replies to the webhook above. The public URL must be reachable
over HTTPS and must match the URL used for Twilio signature validation.

For local testing, expose the FastAPI API (port 8000), not only the voice runtime:

```powershell
ngrok http 8000
```

Then set the resulting HTTPS URL in `TWILIO_WHATSAPP_WEBHOOK_URL` and restart FastAPI.

WhatsApp actions update vendor-dispatch status, but they do not close a case automatically.
Staff still approve quotes, record the final billed amount, and resolve the case.

## 7. Configure incoming Twilio phone calls

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
- Replace the demo staff accounts with your identity provider or managed staff provisioning.
- Complete A2P 10DLC registration before relying on US SMS confirmations.
- Implement the real Twilio on-call transfer adapter before treating emergency escalation as a
  live handoff.
- Configure an emergency address and accept Twilio's emergency-calling terms if the number will
  place emergency calls.
