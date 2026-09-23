from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="AUTOPROTOCOL_", env_file=".env", extra="ignore")

    data_dir: Path = Path("data")
    models_dir: Path = Path("models")
    device: Literal["cpu", "cuda"] = "cpu"
    max_upload_mb: int = Field(default=250, gt=0)
    max_duration_minutes: int = Field(default=120, gt=0)
    ffmpeg: str = "ffmpeg"
    ffprobe: str = "ffprobe"
    port: int = Field(default=8000, ge=1024, le=65535)
    stage_timeout_seconds: int = Field(default=1800, ge=30)

    @property
    def duration_limit_seconds(self) -> int:
        return min(self.max_duration_minutes, 10) * 60

    @property
    def database_path(self) -> Path:
        return self.data_dir / "autoprotocol.sqlite3"
