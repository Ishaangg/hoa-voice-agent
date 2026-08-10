# ruff: noqa: E501
import re
from contextlib import suppress
from typing import Any
from uuid import UUID

from loguru import logger
from pipecat.frames.frames import (
    Frame,
    InterruptionFrame,
    LLMContextFrame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    LLMTextFrame,
    TTSSpeakFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

from hoa_voice_agent.services.hotel_conversation import HotelConversationService
from hoa_voice_agent.services.telemetry import CallTelemetry
from hoa_voice_agent.voice.conversation_pacer import ConversationPacer
from hoa_voice_agent.voice.language import (
    LanguageAdapter,
    VoiceLanguage,
)
from hoa_voice_agent.voice.language import (
    language_adapter as build_language_adapter,
)
from hoa_voice_agent.voice.response_gate import ResponseTurnGate


def _phrase_chunks(text: str, *, max_chars: int = 180) -> list[str]:
    """Split spoken text into coherent phrase-sized TTS chunks."""

    normalized = " ".join(text.split())
    # Providers occasionally return adjacent sentences without whitespace.
    # Add a boundary after question/exclamation punctuation before splitting;
    # otherwise TTS receives ``question?Next`` as one run-on phrase.
    normalized = re.sub(r"([!?])(?=[^\s!?])", r"\1 ", normalized)
    if not normalized:
        return []
    sentences = [part.strip() for part in re.split(r"(?<=[.!?])\s+", normalized) if part.strip()]
    if len(sentences) == 1 and len(sentences[0]) <= max_chars:
        return sentences

    chunks: list[str] = []
    for sentence in sentences:
        if len(sentence) <= max_chars:
            chunks.append(sentence)
            continue
        clauses = [part.strip() for part in re.split(r"(?<=[,;:])\s+", sentence) if part.strip()]
        current = ""
        for clause in clauses:
            candidate = f"{current} {clause}".strip()
            if current and len(candidate) > max_chars:
                chunks.append(current)
                current = clause
            else:
                current = candidate
                while len(current) > max_chars:
                    split_at = current.rfind(" ", 0, max_chars + 1)
                    if split_at < 1:
                        split_at = max_chars
                    chunks.append(current[:split_at].strip())
                    current = current[split_at:].strip()
        if current:
            chunks.append(current)
    return chunks


def _strip_repeated_opening(text: str, opening: str | None) -> str:
    """Remove a model-repeated streaming opening from the final speech.

    The planner may include ``speech_opening`` again in ``speech_final`` even
    though the opening was already sent while the decision was streaming.  We
    compare a whitespace- and typography-normalized prefix so curly quotes or
    an extra space do not result in the acknowledgement being spoken twice.
    """

    candidate = " ".join(text.split()).strip()
    prefix = " ".join((opening or "").split()).strip()
    if not candidate or not prefix:
        return candidate

    def canonical(value: str) -> str:
        return (
            value.casefold()
            .replace("\u2018", "'")
            .replace("\u2019", "'")
            .replace("\u201c", '"')
            .replace("\u201d", '"')
            .replace("\u2013", "-")
            .replace("\u2014", "-")
        )

    prefix_key = canonical(prefix)
    # Use normalized text for the remainder as this is spoken text. It avoids
    # relying on character offsets when a provider used curly punctuation or
    # collapsed whitespace differently from the opening.
    for _ in range(3):
        if not canonical(candidate).startswith(prefix_key):
            break
        candidate = candidate[len(prefix) :].lstrip(" \t\r\n-:;,.!")
        if not candidate:
            break
    return candidate


class HotelWorkflowProcessor(FrameProcessor):
    """Pipecat bridge for hotel guest-service turns."""

    def __init__(
        self,
        service: HotelConversationService,
        conversation_id: UUID,
        initial_language: VoiceLanguage = "en",
        telemetry: CallTelemetry | None = None,
        language_adapter: LanguageAdapter | None = None,
    ) -> None:
        super().__init__()
        self._service = service
        self._conversation_id = conversation_id
        self._language_adapter = language_adapter or build_language_adapter(
            initial_language,
            english_voice="",
            hindi_voice="",
        )
        self._telemetry = telemetry
        self._response_gate = ResponseTurnGate()

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        if isinstance(frame, InterruptionFrame):
            epoch = self._response_gate.mark_interruption()
            logger.debug("Hotel voice response epoch advanced to {}", epoch)
            await super().process_frame(frame, direction)
            await self.push_frame(frame, direction)
            return
        await super().process_frame(frame, direction)
        if isinstance(frame, LLMContextFrame):
            if not self._response_gate.accept_context(frame.context.messages):
                return
            user_text = self._latest_user_text(frame.context.messages)
            if not user_text:
                return
            opening_text: str | None = None
            response_stream_open = False
            turn_epoch = self._response_gate.interruption_epoch

            async def speak_progress(text: str) -> None:
                nonlocal opening_text, response_stream_open
                # A late model chunk from an interrupted turn must never leak
                # into the caller's new response epoch.
                if turn_epoch != self._response_gate.interruption_epoch:
                    logger.debug("Ignoring stale hotel progress phrase for epoch {}", turn_epoch)
                    return
                phrase = " ".join(text.split())
                if not phrase:
                    return
                if opening_text is None:
                    opening_text = phrase
                # Keep progress phrases and the validated final response in a
                # single Cartesia context. A standalone TTSSpeakFrame creates
                # a second context that can race the final stream.
                await self._speak_phrase_stream(
                    phrase,
                    FrameDirection.DOWNSTREAM,
                    start=not response_stream_open,
                    end=False,
                )
                response_stream_open = True
                if self._telemetry:
                    self._telemetry.record("response_pacing_phrase", 1)

            pacer = ConversationPacer(
                speak_progress,
                initial_phrase=self._language_adapter.tool_progress(
                    "default", phrase_index=0
                ),
                follow_up_phrase=self._language_adapter.tool_progress(
                    "default", phrase_index=1
                ),
            )
            # Do not arm generic pacing while the interpreter is still
            # classifying the turn. A tool-specific phrase is offered only
            # after the planner has selected a lookup, so ordinary answers do
            # not sound like database work is happening.
            try:
                route = await self._service.route_voice_turn(
                    self._conversation_id,
                    user_text,
                    on_response=pacer.offer,
                )
            except Exception:
                await pacer.close()
                with suppress(Exception):
                    await self._service.session.rollback()
                logger.exception("Hotel workflow failed for conversation {}", self._conversation_id)
                if self._telemetry:
                    self._telemetry.record("fallback", 1, metadata={"stage": "workflow"})
                fallback = self._language_adapter.text("workflow_failure")
                frame.context.add_message({"role": "assistant", "content": fallback})
                with suppress(Exception):
                    await self._service.record_assistant_turn(self._conversation_id, fallback)
                if response_stream_open:
                    await self._speak_phrase_stream(
                        fallback,
                        FrameDirection.DOWNSTREAM,
                        start=False,
                        end=True,
                    )
                else:
                    await self.push_frame(
                        self._speak_frame(fallback),
                        FrameDirection.DOWNSTREAM,
                    )
                return
            await pacer.close()
            if route.response:
                if self._telemetry:
                    self._telemetry.record(
                        "task_success",
                        1,
                        metadata={"intent": route.response.intent, "mode": "workflow"},
                    )
                    if route.response.intent in {"identity", "correction"}:
                        self._telemetry.record("correction", 1)
                # The opening is intentionally non-committal. Once the
                # validated decision has executed, speak the actual outcome as
                # a second sentence unless it is identical to the opening.
                final_text = _strip_repeated_opening(route.response.say, opening_text)
                if response_stream_open and not final_text:
                    # The planner/tool layer returned only the progress phrase
                    # that has already been spoken.  Ending the TTS context
                    # here leaves the caller stranded after "I'm checking".
                    # A completed turn must always contain a caller-facing
                    # outcome, even when the upstream provider failed to
                    # produce one.
                    logger.warning(
                        "Hotel planner returned progress-only final response for conversation {}",
                        self._conversation_id,
                    )
                    final_text = self._language_adapter.text("understand_failure")
                # Keep the same de-duplicated text in the transcript and
                # assistant context; otherwise the audio sounds correct but
                # the call history still shows the opening twice.
                response_for_context = final_text or route.response.say
                frame.context.add_message(
                    {"role": "assistant", "content": response_for_context}
                )
                if response_stream_open:
                    # Close the same context opened for the early phrase.  The
                    # final text is prefixed with a space by the helper so the
                    # two phrases do not concatenate as ``now.I``.
                    await self._speak_phrase_stream(
                        final_text,
                        FrameDirection.DOWNSTREAM,
                        start=False,
                        end=True,
                    )
                else:
                    await self._speak_phrase_stream(
                        final_text,
                        FrameDirection.DOWNSTREAM,
                        start=True,
                        end=True,
                    )
                return
            # The hotel service is response-complete. A missing response must
            # never fall through to a second general LLM call, otherwise one
            # guest turn can incur two model round trips and duplicate speech.
            fallback = self._language_adapter.text("understand_failure")
            frame.context.add_message({"role": "assistant", "content": fallback})
            await self._service.record_assistant_turn(self._conversation_id, fallback)
            if self._telemetry:
                self._telemetry.record("fallback", 1, metadata={"stage": "planner"})
            if response_stream_open:
                await self._speak_phrase_stream(
                    fallback,
                    FrameDirection.DOWNSTREAM,
                    start=False,
                    end=True,
                )
            else:
                await self.push_frame(
                    self._speak_frame(fallback),
                    FrameDirection.DOWNSTREAM,
                )
            return
        await self.push_frame(frame, direction)

    @staticmethod
    def _latest_user_text(messages: list[Any]) -> str | None:
        for message in reversed(messages):
            if isinstance(message, dict) and message.get("role") == "user":
                content = message.get("content")
                if isinstance(content, str) and content.strip():
                    return content.strip()
        return None

    @staticmethod
    def _ensure_system_prompt(messages: list[Any], prompt: str | None) -> None:
        if not prompt:
            return
        if messages and isinstance(messages[0], dict) and messages[0].get("role") == "system":
            messages[0]["content"] = prompt
        else:
            messages.insert(0, {"role": "system", "content": prompt})

    def _speak_frame(self, text: str) -> TTSSpeakFrame:
        """Create a standalone response without duplicating context history."""

        frame = TTSSpeakFrame(text, append_to_context=False)
        frame.metadata["voice_response_epoch"] = self._response_gate.interruption_epoch
        return frame

    async def _speak_phrase_stream(
        self,
        text: str,
        direction: FrameDirection,
        *,
        start: bool = True,
        end: bool = True,
    ) -> None:
        """Send validated final speech through one Cartesia TTS context.

        This runs only after the decision has been parsed and side effects have
        completed. Phrase chunks therefore remain truthful while Cartesia gets
        one coherent streaming context instead of a new request per sentence.
        """

        if start:
            await self.push_frame(LLMFullResponseStartFrame(), direction)
        chunks = _phrase_chunks(text)
        for index, chunk in enumerate(chunks):
            # LLMTextFrame marks its contents as already carrying inter-frame
            # spacing. Preserve a boundary when appending to an existing
            # opening context instead of producing ``now.I``.
            if not start and index == 0 and not chunk[:1].isspace():
                chunk = f" {chunk}"
            frame = LLMTextFrame(chunk)
            # The workflow already persisted the final assistant response.
            # Avoid duplicating it in the Pipecat assistant context.
            frame.append_to_context = False
            await self.push_frame(frame, direction)
        if end:
            await self.push_frame(LLMFullResponseEndFrame(), direction)
