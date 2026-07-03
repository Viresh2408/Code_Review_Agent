"""
Settings — loaded from environment variables / .env file.

All secrets are read here and nowhere else, so a quick grep for
`settings.` finds every secret access site in the codebase.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Typed, validated application configuration."""

    model_config = SettingsConfigDict(
        env_file=(".env", "../.env"),
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ── App ────────────────────────────────────────────────────────────────────
    app_env: str = "development"
    log_level: str = "INFO"
    max_inline_comments: int = 15
    max_cost_per_review_usd: float = 0.50

    # ── GitHub App ─────────────────────────────────────────────────────────────
    github_app_id: str
    github_app_slug: str = ""
    github_private_key_path: str = "secrets/github-app.pem"
    github_webhook_secret: str

    @property
    def github_private_key(self) -> str:
        """Read the PEM key from disk at access time (not at import time)."""
        key_path = Path(self.github_private_key_path)
        if not key_path.exists():
            raise FileNotFoundError(
                f"GitHub App private key not found at: {key_path.resolve()}\n"
                "Download it from your GitHub App settings and place it at "
                f"`{self.github_private_key_path}`."
            )
        return key_path.read_text()

    # ── Database ───────────────────────────────────────────────────────────────
    database_url: str = "postgresql+asyncpg://postgres:postgres@localhost:5432/codereview"
    sync_database_url: str = "postgresql://postgres:postgres@localhost:5432/codereview"

    # ── Redis / Celery ─────────────────────────────────────────────────────────
    redis_url: str = "redis://localhost:6379/0"
    celery_broker_url: str = "redis://localhost:6379/0"
    celery_result_backend: str = "redis://localhost:6379/1"

    # ── ChromaDB ──────────────────────────────────────────────────────────────
    chroma_host: str = "localhost"
    chroma_port: int = 8001

    # ── Neo4j ─────────────────────────────────────────────────────────────────
    neo4j_uri: str = "bolt://localhost:7687"
    neo4j_user: str = "neo4j"
    neo4j_password: str = "password"

    # ── LLM APIs ──────────────────────────────────────────────────────────────
    anthropic_api_key: str = ""

    # ── JWT Security ──────────────────────────────────────────────────────────
    jwt_secret_key: str = "dev-jwt-secret-key-change-in-production"



@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the cached Settings singleton. Call this everywhere."""
    return Settings()
