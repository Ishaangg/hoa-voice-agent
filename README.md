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

When `GROQ_API_KEY` is configured, Qwen 3.6 27B (served by Groq) is the primary structured router for
ordinary caller turns. It determines whether a caller is reporting a maintenance issue and
extracts typed incident facts. The rule-based extractor remains only as an offline/error
fallback, plus a fast path for explicit life-safety language. The LLM never decides emergency
priority, vendor approval, pricing, or routing authority.

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

The same console now has a separate hotel workspace for the extensible guest-
service vertical. Open <http://localhost:8000/operations?domain=hotel> and sign
in with `hotel.manager@evergreen.demo` / `demo-pass`. Hotel operational records
live in dedicated `hotel_*` tables (guests, rooms, reservations, requests,
departments, knowledge, and sessions); only the low-level transport and
database infrastructure is shared with HOA.
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

The development seed supplies an Evergreen Community Management tenant with one MVP community:

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

The Compose stack runs PostgreSQL migrations as a one-shot `migrate` service,
then starts the API and notification `outbox` processes separately. The Pipecat
`voice` process is behind the `production` profile so local API-only demos do
not fail on missing provider credentials. Start it after configuring `.env`:

```powershell
docker compose --profile production up --build
```

The API and voice worker have `DATABASE_AUTO_CREATE=false`; schema
changes must be represented by Alembic revisions rather than silently changing
production tables. The outbox is the durable boundary for Twilio/WhatsApp
delivery: a case and its notification intent commit together, and the worker
retries provider failures with idempotency keys.

For a manual production rollout:

```powershell
$env:DATABASE_URL = "postgresql+asyncpg://user:password@db.example/hoa_voice_agent"
$env:DATABASE_AUTO_CREATE = "false"
python -m pip install -e ".[dev,voice]"
alembic upgrade head
python -m hoa_voice_agent.voice.outbox_worker
# In another process/container:
python -m hoa_voice_agent.voice.production_worker -t twilio --host 0.0.0.0
```

`production_worker` is the media-worker boundary. Scale it behind the telephony
ingress or use Pipecat Cloud's dispatcher; do not run the development browser
runner and the outbox in the same production process.

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
Flux Multilingual STT and Cartesia Sonic 3.5 TTS. In HOA mode, the configured LLM classifies ordinary
resident turns before the deterministic HOA controller applies safety and authorization rules.
In hotel mode, one hotel interpreter call understands each turn and may select a read-only
PMS tool when live room or request data is needed. Flux owns turn detection in the active
profile; the explicit Nova fallback continues to use the local VAD/turn strategy.

Set `GROQ_API_KEY` in `.env` for the hosted Qwen service, then install and run:

```powershell
python -m pip install -e ".[voice]"
python -m hoa_voice_agent.voice.bot -t webrtc
```

Configure Deepgram Flux as the real-time STT provider:

```env
VOICE_STT_PROVIDER=deepgram_flux
DEEPGRAM_API_KEY=replace-with-your-key
DEEPGRAM_FLUX_STT_MODEL=flux-general-multi
DEEPGRAM_FLUX_LANGUAGE_HINTS=["en","hi"]
```

Deepgram performs streaming STT remotely, so the agent does not use local Whisper or your GPU.
Fish Audio is the active low-latency TTS profile in the demo; Cartesia remains available
as a fallback. Both HOA and hotel workflows use the configured Groq/OpenAI-compatible LLM.
Hotel tool calling is limited to read-only PMS lookups; application code owns all writes and
side effects.

The Flux voice runtime uses Deepgram's model-integrated turn detection for semantic
end-of-turn detection. Flux emits speech start/stop events, while the model decides
whether the caller has actually finished a thought. The local VAD and Smart Turn
settings are used only by the explicit Nova fallback. Flux's final turn event is
passed directly to the hotel/HOA workflow.

### Test Fish Audio S2.1 Pro Free output

Fish Audio can be selected without changing the LLM or STT pipeline. Add your
Fish API key and a voice/model reference ID (the ID from a `fish.audio/m/...`
voice URL), then restart the voice process:

```env
VOICE_TTS_PROVIDER=fish
FISH_API_KEY=replace-with-your-fish-key
FISH_TTS_MODEL=s2.1-pro-free
FISH_TTS_REFERENCE_ID=replace-with-your-fish-voice-id
# Fish's API-level lowest-latency mode. The SDK type is stale, so the adapter
# forwards this request value directly.
FISH_TTS_LATENCY=low
FISH_TTS_CHUNK_LENGTH=100
FISH_TTS_MIN_CHUNK_LENGTH=0
FISH_TTS_CONDITION_ON_PREVIOUS_CHUNKS=false
FISH_TTS_PRECONNECT=true
FISH_TTS_SPEED=1.08
FISH_TTS_STYLE=warm, conversational
```

The adapter uses Fish's realtime WebSocket endpoint with 24 kHz PCM chunks and sends a
`FlushEvent` immediately after each response text chunk. It also opens the WebSocket when
the voice turn begins, overlapping the handshake with response generation. `FISH_TTS_PRECONNECT`
can be disabled if a deployment prefers the simpler on-demand connection. The style cue is
sent only to Fish and never appears in the stored transcript.

Run the provider comparison from the repository root:

```powershell
python scripts/benchmark_fish_tts.py --runs 5
```

This reports application-boundary first-audio p50/p95 for Fish WebSocket and HTTP streaming.
It includes network setup, unlike Fish's published TTFA benchmark, so use it for deployment
decisions. The free `s2.1-pro-free` tier has no latency SLA; use a production tier when a
latency guarantee is required.

Open <http://localhost:7860/client> and allow its microphone prompt. This process starts
its own local WebRTC development runner; it uses the same database and demo data
as the FastAPI API. Both voice modes need `DEEPGRAM_API_KEY`, the configured Groq/Qwen LLM
key, and the API key for the selected TTS provider (`CARTESIA_API_KEY` or
`FISH_API_KEY` plus `FISH_TTS_REFERENCE_ID`).

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

Per-call voice telemetry is written to `voice_metrics` without blocking audio:
turn latency, first-audio latency, STT/LLM/TTS TTFB where applicable, user-turn duration, task
success, fallback, and correction events. The operations analytics endpoint
exposes aggregated latency values for the management company.

The resulting cascade is:

```text
Browser microphone → Pipecat SmallWebRTC → Deepgram Flux Multilingual STT
                   → Flux turn events + hotel/HOA LLM routing
                   → deterministic safety and authorization policy
                   → Cartesia Sonic 3.5 TTS
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

1. Add routing confidence evaluation and a review set of real resident phrasings.
2. Replace simulated messaging and call control with Twilio and LiveKit adapters.
3. Add Redis call locks, idempotency keys, and cached community configuration.
4. Move the browser transport from SmallWebRTC to LiveKit before adding SIP phone calls.
