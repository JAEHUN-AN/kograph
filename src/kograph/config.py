"""Central configuration. All secrets come from environment / .env — never hardcode."""

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Validated at startup: missing required keys fail fast with a clear message."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # DART OpenAPI
    dart_api_key: str = Field(..., description="opendart.fss.or.kr 인증키")

    # Oracle (raw layer)
    oracle_host: str = "localhost"
    oracle_port: int = 1521
    oracle_service: str = "FREEPDB1"
    oracle_user: str = "kograph"
    oracle_password: str = Field(..., description="Oracle APP_USER password")

    # Anthropic (Week 2+)
    anthropic_api_key: str = ""

    # Neo4j (Week 2)
    neo4j_uri: str = "bolt://localhost:7687"
    neo4j_user: str = "neo4j"
    neo4j_password: str = ""

    @property
    def oracle_dsn(self) -> str:
        return f"{self.oracle_host}:{self.oracle_port}/{self.oracle_service}"


@lru_cache
def get_settings() -> Settings:
    return Settings()
