import httpx

from hoa_voice_agent.domain.models import IncidentType
from hoa_voice_agent.services.nvidia_incident_extractor import NvidiaIncidentExtractor


def extractor_with_response(payload: dict) -> NvidiaIncidentExtractor:
    transport = httpx.MockTransport(lambda _request: httpx.Response(200, json=payload))
    return NvidiaIncidentExtractor(
        api_key="test-key",
        base_url="https://example.test/v1",
        model="test-model",
        client_factory=lambda: httpx.AsyncClient(transport=transport),
    )


async def test_nemotron_extractor_uses_structured_facts() -> None:
    extractor = extractor_with_response(
        {
            "choices": [
                {
                    "message": {
                        "content": (
                            '{"incident_type":"gas_smell","location":"lobby",'
                            '"water_active":null,"source_known":false,"resident_safe":null}'
                        )
                    }
                }
            ]
        }
    )

    report = await extractor.extract("I smell gas in the lobby.")

    assert report.incident_type == IncidentType.GAS_SMELL
    assert report.location == "lobby"
    assert report.source_known is False
    assert report.resident_safe is None


async def test_nemotron_extractor_falls_back_when_response_is_not_json() -> None:
    extractor = extractor_with_response({"choices": [{"message": {"content": "not JSON"}}]})

    report = await extractor.extract("Water is leaking from the ceiling.")

    assert report.incident_type == IncidentType.ACTIVE_FLOODING
