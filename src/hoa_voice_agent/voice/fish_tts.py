"""Pipecat TTS adapter for Fish Audio's realtime WebSocket API.

Fish returns raw PCM chunks when ``format=pcm`` is selected.  That lets us
forward audio directly to Pipecat without an MP3 decoder or a second buffering
stage, which is important for conversational latency.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncGenerator, AsyncIterator

from loguru import logger
from pipecat.frames.frames import Frame, UserStartedSpeakingFrame, VADUserStartedSpeakingFrame
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.services.settings import TTSSettings
from pipecat.services.tts_service import TextAggregationMode, TTSService


class FishAudioTTSService(TTSService):
    """Stream Fish Audio PCM output into Pipecat audio frames.

    The service intentionally uses sentence aggregation by default. Pipecat's
    token mode invokes ``run_tts`` once per token, which would open a separate
    Fish session for every token. Instead, each sentence is sent through one
    WebSocket session and flushed immediately so Fish does not wait for its
    server-side text buffer.
    """

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        reference_id: str,
        sample_rate: int = 24000,
        latency: str = "balanced",
        chunk_length: int = 100,
        min_chunk_length: int = 0,
        condition_on_previous_chunks: bool = False,
        preconnect: bool = True,
        speed: float = 1.0,
        temperature: float = 0.7,
        top_p: float = 0.7,
        style: str = "warm, natural, professional, empathetic",
        text_aggregation_mode: TextAggregationMode = TextAggregationMode.SENTENCE,
    ) -> None:
        try:
            # Import from the SDK modules rather than the package root. This
            # also works in environments where the legacy local `fishaudio`
            # package has left a minimal root __init__.py behind.
            import ormsgpack
            from fishaudio.client import AsyncFishAudio
            from fishaudio.resources.realtime import aiter_websocket_audio
            from fishaudio.types import (
                CloseEvent,
                FlushEvent,
                Prosody,
                StartEvent,
                TextEvent,
                TTSConfig,
                TTSRequest,
            )
            from httpx_ws import aconnect_ws
        except ImportError as exc:  # pragma: no cover - exercised in misconfigured installs
            raise RuntimeError(
                'Fish Audio support is not installed. Run: python -m pip install -e ".[voice]"'
            ) from exc

        super().__init__(
            # Fish does not select a locale in this adapter; explicitly mark
            # it unsupported so Pipecat does not emit a missing-settings error.
            settings=TTSSettings(model=model, voice=reference_id, language=None),
            text_aggregation_mode=text_aggregation_mode,
            # Fish emits the PCM frames; TTSService should not synthesize a
            # second stop frame around the provider's streamed context.
            sample_rate=sample_rate,
            push_text_frames=True,
        )
        self._fish_client = AsyncFishAudio(api_key=api_key)
        self._fish_tts_config = TTSConfig(
            format="pcm",
            sample_rate=sample_rate,
            # The current SDK's Pydantic type omits the API's ``low`` value.
            # The request-level override below carries the value to Fish.
            latency="balanced",
            chunk_length=chunk_length,
            min_chunk_length=min_chunk_length,
            condition_on_previous_chunks=condition_on_previous_chunks,
            prosody=Prosody(speed=speed),
            temperature=temperature,
            top_p=top_p,
        )
        self._fish_model = model
        self._fish_reference_id = reference_id
        self._fish_sample_rate = sample_rate
        self._fish_style = style.strip()
        self._fish_latency = latency
        self._fish_text_event = TextEvent
        self._fish_flush_event = FlushEvent
        self._fish_close_event = CloseEvent
        self._fish_start_event = StartEvent
        self._fish_tts_request = TTSRequest
        self._fish_aiter_audio = aiter_websocket_audio
        self._fish_aconnect_ws = aconnect_ws
        self._fish_ormsgpack = ormsgpack
        self._fish_preconnect = preconnect
        self._preconnect_task = None
        self._preconnect_commands = None
        self._preconnect_audio = None
        self._preconnect_ready = None

    async def on_turn_context_created(self, context_id: str) -> None:
        """Start the Fish handshake while the LLM is generating its reply."""

        await super().on_turn_context_created(context_id)
        await self.warmup_next_turn()

    async def warmup_next_turn(self) -> None:
        """Open a socket early while the caller is still speaking."""

        if not self._fish_preconnect:
            return
        if self._preconnect_task is not None and not self._preconnect_task.done():
            return
        self._preconnect_commands = asyncio.Queue(maxsize=1)
        self._preconnect_audio = asyncio.Queue()
        self._preconnect_ready = asyncio.Event()
        self._preconnect_task = asyncio.create_task(self._preconnect_owner())

    async def on_turn_context_completed(self) -> None:
        """Close an idle preconnect when a turn produced no spoken text."""

        await super().on_turn_context_completed()
        await self._cancel_preconnect()

    def _styled_text(self, text: str) -> str:
        """Add an S2 style and turn-level prosody cue without changing the transcript.

        S2 supports natural-language bracket cues inline.  A single static cue
        makes every turn land with the same cadence, so the adapter adds a
        restrained cue based on the speech act (question, reassurance,
        greeting, or urgent notice).  The cue is sent only to Fish; Pipecat's
        text frames continue to use the original transcript.
        """
        if not self._fish_style or text.lstrip().startswith("["):
            return text
        return f"[{self._fish_style}; {self._prosody_cue(text)}] {text}"

    @staticmethod
    def _prosody_cue(text: str) -> str:
        """Choose a subtle, human delivery direction for one response turn."""

        normalized = " ".join(text.lower().split())
        if "?" in text or normalized.startswith(("may i", "can i", "would you")):
            return "warm and curious, with a gentle question intonation"
        if any(
            term in normalized
            for term in ("emergency", "immediately", "safety", "danger", "fire", "smoke")
        ):
            return "calm and focused, reassuring but clearly urgent"
        if any(term in normalized for term in ("sorry", "apologize", "apologies")):
            return "gently apologetic and empathetic"
        if any(
            term in normalized
            for term in ("thank you", "thanks", "got it", "of course", "no problem")
        ):
            return "friendly and affirming, lightly upbeat"
        if any(term in normalized for term in ("welcome", "hello", "hi there")):
            return "warm and welcoming, like a natural hotel concierge"
        return "warm and conversational, with natural pauses and gentle emphasis"

    async def run_tts(self, text: str, context_id: str) -> AsyncGenerator[Frame | None, None]:
        audio_started = False
        styled_text = self._styled_text(text)
        request_started = time.perf_counter()

        async def text_stream():
            # Send a typed text event and flush immediately. Without the flush,
            # Fish waits for chunk_length/min_chunk_length or the stop event,
            # which puts its text-buffer delay back on the critical path.
            yield self._fish_text_event(text=styled_text)
            yield self._fish_flush_event()

        logger.debug(
            "Fish Audio TTS request model={} reference={} chars={}",
            self._fish_model,
            self._fish_reference_id,
            len(text),
        )
        try:
            # A preconnect is useful only when its handshake completed before
            # text reaches TTS. The LLM response-start hook can arrive too late
            # for a cold Fish socket; waiting for that socket would add several
            # seconds to first audio. Fall back immediately to the direct
            # stream in that case.
            if (
                self._preconnect_task is not None
                and not self._preconnect_task.done()
                and self._preconnect_ready is not None
                and self._preconnect_ready.is_set()
            ):
                audio_stream = self._stream_preconnected_text(styled_text)
            else:
                if self._preconnect_task is not None and not self._preconnect_task.done():
                    await self._cancel_preconnect()
                audio_stream = self._stream_fish(text_stream(), self._fish_latency)
            async for frame in self._stream_audio_frames_from_iterator(
                audio_stream,
                in_sample_rate=self._fish_sample_rate,
                context_id=context_id,
            ):
                if not audio_started:
                    audio_started = True
                    logger.debug(
                        "Fish Audio first audio latency={}ms context={} chars={}",
                        round((time.perf_counter() - request_started) * 1000, 1),
                        context_id,
                        len(text),
                    )
                yield frame
        except Exception as exc:
            # If a WebSocket request fails before any audio is delivered, retry
            # once with the SDK's balanced mode so a call does not fail outright.
            if audio_started:
                raise
            logger.warning("Fish Audio TTS stream failed; retrying balanced stream: {}", exc)
            audio_stream = self._stream_fish(text_stream(), "balanced")
            async for frame in self._stream_audio_frames_from_iterator(
                audio_stream,
                in_sample_rate=self._fish_sample_rate,
                context_id=context_id,
            ):
                yield frame

    async def _preconnect_owner(self):
        """Own a preconnected Fish stream from open through close.

        ``httpx-ws`` cancellation scopes must be entered and exited in the
        same task. This owner task keeps that lifecycle together while the
        voice pipeline sends the first text through an asyncio queue.
        """

        wrapper = self._fish_client._client_wrapper
        request = self._fish_tts_request(
            text="",
            format="pcm",
            sample_rate=self._fish_sample_rate,
            # The preconnected path bypasses the SDK helper that normally
            # attaches ``reference_id``. Without this field Fish can use its
            # default voice for the response after the greeting, which makes
            # one call sound like it changed speakers.
            reference_id=self._fish_reference_id,
            chunk_length=self._fish_tts_config.chunk_length,
            min_chunk_length=self._fish_tts_config.min_chunk_length,
            condition_on_previous_chunks=self._fish_tts_config.condition_on_previous_chunks,
            prosody=self._fish_tts_config.prosody,
            temperature=self._fish_tts_config.temperature,
            top_p=self._fish_tts_config.top_p,
        )
        # Fish's API accepts low even though the installed SDK's type omits it.
        request.latency = self._fish_latency
        cm = self._fish_aconnect_ws(
            "/v1/tts/live",
            client=wrapper.client,
            headers=wrapper.get_headers({"model": self._fish_model}),
        )
        try:
            ws = await cm.__aenter__()
            if self._preconnect_ready is not None:
                self._preconnect_ready.set()
            await ws.send_bytes(
                self._fish_ormsgpack.packb(
                    self._fish_start_event(request=request).model_dump()
                )
            )
            logger.debug("Fish Audio WebSocket preconnected context={}", id(ws))
            text = await self._preconnect_commands.get()
            await ws.send_bytes(
                self._fish_ormsgpack.packb(
                    self._fish_text_event(text=text).model_dump()
                )
            )
            await ws.send_bytes(
                self._fish_ormsgpack.packb(self._fish_flush_event().model_dump())
            )
            await ws.send_bytes(
                self._fish_ormsgpack.packb(self._fish_close_event().model_dump())
            )
            async for chunk in self._fish_aiter_audio(ws):
                await self._preconnect_audio.put(chunk)
        except Exception as exc:
            logger.debug("Fish preconnect stream failed: {}", exc)
            if self._preconnect_audio is not None:
                await self._preconnect_audio.put(exc)
        finally:
            if self._preconnect_audio is not None:
                await self._preconnect_audio.put(None)
            await cm.__aexit__(None, None, None)

    async def _stream_preconnected_text(self, text: str) -> AsyncIterator[bytes]:
        """Send one utterance through the WebSocket opened at turn start."""

        if self._preconnect_commands is None or self._preconnect_audio is None:
            raise RuntimeError("Fish preconnect queues are not initialized")
        await self._preconnect_commands.put(text)
        try:
            while True:
                item = await self._preconnect_audio.get()
                if item is None:
                    break
                if isinstance(item, Exception):
                    raise item
                yield item
        finally:
            # If TTS is interrupted, cancel the owner so its WebSocket closes
            # in the task that entered the cancellation scope.
            await self._cancel_preconnect()

    def _stream_fish(self, text_stream, latency: str):
        """Create one Fish stream while preserving the SDK's async client.

        ``latency=low`` is accepted by Fish's API but is missing from the
        installed SDK's Literal type. The SDK assigns the request-level value
        to its Pydantic request without assignment validation, so the API
        value can be used safely while keeping the SDK transport and retries.
        """

        return self._fish_client.tts.stream_websocket(
            text_stream,
            reference_id=self._fish_reference_id,
            format="pcm",
            latency=latency,  # type: ignore[arg-type]
            config=self._fish_tts_config,
            # The server accepts the current S2.1 Pro model name in the header.
            model=self._fish_model,  # type: ignore[arg-type]
        )

    async def cleanup(self) -> None:
        await self._cancel_preconnect()
        await self._fish_client.close()
        await super().cleanup()

    async def _cancel_preconnect(self) -> None:
        if self._preconnect_task is not None and not self._preconnect_task.done():
            self._preconnect_task.cancel()
            try:
                await self._preconnect_task
            except asyncio.CancelledError:
                pass
        self._preconnect_task = None
        self._preconnect_commands = None
        self._preconnect_audio = None
        self._preconnect_ready = None


class FishAudioWarmupProcessor(FrameProcessor):
    """Warm Fish while the caller speaks, before LLM/TTS work starts."""

    def __init__(self, tts: FishAudioTTSService) -> None:
        super().__init__()
        self._tts = tts

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        # Preserve Pipecat's StartFrame/CancelFrame lifecycle bookkeeping
        # before forwarding frames or warming the provider.
        await super().process_frame(frame, direction)
        if isinstance(frame, (UserStartedSpeakingFrame, VADUserStartedSpeakingFrame)):
            await self._tts.warmup_next_turn()
        await self.push_frame(frame, direction)
