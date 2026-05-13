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


def load_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
