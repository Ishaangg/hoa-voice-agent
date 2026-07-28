"""Run a browser WebRTC HOA voice session with Pipecat.

Install first: ``python -m pip install -e \".[voice]\"``.
The Pipecat development runner then serves a browser client at
``http://localhost:7860/client``.
"""

from __future__ import annotations

from contextlib import suppress

from loguru import logger
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.audio.vad.vad_analyzer import VADParams
from pipecat.frames.frames import TTSSpeakFrame, TTSUpdateSettingsFrame
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineParams, PipelineTask
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import (
    LLMContextAggregatorPair,
    LLMUserAggregatorParams,
)
from pipecat.runner.types import RunnerArguments, SmallWebRTCRunnerArguments
from pipecat.services.nvidia.llm import NvidiaLLMService
from pipecat.services.nvidia.stt import NvidiaSTTService
from pipecat.services.nvidia.tts import NvidiaTTSService
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
from hoa_voice_agent.voice.settings import VoiceRuntimeSettings
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
        from pipecat.services.whisper.stt import WhisperSTTService
    except ImportError as exc:
        raise RuntimeError(
            "Local Whisper is not installed. Run: python -m pip install -e \".[voice]\""
        ) from exc
    return WhisperSTTService(
        settings=WhisperSTTService.Settings(model=settings.whisper_model),
        device=settings.whisper_device,
        compute_type=settings.whisper_compute_type,
    )


async def run_bot(transport: SmallWebRTCTransport, settings: VoiceRuntimeSettings) -> None:
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
            )
            if settings.sms_mode == "twilio"
            else FakeMessagingAdapter()
        )
        conversation_service = ConversationService(
            session=session,
            community_management=community_management,
            messaging=messaging,
            call_control=FakeCallControlAdapter(),
            incident_extractor=NvidiaIncidentExtractor(
                api_key=settings.nvidia_api_key,
                base_url=settings.nvidia_llm_base_url,
                model=settings.nvidia_llm_model,
            ),
        )
        conversation = await conversation_service.start(
            settings.voice_demo_caller_phone,
            COMMUNITY_ID,
        )

        stt = create_stt(settings)
        tts = NvidiaTTSService(
            api_key=settings.nvidia_api_key,
            server=settings.nvidia_tts_server,
            use_ssl=settings.use_ssl_for(settings.nvidia_tts_server),
            model_function_map={
                "function_id": settings.nvidia_tts_function_id,
                "model_name": settings.nvidia_tts_model,
            },
            settings=NvidiaTTSService.Settings(voice=settings.nvidia_tts_voice),
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
            settings.nvidia_tts_voice,
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
            await task.queue_frames(
                [
                    TTSUpdateSettingsFrame(settings={"voice": settings.nvidia_tts_greeting_voice}),
                    TTSSpeakFrame(conversation.say),
                ]
            )

        @transport.event_handler("on_client_connected")
        async def on_client_connected(_transport, _connection) -> None:
            logger.info("WebRTC client connected; waiting for RTVI client-ready handshake.")

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
    """Pipecat runner entry point for browser WebRTC sessions."""

    if not isinstance(runner_args, SmallWebRTCRunnerArguments):
        raise RuntimeError("This HOA voice bot currently supports Pipecat SmallWebRTC only.")

    settings = VoiceRuntimeSettings()
    transport = SmallWebRTCTransport(
        webrtc_connection=runner_args.webrtc_connection,
        params=TransportParams(audio_in_enabled=True, audio_out_enabled=True),
    )
    await run_bot(transport, settings)


def main() -> None:
    try:
        from pipecat.runner.run import main as runner_main
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            "The Pipecat runner is missing. Install the optional voice runtime with "
            "`python -m pip install -e \".[voice]\"`."
        ) from exc
    # Validate before the browser can connect. This prevents a configuration error
    # from appearing as an apparently successful but silent WebRTC session.
    VoiceRuntimeSettings()
    runner_main()


if __name__ == "__main__":
    with suppress(KeyboardInterrupt):
        main()
