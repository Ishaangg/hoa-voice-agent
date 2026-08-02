"""Run a browser WebRTC HOA voice session with Pipecat.

Install first: ``python -m pip install -e \".[voice]\"``.
The Pipecat development runner then serves a browser client at
``http://localhost:7860/client``.
"""

from __future__ import annotations

import sys
from collections.abc import Sequence
from contextlib import suppress

from fastapi import HTTPException, Request
from fastapi.responses import Response
from loguru import logger
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.audio.vad.vad_analyzer import VADParams
from pipecat.frames.frames import TTSSpeakFrame
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineParams, PipelineTask
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import (
    LLMContextAggregatorPair,
    LLMUserAggregatorParams,
)
from pipecat.runner.types import (
    RunnerArguments,
    SmallWebRTCRunnerArguments,
    WebSocketRunnerArguments,
)
from pipecat.services.cartesia.tts import CartesiaTTSService, GenerationConfig
from pipecat.services.nvidia.llm import NvidiaLLMService
from pipecat.services.nvidia.stt import NvidiaSTTService
from pipecat.services.tts_service import TextAggregationMode
from pipecat.transports.base_transport import TransportParams
from pipecat.transports.smallwebrtc.transport import SmallWebRTCTransport
from pipecat.turns.user_stop import SpeechTimeoutUserTurnStopStrategy
from pipecat.turns.user_turn_strategies import UserTurnStrategies

from hoa_voice_agent.adapters.fake import (
    COMMUNITY_ID,
    FakeCallControlAdapter,
    FakeCommunityManagementAdapter,
    FakeMessagingAdapter,
)
from hoa_voice_agent.adapters.twilio import TwilioMessagingAdapter
from hoa_voice_agent.persistence import tables as _tables  # noqa: F401
from hoa_voice_agent.persistence.database import Database
from hoa_voice_agent.persistence.seed import seed_demo_community_knowledge
from hoa_voice_agent.services.conversation import ConversationService
from hoa_voice_agent.services.nvidia_incident_extractor import NvidiaIncidentExtractor
from hoa_voice_agent.voice.language import tts_settings_for_language
from hoa_voice_agent.voice.settings import VoiceRuntimeSettings
from hoa_voice_agent.voice.twilio_voice import (
    is_valid_twilio_request,
    is_valid_twilio_websocket_request,
    twiml_for_media_stream,
)
from hoa_voice_agent.voice.workflow_processor import HOAWorkflowProcessor


def create_stt(settings: VoiceRuntimeSettings):
    if settings.voice_stt_provider == "nvidia":
        return NvidiaSTTService(
            api_key=settings.nvidia_api_key,
            server=settings.nvidia_stt_server,
            use_ssl=settings.use_ssl_for(settings.nvidia_stt_server),
            model_function_map={
                "function_id": settings.nvidia_stt_function_id,
                "model_name": settings.nvidia_stt_model,
            },
        )

    try:
        from pipecat.services.deepgram.stt import DeepgramSTTService
    except ImportError as exc:
        raise RuntimeError(
            'Deepgram support is not installed. Run: python -m pip install -e ".[voice]"'
        ) from exc
    return DeepgramSTTService(
        api_key=settings.deepgram_api_key,
        settings=DeepgramSTTService.Settings(
            model=settings.deepgram_stt_model,
            language=settings.deepgram_stt_language,
            interim_results=True,
            numerals=True,
            punctuate=True,
        ),
    )


async def run_bot(
    transport,
    settings: VoiceRuntimeSettings,
    *,
    caller_phone: str,
    greeting_after_rtvi_ready: bool,
) -> None:
    """Build one isolated resident audio session."""

    database = Database(settings.database_url)
    await database.create_schema()
    session = database.session_factory()
    try:
        await seed_demo_community_knowledge(session)
        community_management = FakeCommunityManagementAdapter()
        messaging = (
            TwilioMessagingAdapter(
                account_sid=settings.twilio_account_sid,
                auth_token=settings.twilio_auth_token,
                from_phone=settings.twilio_from_phone,
                whatsapp_from=settings.twilio_whatsapp_from,
                whatsapp_content_sid=settings.twilio_whatsapp_content_sid,
            )
            if settings.sms_mode == "twilio"
            else FakeMessagingAdapter()
        )
        conversation_service = ConversationService(
            session=session,
            community_management=community_management,
            messaging=messaging,
            call_control=FakeCallControlAdapter(),
            vendor_notification_channel=settings.vendor_notification_channel,
            incident_extractor=NvidiaIncidentExtractor(
                api_key=settings.nvidia_api_key,
                base_url=settings.nvidia_llm_base_url,
                model=settings.nvidia_llm_model,
                # Known safety phrases are handled locally. This avoids an LLM
                # round trip before creating an emergency or urgent work order.
                prefer_local_for_known_incidents=True,
            ),
        )
        conversation = await conversation_service.start(
            caller_phone,
            COMMUNITY_ID,
            language=settings.voice_default_language,
        )

        stt = create_stt(settings)
        tts_language_settings = tts_settings_for_language(
            settings.voice_default_language,
            english_voice=settings.cartesia_tts_english_voice,
            hindi_voice=settings.cartesia_tts_hindi_voice,
        )
        tts = CartesiaTTSService(
            api_key=settings.cartesia_api_key,
            settings=CartesiaTTSService.Settings(
                model=settings.cartesia_tts_model,
                voice=tts_language_settings["voice"],
                language=tts_language_settings["language"],
                generation_config=GenerationConfig(
                    speed=settings.cartesia_tts_speed,
                    emotion=settings.cartesia_tts_emotion,
                ),
            ),
            text_aggregation_mode=TextAggregationMode(settings.cartesia_tts_text_mode),
            max_buffer_delay_ms=(
                settings.cartesia_tts_max_buffer_delay_ms
                if settings.cartesia_tts_text_mode == "token"
                else None
            ),
        )
        llm = NvidiaLLMService(
            api_key=settings.nvidia_api_key,
            base_url=settings.nvidia_llm_base_url,
            settings=NvidiaLLMService.Settings(
                model=settings.nvidia_llm_model,
                temperature=0.2,
                max_tokens=settings.voice_llm_max_tokens,
                # The OpenAI SDK forwards provider-specific NVIDIA fields only through
                # ``extra_body``; passing this at the top level aborts the stream.
                extra={"extra_body": {"chat_template_kwargs": {"enable_thinking": False}}},
            ),
        )

        context = LLMContext()
        aggregators = LLMContextAggregatorPair(
            context,
            user_params=LLMUserAggregatorParams(
                # Use VAD plus a short speech timeout to commit a transcript reliably.
                # Smart Turn can hold a browser session open indefinitely when its local
                # analyzer fails to receive a clean end-of-speech signal.
                vad_analyzer=SileroVADAnalyzer(
                    params=VADParams(stop_secs=settings.voice_vad_stop_secs)
                ),
                user_turn_strategies=UserTurnStrategies(
                    stop=[
                        SpeechTimeoutUserTurnStopStrategy(
                            user_speech_timeout=settings.voice_turn_stop_secs
                        )
                    ]
                ),
            ),
        )
        workflow = HOAWorkflowProcessor(
            conversation_service,
            conversation.conversation_id,
            settings.voice_default_language,
        )
        pipeline = Pipeline(
            [
                transport.input(),
                stt,
                aggregators.user(),
                workflow,
                llm,
                tts,
                transport.output(),
                aggregators.assistant(),
            ]
        )
        task = PipelineTask(
            pipeline,
            params=PipelineParams(
                audio_in_sample_rate=16000,
                audio_out_sample_rate=24000,
                enable_metrics=True,
            ),
        )

        greeting_queued = False

        async def queue_greeting() -> None:
            nonlocal greeting_queued
            if greeting_queued:
                return
            greeting_queued = True
            logger.info(
                "Voice session {} is ready; sending greeting for conversation {}.",
                task.id,
                conversation.conversation_id,
            )
            await task.queue_frames([TTSSpeakFrame(conversation.say)])

        @transport.event_handler("on_client_connected")
        async def on_client_connected(_transport, _connection) -> None:
            if greeting_after_rtvi_ready:
                logger.info("WebRTC client connected; waiting for RTVI client-ready handshake.")
                return
            logger.info("Twilio caller connected; starting voice session {}.", task.id)
            await queue_greeting()

        if greeting_after_rtvi_ready:

            @task.rtvi.event_handler("on_client_ready")
            async def on_client_ready(_rtvi) -> None:
                # The connection event can arrive before pipeline event handlers are fully
                # registered. RTVI client-ready occurs only after the browser can receive
                # bot output, so it is the reliable place to start the spoken session.
                await queue_greeting()

        @transport.event_handler("on_client_disconnected")
        async def on_client_disconnected(_transport, _connection) -> None:
            logger.info("WebRTC client disconnected; cancelling voice session {}.", task.id)
            await task.cancel()

        await PipelineRunner(handle_sigint=False).run(task)
    finally:
        await session.close()
        await database.dispose()


async def bot(runner_args: RunnerArguments) -> None:
    """Pipecat runner entry point for browser and Twilio voice sessions."""

    settings = VoiceRuntimeSettings()
    if isinstance(runner_args, SmallWebRTCRunnerArguments):
        transport = SmallWebRTCTransport(
            webrtc_connection=runner_args.webrtc_connection,
            params=TransportParams(audio_in_enabled=True, audio_out_enabled=True),
        )
        await run_bot(
            transport,
            settings,
            caller_phone=settings.voice_demo_caller_phone,
            greeting_after_rtvi_ready=True,
        )
        return

    if isinstance(runner_args, WebSocketRunnerArguments):
        from pipecat.runner.utils import create_transport
        from pipecat.transports.websocket.fastapi import FastAPIWebsocketParams

        transport = await create_transport(
            runner_args,
            {
                "twilio": lambda: FastAPIWebsocketParams(
                    audio_in_enabled=True,
                    audio_out_enabled=True,
                )
            },
        )
        if runner_args.transport_type != "twilio":
            raise RuntimeError("This HOA phone bot only accepts Twilio Media Streams.")
        caller_phone = runner_args.call_data.from_number if runner_args.call_data else None
        if not caller_phone:
            raise RuntimeError("Twilio Media Stream did not include the caller phone number.")
        await run_bot(
            transport,
            settings,
            caller_phone=caller_phone,
            greeting_after_rtvi_ready=False,
        )
        return

    raise RuntimeError("This HOA voice bot supports SmallWebRTC and Twilio Media Streams only.")


def configure_twilio_voice_webhook() -> None:
    """Register the signed Twilio webhook before Pipecat starts its FastAPI app."""

    from pipecat.runner.run import app

    class TwilioMediaStreamSignatureMiddleware:
        """Reject unsigned traffic before Pipecat accepts the phone WebSocket."""

        def __init__(self, next_app) -> None:
            self._next_app = next_app

        async def __call__(self, scope, receive, send) -> None:
            if scope["type"] == "websocket" and scope["path"] == "/ws":
                settings = VoiceRuntimeSettings()
                if (
                    settings.twilio_validate_webhooks
                    and settings.twilio_voice_stream_url
                    and not is_valid_twilio_websocket_request(
                        auth_token=settings.twilio_auth_token or "",
                        stream_url=settings.twilio_voice_stream_url,
                        signature=dict(scope["headers"]).get(b"x-twilio-signature", b"").decode(),
                    )
                ):
                    await send({"type": "websocket.close", "code": 4403})
                    return
            await self._next_app(scope, receive, send)

    app.add_middleware(TwilioMediaStreamSignatureMiddleware)

    @app.post("/twilio/voice")
    async def incoming_twilio_call(request: Request) -> Response:
        settings = VoiceRuntimeSettings()
        if not settings.twilio_voice_webhook_url or not settings.twilio_voice_stream_url:
            raise HTTPException(status_code=503, detail="Twilio Voice is not configured.")

        raw_body = (await request.body()).decode("utf-8")
        from urllib.parse import parse_qs

        form_values = {key: values[-1] for key, values in parse_qs(raw_body).items()}
        if settings.twilio_validate_webhooks and not is_valid_twilio_request(
            auth_token=settings.twilio_auth_token or "",
            url=settings.twilio_voice_webhook_url,
            form_values=form_values,
            signature=request.headers.get("X-Twilio-Signature"),
        ):
            raise HTTPException(status_code=403, detail="Invalid Twilio webhook signature.")

        return Response(
            content=twiml_for_media_stream(
                stream_url=settings.twilio_voice_stream_url,
                from_number=form_values.get("From", ""),
                to_number=form_values.get("To", ""),
            ),
            media_type="application/xml",
        )


def is_twilio_runner(arguments: Sequence[str]) -> bool:
    """Return whether this runner process is serving Twilio Media Streams.

    SmallWebRTC and Twilio both use a WebSocket path named ``/ws``. Mounting
    Twilio's signature middleware for a browser runner would therefore reject
    every browser WebSocket as an unsigned Twilio request.
    """

    for index, argument in enumerate(arguments):
        if argument in {"-t", "--transport"} and index + 1 < len(arguments):
            return arguments[index + 1] == "twilio"
        if argument.startswith("--transport="):
            return argument.removeprefix("--transport=") == "twilio"
    return False


def main() -> None:
    try:
        from pipecat.runner.run import main as runner_main
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            "The Pipecat runner is missing. Install the optional voice runtime with "
            '`python -m pip install -e ".[voice]"`.'
        ) from exc
    # Validate before the browser can connect. This prevents a configuration error
    # from appearing as an apparently successful but silent WebRTC session.
    VoiceRuntimeSettings()
    if is_twilio_runner(sys.argv[1:]):
        configure_twilio_voice_webhook()
    runner_main()


if __name__ == "__main__":
    with suppress(KeyboardInterrupt):
        main()
