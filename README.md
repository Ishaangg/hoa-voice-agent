# HOA Voice Agent

Phase 1 foundation for a cascaded, streaming HOA resident-support agent.

This first vertical slice deliberately separates the safety-critical workflow from the
future voice and LLM layers. It supports:

- one fake community and resident;
- FAQ answers;
- maintenance intake;
- typed, deterministic emergency decisions;
- persistent internal work orders;
- simulated SMS confirmations;
- simulated transfers to an on-call manager; and
- a browser speech simulator for workflow testing.

When `NVIDIA_API_KEY` is configured, Nemotron Nano extracts typed incident facts from each
maintenance transcript. The rule-based extractor remains only as an offline/error fallback.
The LLM produces facts, but it never decides emergency priority or routing.

## Run locally

Python 3.12 or 3.13 is supported.

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
uvicorn hoa_voice_agent.api:app --reload
```

Open:

- Browser simulator: <http://localhost:8000/demo>
- Operations dashboard: <http://localhost:8000/operations>
- OpenAPI: <http://localhost:8000/docs>
- Health: <http://localhost:8000/health>

The agent has no preloaded resident directory. Every incoming phone number creates a local,
unverified caller profile. The agent asks for the caller's name and unit once, persists those
claimed details, and uses them to resume later calls. The API uses a local SQLite database
unless `DATABASE_URL` is set. This is operational context, not identity verification; require
an SMS one-time code before exposing account-specific data.

At startup, the demo seeds approved Pine Grove facts into the `community_knowledge` database
table. These facts cover office, amenities, parking, emergency, and routing topics. The agent
uses only active facts from that community when answering an HOA question. The staff console
now provides community-scoped knowledge management; only items with approval state `approved`
become active for the agent.

## Staff Operations Console

The React console at `/operations` is the management-company surface: it prioritizes emergency
and unassigned cases, shows call transcripts and caller context, supports acknowledge/assign/
resolve actions with an immutable audit trail, and manages approved community knowledge and
routing contacts. It uses a signed, HTTP-only pilot session cookie and company/community-scoped
roles.

Build it before starting FastAPI:

```powershell
npm install --prefix web
npm run build --prefix web
```

The development seed supplies an Evergreen Community Management tenant with two communities:

- `ops@evergreen.demo` / `demo-pass` — Operations Admin
- `manager@evergreen.demo` / `demo-pass` — Pine Grove Community Manager
- `oncall@evergreen.demo` / `demo-pass` — Pine Grove On-Call Responder

Those credentials are local-pilot data only. Replace them with real provisioning and PostgreSQL
before exposing the console to staff.

## Run with PostgreSQL and Redis

```powershell
docker compose up --build
```

This starts:

- API: `localhost:8000`
- PostgreSQL: `localhost:5432`
- Redis: `localhost:6379`

Redis is provisioned as the low-latency state/cache boundary but is not yet used by the
Phase 1 conversation service.

## Exercise the API

Start a conversation:

```powershell
$call = Invoke-RestMethod `
  -Method Post `
  -Uri http://localhost:8000/api/v1/conversations `
  -ContentType application/json `
  -Body '{"caller_phone":"+15550101111"}'
```

Report active flooding:

```powershell
Invoke-RestMethod `
  -Method Post `
  -Uri "http://localhost:8000/api/v1/conversations/$($call.conversation_id)/turns" `
  -ContentType application/json `
  -Body '{"text":"Water is leaking from the ceiling in Building A hallway."}'
```

## Test

```powershell
pytest
ruff check .
```

## Run browser WebRTC voice

The workflow demo uses a temporary browser speech-recognition API. The real audio path is
the separate Pipecat process below. It streams browser microphone audio through Deepgram
Nova-3 STT and Cartesia Sonic 3.5 TTS, while keeping the deterministic HOA controller in the middle.
It uses VAD plus a short silence timeout to finish each resident turn reliably.

Set `NVIDIA_API_KEY` in `.env` for NVIDIA's hosted services, then install and run:

```powershell
python -m pip install -e ".[voice]"
python -m hoa_voice_agent.voice.bot -t webrtc
```

Configure Deepgram as the real-time STT provider:

```env
VOICE_STT_PROVIDER=deepgram
DEEPGRAM_API_KEY=replace-with-your-key
DEEPGRAM_STT_MODEL=nova-3
# `multi` supports Hindi-English code switching; use `hi` for Hindi-only calls.
DEEPGRAM_STT_LANGUAGE=multi
```

Deepgram performs streaming STT remotely, so the agent does not use local Whisper or your GPU.
Cartesia provides the streaming TTS output; the NVIDIA LLM remains hosted.

Open <http://localhost:7860/client> and allow its microphone prompt. This process starts
its own local WebRTC development runner; it uses the same database and Fake Community data
as the FastAPI API. The voice path needs `DEEPGRAM_API_KEY`, `CARTESIA_API_KEY`, and the
configured NVIDIA LLM key.

## Receive real calls through a Twilio number

The phone agent uses Twilio's bidirectional Media Streams. The signed inbound-call webhook
returns TwiML, which opens a secure WebSocket stream to the same Pipecat pipeline used by the
browser demo. The caller's actual `From` number is passed into the conversation, so a returning
resident is identified from their own caller profile and a new number creates an unverified one.

For a local demo, create a public HTTPS/WSS tunnel to the Pipecat runner first. For example, if
the runner listens on `7860`, an ngrok tunnel gives you a public address such as
`https://example.ngrok.app`:

```env
TWILIO_ACCOUNT_SID=ACxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
TWILIO_AUTH_TOKEN=your-auth-token
TWILIO_FROM_PHONE=+1xxxxxxxxxx
TWILIO_VOICE_WEBHOOK_URL=https://example.ngrok.app/twilio/voice
TWILIO_VOICE_STREAM_URL=wss://example.ngrok.app/ws
TWILIO_VALIDATE_WEBHOOKS=true
```

Start the phone runtime with the Twilio transport:

```powershell
python -m hoa_voice_agent.voice.bot -t twilio
```

Then, in the Twilio Console, open the purchased number's **Voice Configuration** and set **A call
comes in** to `POST https://example.ngrok.app/twilio/voice`. Call that number from a different
phone. Both the inbound webhook and Media Stream WebSocket handshake signatures are checked before
the call reaches the agent; do not turn signature checking off outside an isolated test.

Twilio requires the media endpoint to be publicly reachable as `wss://` over port 443. The
Pipecat serializer converts the 8 kHz mu-law call stream to and from the pipeline audio format.
The current emergency transfer adapter remains simulated, so do not treat it as a live on-call
handoff until we implement the Twilio transfer adapter.

The resulting cascade is:

```text
Browser microphone → Pipecat SmallWebRTC → Deepgram Nova-3 streaming STT
                   → Smart Turn + Nemotron Nano fact extraction
                   → deterministic HOA policy → Cartesia Sonic 3.5 TTS
                   → Browser speaker
```

## Architecture boundary

```text
Browser simulator / future LiveKit transport
                    |
             conversation service
              /             \
 temporary fact extractor   FAQ lookup
              |
 deterministic emergency policy
              |
 work-order + SMS + transfer adapters
              |
PostgreSQL / future Buildium, Twilio, LiveKit
```

## Connect real operations systems

The API defaults to safe fake adapters. To enable real side effects, point it at an
operations-integration service that authenticates the agent and maps these events to your
property-management, SMS, and telephony providers:

```env
OPERATIONS_MODE=webhook
OPERATIONS_WEBHOOK_BASE_URL=https://your-operations-service.example
OPERATIONS_WEBHOOK_BEARER_TOKEN=replace-with-a-secret
```

The service must accept authenticated JSON `POST` requests and return
`{"reference_id":"..."}` for each endpoint:

- `/v1/work-orders` — work-order ID, conversation/community/resident IDs, incident type,
  priority, and description;
- `/v1/messages/sms` — recipient phone number and confirmation message;
- `/v1/on-call-transfers` — conversation ID and the on-call phone number.

Every request includes an `Idempotency-Key` header so the operations service can safely
reject duplicates if the agent retries. This first integration slice keeps community and
resident lookup on the demo directory; the next provider-specific adapter will replace
that read path once the property-management platform is chosen.

## Send real SMS with Twilio

The agent can send the existing work-order confirmation through Twilio's Messages API.
Keep secrets in `.env` only, then set:

```env
SMS_MODE=twilio
TWILIO_ACCOUNT_SID=ACxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
TWILIO_AUTH_TOKEN=your-auth-token
TWILIO_FROM_PHONE=+1xxxxxxxxxx
```

With `SMS_MODE=fake` (the default), no real message is sent. The API and Pipecat voice bot
both use Twilio when this mode is enabled. A successful Twilio request is recorded as a
`twilio:SM...` workflow action reference.

## Next implementation slice

1. Replace rule-based fact extraction with strict structured output from the selected LLM.
2. Replace simulated messaging and call control with Twilio and LiveKit adapters.
3. Add Redis call locks, idempotency keys, and cached community configuration.
4. Move the browser transport from SmallWebRTC to LiveKit before adding SIP phone calls.
