from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    tmdb_api_key: str = Field(..., alias="TMDB_API_KEY")
    youtube_api_key: str = Field(..., alias="YOUTUBE_API_KEY")

    gcp_project: str = Field(..., alias="GCP_PROJECT")
    bq_dataset: str = Field("movie_trailers", alias="BQ_DATASET")
    bq_location: str = Field("us-central1", alias="BQ_LOCATION")

    discover_window_months: int = Field(6, alias="DISCOVER_WINDOW_MONTHS")
    tracking_grace_days: int = Field(7, alias="TRACKING_GRACE_DAYS")
    box_office_min_age_days: int = Field(180, alias="BOX_OFFICE_MIN_AGE_DAYS")
    max_active_trailers: int = Field(9300, alias="MAX_ACTIVE_TRAILERS")
    transcripts_max_per_run: int = Field(100, alias="TRANSCRIPTS_MAX_PER_RUN")
    whisper_model_name: str = Field("small", alias="WHISPER_MODEL_NAME")

    tmdb_regions: list[str] = Field(
        default_factory=lambda: ["US", "GB", "FR", "DE", "IT", "ES", "JP", "KR", "IN", "BR", "MX"],
        alias="TMDB_REGIONS",
    )

    # --- Digest email (optional; only required when running `mt send-digest`) ---
    smtp_host: str | None = Field(None, alias="SMTP_HOST")
    smtp_port: int = Field(587, alias="SMTP_PORT")
    # SMTP_SENDER is the From address; also reused as the SMTP login username
    # unless SMTP_USERNAME is set explicitly. Yahoo, Gmail, etc. all expect
    # username == full email when authenticating with an app password.
    smtp_sender: str | None = Field(None, alias="SMTP_SENDER")
    smtp_username: str | None = Field(None, alias="SMTP_USERNAME")
    smtp_password: str | None = Field(None, alias="SMTP_PASSWORD")
    # SMTP_SSL=true → implicit TLS (SMTPS, usually port 465).
    # SMTP_SSL=false (default) → STARTTLS upgrade on port 587.
    smtp_ssl: bool = Field(False, alias="SMTP_SSL")
    digest_email_to: str | None = Field(None, alias="DIGEST_EMAIL_TO")

    # --- Content engine (optional; `mt suggest-content --polish`) ---
    # Falls back to ANTHROPIC_API_KEY from the environment if unset.
    anthropic_api_key: str | None = Field(None, alias="ANTHROPIC_API_KEY")
    content_polish_model: str = Field("claude-opus-4-8", alias="CONTENT_POLISH_MODEL")


def load_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
