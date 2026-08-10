import pytest

from hoa_voice_agent.config import Settings


def test_production_requires_postgresql() -> None:
    with pytest.raises(ValueError, match="DATABASE_URL must use PostgreSQL"):
        Settings(app_env="production")

    with pytest.raises(ValueError, match="DATABASE_AUTO_CREATE must be false"):
        Settings(
            app_env="production",
            database_url="postgresql+asyncpg://hoa:hoa@db/hoa_voice_agent",
        )

    settings = Settings(
        app_env="production",
        database_url="postgresql+asyncpg://hoa:hoa@db/hoa_voice_agent",
        database_auto_create=False,
    )
    assert settings.database_auto_create is False
