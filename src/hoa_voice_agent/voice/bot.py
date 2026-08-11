"""Run a browser WebRTC HOA voice session with Pipecat.

Install first: ``python -m pip install -e \".[voice]\"``.
The Pipecat development runner then serves a browser client at
``http://localhost:7860/client``.
"""

# ruff: noqa: E501

from __future__ import annotations

import sys
from collections.abc import Sequence
from contextlib import suppress
from uuid import UUID

from fastapi import HTTPException, Request
from fastapi.responses import Response
from loguru import logger
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.audio.vad.vad_analyzer import VADParams
from pipecat.frames.frames import TTSSpeakFrame
from pipecat.observers.user_bot_latency_observer import UserBotLatencyObserver
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
from pipecat.services.nvidia.stt import NvidiaSTTService
from pipecat.services.openai.llm import OpenAILLMService
from pipecat.services.tts_service import TextAggregationMode
from pipecat.transcriptions.language import Language
from pipecat.transports.base_transport import TransportParams
from pipecat.transports.smallwebrtc.transport import SmallWebRTCTransport
from pipecat.turns.user_stop import (
    SpeechTimeoutUserTurnStopStrategy,
    TurnAnalyzerUserTurnStopStrategy,
)
from pipecat.turns.user_turn_strategies import (
    ExternalUserTurnStrategies,
    UserTurnStrategies,
)

from hoa_voice_agent.adapters.fake import (
    COMMUNITY_ID,
    FakeCallControlAdapter,
    FakeCommunityManagementAdapter,
    FakeMessagingAdapter,
)
from hoa_voice_agent.adapters.twilio import TwilioMessagingAdapter
from hoa_voice_agent.persistence import hotel_tables as _hotel_tables  # noqa: F401
from hoa_voice_agent.persistence import tables as _tables  # noqa: F401
from hoa_voice_agent.persistence.database import Database
from hoa_voice_agent.persistence.seed import seed_demo_community_knowledge, seed_demo_hotel
from hoa_voice_agent.services.conversation import ConversationService
from hoa_voice_agent.services.hotel_audit import HotelConversationAuditLog
from hoa_voice_agent.services.hotel_conversation import HotelConversationService
from hoa_voice_agent.services.hotel_turn_interpreter import OpenAIHotelTurnInterpreter
from hoa_voice_agent.services.openai_incident_extractor import OpenAIIncidentExtractor
from hoa_voice_agent.services.outbox import OutboxNotificationQueue
from hoa_voice_agent.services.telemetry import CallTelemetry
from hoa_voice_agent.voice.ambient_audio import AmbientNoiseMixer
from hoa_voice_agent.voice.deepgram_finalization import DeepgramFinalizationProcessor
from hoa_voice_agent.voice.fish_tts import FishAudioTTSService, FishAudioWarmupProcessor
from hoa_voice_agent.voice.hotel_workflow_processor import HotelWorkflowProcessor
from hoa_voice_agent.voice.interim_transcript_recovery import (
    InterimTranscriptRecoveryProcessor,
)
from hoa_voice_agent.voice.interruption_gate import GuardedInterruptionUserTurnStartStrategy
from hoa_voice_agent.voice.language import LanguageAdapter, language_adapter
from hoa_voice_agent.voice.response_gate import VoiceOutputGateProcessor
from hoa_voice_agent.voice.settings import VoiceRuntimeSettings
from hoa_voice_agent.voice.twilio_voice import (
    is_valid_twilio_request,
    is_valid_twilio_websocket_request,
    parse_twilio_form,
    twiml_for_media_stream,
)
from hoa_voice_agent.voice.workflow_processor import HOAWorkflowProcessor


def create_stt(
    settings: VoiceRuntimeSettings,
    language: LanguageAdapter | None = None,
):
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

    if settings.voice_stt_provider == "deepgram_flux":
        try:
            # Pipecat 1.6 exposes Flux from the nested ``flux.stt`` module.
            from pipecat.services.deepgram.flux.stt import DeepgramFluxSTTService
        except ImportError as exc:
            raise RuntimeError(
                "Deepgram Flux support is not installed in this Pipecat environment."
            ) from exc

        try:
            language_hints = [Language(code) for code in settings.deepgram_flux_language_hints]
        except ValueError as exc:
            raise ValueError(
                "DEEPGRAM_FLUX_LANGUAGE_HINTS must contain valid language codes."
            ) from exc

        return DeepgramFluxSTTService(
            api_key=settings.deepgram_api_key,
            settings=DeepgramFluxSTTService.Settings(
                model=settings.deepgram_flux_stt_model,
                language_hints=language_hints,
                eot_threshold=settings.deepgram_flux_eot_threshold,
                eager_eot_threshold=settings.deepgram_flux_eager_eot_threshold,
                eot_timeout_ms=settings.deepgram_flux_eot_timeout_ms,
                keyterm=settings.deepgram_stt_keyterms,
            ),
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
            language=(
                language.stt_language
                if language is not None
                else (settings.deepgram_stt_language or "multi")
            ),
            interim_results=True,
            endpointing=settings.deepgram_stt_endpointing_ms,
            keyterm=settings.deepgram_stt_keyterms,
            numerals=True,
            punctuate=True,
        ),
        ttfs_p99_latency=settings.deepgram_stt_ttfs_p99_latency,
    )


def create_turn_strategies(settings: VoiceRuntimeSettings) -> UserTurnStrategies:
    """Create end-of-turn detection with a conservative fallback.

    VAD detects that speech stopped; it cannot reliably tell whether the caller
    finished a thought. Smart Turn uses the audio context around the pause to
    avoid submitting fragments such as ``"my room..."`` to the workflow. The
    fixed timeout remains available for environments where the bundled ONNX
    analyzer cannot initialize.
    """

    if settings.voice_stt_provider == "deepgram_flux":
        # Flux emits user-started/user-stopped frames from its model-integrated
        # turn detector. Local VAD or a timeout would create a second,
        # competing definition of the caller's turn.
        return ExternalUserTurnStrategies()

    if settings.voice_use_smart_turn:
        try:
            from pipecat.audio.turn.smart_turn.base_smart_turn import SmartTurnParams
            from pipecat.audio.turn.smart_turn.local_smart_turn_v3 import (
                LocalSmartTurnAnalyzerV3,
            )

            analyzer = LocalSmartTurnAnalyzerV3(
                cpu_count=settings.voice_smart_turn_cpu_count,
                params=SmartTurnParams(stop_secs=settings.voice_smart_turn_stop_secs),
            )
            logger.info(
                "Using LocalSmartTurnAnalyzerV3 for end-of-turn detection "
                "(stop_secs={}, cpu_count={}).",
                settings.voice_smart_turn_stop_secs,
                settings.voice_smart_turn_cpu_count,
            )
            return UserTurnStrategies(
                start=(
                    [
                        GuardedInterruptionUserTurnStartStrategy(
                            min_words_while_bot_speaking=settings.voice_interrupt_min_words
                        )
                    ]
                    if settings.voice_use_interruption_gate
                    else None
                ),
                stop=[TurnAnalyzerUserTurnStopStrategy(turn_analyzer=analyzer)]
            )
        except Exception as exc:  # pragma: no cover - depends on local ONNX runtime
            logger.warning(
                "Smart Turn could not initialize; falling back to speech timeout: {}",
                exc,
            )

    return UserTurnStrategies(
        start=(
            [
                GuardedInterruptionUserTurnStartStrategy(
                    min_words_while_bot_speaking=settings.voice_interrupt_min_words
                )
            ]
            if settings.voice_use_interruption_gate
            else None
        ),
        stop=[
            SpeechTimeoutUserTurnStopStrategy(
                user_speech_timeout=settings.voice_turn_stop_secs
            )
        ]
    )


def create_audio_out_mixer(settings: VoiceRuntimeSettings) -> AmbientNoiseMixer | None:
    """Create the optional ambient bed for one isolated outbound session."""

    if not settings.voice_ambient_enabled:
        return None
    return AmbientNoiseMixer(volume=settings.voice_ambient_volume)


async def run_bot(
    transport,
    settings: VoiceRuntimeSettings,
    *,
    caller_phone: str,
    greeting_after_rtvi_ready: bool,
) -> None:
    """Build one isolated resident audio session."""

    database = Database(
        settings.database_url,
        auto_create_schema=settings.database_auto_create,
    )
    await database.create_schema()
    session = database.session_factory()
    telemetry: CallTelemetry | None = None
    tts = None
    try:
        await seed_demo_community_knowledge(session)
        if settings.voice_domain == "hotel":
            await seed_demo_hotel(session)
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
        conversation_service = None
        hotel_service = None
        selected_language = (
            settings.hotel_response_language
            if settings.voice_domain == "hotel" and settings.hotel_response_language
            else settings.voice_default_language
        )
        session_language = language_adapter(
            selected_language,
            english_voice=settings.cartesia_tts_english_voice,
            hindi_voice=settings.cartesia_tts_hindi_voice,
            voice_override=settings.cartesia_tts_voice,
            language_override=settings.cartesia_tts_language,
        )
        if settings.voice_domain == "hotel":
            hotel_service = HotelConversationService(
                session=session,
                hotel_id=UUID(settings.hotel_demo_hotel_id),
                messaging=messaging,
                notification_channel=settings.vendor_notification_channel,
                interpreter=OpenAIHotelTurnInterpreter(
                    api_key=settings.resolved_hotel_llm_api_key,
                    base_url=settings.hotel_llm_base_url,
                    model=settings.hotel_llm_model,
                    reasoning_effort=settings.hotel_llm_reasoning_effort,
                ),
                notification_queue=(
                    OutboxNotificationQueue(session) if settings.outbox_enabled else None
                ),
                audit_log=HotelConversationAuditLog(settings.hotel_audit_log_path),
                language_adapter=session_language,
            )
            conversation_id, greeting = await hotel_service.start(caller_phone)
        else:
            conversation_service = ConversationService(
                session=session,
                community_management=community_management,
                messaging=messaging,
                call_control=FakeCallControlAdapter(),
                vendor_notification_channel=settings.vendor_notification_channel,
                incident_extractor=OpenAIIncidentExtractor(
                    api_key=settings.groq_api_key,
                    base_url=settings.groq_llm_base_url,
                    model=settings.groq_llm_model,
                    # The structured LLM router classifies normal resident turns.
                    # ConversationService still keeps an immediate local path for
                    # explicit life-safety language and provider failures.
                    prefer_local_for_known_incidents=False,
                ),
                notification_queue=(
                    OutboxNotificationQueue(session) if settings.outbox_enabled else None
                ),
            )
            conversation = await conversation_service.start(
                caller_phone,
                COMMUNITY_ID,
                language=settings.voice_default_language,
            )
            conversation_id, greeting = conversation.conversation_id, conversation.say

        telemetry = CallTelemetry(
            database,
            call_id=str(conversation_id),
            conversation_id=conversation_id,
            domain=settings.voice_domain,
        )
        telemetry.record("call_started")
        if hotel_service:
            # The catalog module records cache and recommendation timings
            # without coupling service construction to the voice call id.
            hotel_service.telemetry = telemetry
            hotel_service.booking_coordinator.telemetry = telemetry

        stt = create_stt(settings, session_language)
        if settings.voice_tts_provider == "fish":
            tts = FishAudioTTSService(
                api_key=settings.fish_api_key,
                model=settings.fish_tts_model,
                reference_id=settings.fish_tts_reference_id,
                sample_rate=settings.fish_tts_sample_rate,
                latency=settings.fish_tts_latency,
                chunk_length=settings.fish_tts_chunk_length,
                min_chunk_length=settings.fish_tts_min_chunk_length,
                condition_on_previous_chunks=settings.fish_tts_condition_on_previous_chunks,
                preconnect=settings.fish_tts_preconnect,
                speed=settings.fish_tts_speed,
                temperature=settings.fish_tts_temperature,
                top_p=settings.fish_tts_top_p,
                style=settings.fish_tts_style,
                text_aggregation_mode=TextAggregationMode(settings.fish_tts_text_mode),
            )
        else:
            tts_language_settings = session_language.tts_settings
            tts = CartesiaTTSService(
                api_key=settings.cartesia_api_key,
                settings=CartesiaTTSService.Settings(
                    model=settings.cartesia_tts_model,
                    voice=tts_language_settings.voice,
                    language=tts_language_settings.language,
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
        # Hotel workflow responses already include the hotel interpreter's
        # semantic call and any read-only PMS tool round trip. Do not place a
        # second general LLM after the workflow. HOA keeps its existing
        # streaming LLM path for backwards compatibility.
        llm = None
        if not hotel_service:
            llm = OpenAILLMService(
                api_key=settings.groq_api_key,
                base_url=settings.groq_llm_base_url,
                settings=OpenAILLMService.Settings(
                    model=settings.groq_llm_model,
                    temperature=0.2,
                    max_tokens=settings.voice_llm_max_tokens,
                    # Luna supports an explicit non-thinking mode through the
                    # OpenAI-compatible reasoning_effort field.
                    extra={
                        "reasoning_effort": "none",
                        "extra_body": {"reasoning_format": "hidden"},
                    },
                ),
            )

        context = LLMContext()
        uses_flux_turns = settings.voice_stt_provider == "deepgram_flux"
        aggregators = LLMContextAggregatorPair(
            context,
            user_params=LLMUserAggregatorParams(
                # Flux emits its own turn events. Nova uses local VAD plus the
                # configured turn strategy to decide when to submit a turn.
                vad_analyzer=(
                    None
                    if uses_flux_turns
                    else SileroVADAnalyzer(
                        params=VADParams(
                            confidence=settings.voice_vad_confidence,
                            start_secs=settings.voice_vad_start_secs,
                            stop_secs=settings.voice_vad_stop_secs,
                            min_volume=settings.voice_vad_min_volume,
                        )
                    )
                ),
                user_turn_strategies=create_turn_strategies(settings),
            ),
        )
        if hotel_service:
            workflow = HotelWorkflowProcessor(
                hotel_service,
                conversation_id,
                session_language.mode,
                telemetry=telemetry,
                language_adapter=session_language,
            )
        else:
            workflow = HOAWorkflowProcessor(
                conversation_service,
                conversation_id,
                session_language.mode,
                telemetry=telemetry,
            )
        deepgram_finalization = (
            None
            if uses_flux_turns
            else DeepgramFinalizationProcessor(telemetry=telemetry)
        )
        transcript_recovery = (
            None
            if uses_flux_turns
            else InterimTranscriptRecoveryProcessor(
                fallback_delay_secs=settings.voice_interim_transcript_fallback_secs
            )
        )
        pipeline_frames = [
            transport.input(),
            stt,
            aggregators.user(),
        ]
        if deepgram_finalization is not None:
            pipeline_frames.insert(2, deepgram_finalization)
        if transcript_recovery is not None:
            pipeline_frames.insert(3, transcript_recovery)
        if isinstance(tts, FishAudioTTSService):
            # Start Fish's WebSocket as soon as the caller begins speaking so
            # its handshake overlaps the caller's utterance and LLM work.
            pipeline_frames.append(FishAudioWarmupProcessor(tts))
        pipeline_frames.append(workflow)
        if llm is not None:
            pipeline_frames.append(llm)
        pipeline_frames.extend(
            [
                VoiceOutputGateProcessor(),
                tts,
                transport.output(),
                aggregators.assistant(),
            ]
        )
        pipeline = Pipeline(pipeline_frames)
        latency_observer = UserBotLatencyObserver()

        @latency_observer.event_handler("on_latency_measured")
        async def on_latency_measured(_observer, latency_seconds: float) -> None:
            telemetry.record("turn_latency", latency_seconds * 1000)

        @latency_observer.event_handler("on_first_bot_speech_latency")
        async def on_first_bot_speech_latency(_observer, latency_seconds: float) -> None:
            telemetry.record("first_audio_latency", latency_seconds * 1000)

        @latency_observer.event_handler("on_latency_breakdown")
        async def on_latency_breakdown(_observer, breakdown) -> None:
            if breakdown.user_turn_secs is not None:
                telemetry.record("user_turn", breakdown.user_turn_secs * 1000)
            if breakdown.text_aggregation:
                telemetry.record(
                    "text_aggregation",
                    breakdown.text_aggregation.duration_secs * 1000,
                )
            for item in breakdown.ttfb:
                processor = item.processor.lower()
                metric_name = "service_ttfb"
                if "stt" in processor:
                    metric_name = "stt_ttfb"
                elif "llm" in processor or "openai" in processor or "nvidia" in processor:
                    metric_name = "llm_ttfb"
                elif "tts" in processor or "cartesia" in processor or "fish" in processor:
                    metric_name = "tts_ttfb"
                telemetry.record(
                    metric_name,
                    item.duration_secs * 1000,
                    metadata={"processor": item.processor, "model": item.model},
                )

        task = PipelineTask(
            pipeline,
            conversation_id=str(conversation_id),
            enable_tracing=settings.voice_enable_tracing,
            additional_span_attributes={"voice.domain": settings.voice_domain},
            observers=[latency_observer],
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
                conversation_id,
            )
            await task.queue_frames([TTSSpeakFrame(greeting)])

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
        # The runner normally stops processors during task cancellation, but
        # explicitly close the provider as well. This prevents a reconnect
        # from retaining an old Fish/Cartesia audio stream and producing mixed
        # voices in the browser.
        if tts is not None:
            with suppress(Exception):
                await tts.cleanup()
        if telemetry:
            telemetry.record("call_finished")
            with suppress(Exception):
                await telemetry.close()
        await session.close()
        await database.dispose()


async def bot(runner_args: RunnerArguments) -> None:
    """Pipecat runner entry point for browser and Twilio voice sessions."""

    settings = VoiceRuntimeSettings()
    if isinstance(runner_args, SmallWebRTCRunnerArguments):
        transport = SmallWebRTCTransport(
            webrtc_connection=runner_args.webrtc_connection,
            params=TransportParams(
                audio_in_enabled=True,
                audio_out_enabled=True,
                audio_out_mixer=create_audio_out_mixer(settings),
            ),
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
                    audio_out_mixer=create_audio_out_mixer(settings),
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
        form_values = parse_twilio_form(raw_body)
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
