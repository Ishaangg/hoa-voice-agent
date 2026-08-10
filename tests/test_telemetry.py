from sqlalchemy import select

from hoa_voice_agent.persistence.database import Database
from hoa_voice_agent.persistence.tables import VoiceMetricRecord
from hoa_voice_agent.services.telemetry import CallTelemetry


async def test_call_telemetry_flushes_without_transcript_data() -> None:
    database = Database("sqlite+aiosqlite:///:memory:")
    await database.create_schema()
    telemetry = CallTelemetry(
        database,
        call_id="call-1",
        conversation_id=None,
        domain="hotel",
    )
    telemetry.record("first_audio_latency", 241.4, metadata={"provider": "fish"})
    telemetry.record("task_success", 1)
    await telemetry.close()

    async with database.session_factory() as session:
        rows = list(
            await session.scalars(
                select(VoiceMetricRecord).order_by(VoiceMetricRecord.metric_name)
            )
        )
    assert [row.metric_name for row in rows] == ["first_audio_latency", "task_success"]
    assert rows[0].value_ms == 241
    assert "provider" in (rows[0].metadata_json or "")
    await database.dispose()
