"""Runtime configuration, loaded from the environment and ``.env``.

API keys are deliberately *not* CLI options. Command-line arguments end up in
shell history, process listings and CI logs; an environment variable does not.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

# Published rates, used only to put an estimate in the run report. Both are worth
# re-checking before anyone budgets against them.
SERPER_USD_PER_1K_QUERIES = 1.0
SERPER_FREE_QUERIES = 2_500
GEMINI_USD_PER_1K_GROUNDED_SEARCHES = 14.0
GEMINI_FREE_GROUNDED_SEARCHES_PER_MONTH = 5_000

DEFAULT_MODEL = "gemini-3.6-flash"
DEFAULT_PROVIDER = "serper"


class Settings(BaseSettings):
    """Configuration for a prospecting run."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_prefix="BP_",
        extra="ignore",
    )

    # Read without the BP_ prefix so each matches the name its own vendor uses.
    serper_api_key: SecretStr | None = Field(default=None, validation_alias="SERPER_API_KEY")
    gemini_api_key: SecretStr | None = Field(default=None, validation_alias="GEMINI_API_KEY")

    provider: str = Field(default=DEFAULT_PROVIDER)
    gemini_model: str = Field(default=DEFAULT_MODEL, validation_alias="BP_MODEL")

    # A runaway loop against a metered API is the expensive failure mode, so the
    # ceiling is configuration rather than something a caller can forget to pass.
    # This is an account-level guard, not a property of any one search, which is
    # why it stays here rather than moving into search.yaml.
    max_queries: int = Field(default=50, ge=1)

    # Left unset by default: Serper rejects num > 10 on free accounts when the
    # query uses search operators, which every X-ray query does. Paid plans may
    # set this up to 100. A property of your plan, not of the search.
    results_per_page: int | None = Field(default=None, ge=1, le=100)

    concurrency: int = Field(default=3, ge=1, le=10)
    rate_limit_per_minute: int = Field(default=30, ge=1)

    cache_dir: Path = Field(default=Path(".cache"))
    cache_ttl_hours: int = Field(default=168, ge=0)

    request_timeout_seconds: float = Field(default=30.0, gt=0)
    max_retries: int = Field(default=4, ge=0)

    def require_serper_key(self) -> str:
        """Return the Serper key, or explain precisely how to supply one.

        Raises:
            RuntimeError: if no key is configured.
        """
        return self._require(
            self.serper_api_key,
            "SERPER_API_KEY",
            "https://serper.dev (2,500 free queries, no card required)",
        )

    def require_gemini_key(self) -> str:
        """Return the Gemini key, or explain precisely how to supply one.

        Raises:
            RuntimeError: if no key is configured.
        """
        return self._require(
            self.gemini_api_key,
            "GEMINI_API_KEY",
            "https://aistudio.google.com/apikey",
        )

    @staticmethod
    def _require(value: SecretStr | None, name: str, where: str) -> str:
        """Unwrap a required secret or raise with actionable guidance.

        Raises:
            RuntimeError: if the secret is missing or blank.
        """
        if value is None or not value.get_secret_value().strip():
            raise RuntimeError(
                f"No {name} found. Copy .env.example to .env and paste a key from "
                f"{where}, or run with --demo to use the bundled offline fixtures instead."
            )
        return value.get_secret_value()
