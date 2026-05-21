from pydantic_settings import BaseSettings
from functools import lru_cache
import json
from pathlib import Path


class Settings(BaseSettings):
    # App
    app_env: str = "development"
    app_port: int = 8000
    secret_key: str = "change_me"

    # Image quality
    max_image_size_mb: int = 10
    min_image_width: int = 400
    min_image_height: int = 250
    blur_threshold_fail: float = 30.0
    blur_threshold_warn: float = 80.0
    glare_pixel_ratio: float = 0.08

    # OCR backend
    ocr_backend: str = "paddleocr"  # paddleocr | aws_textract

    # AWS
    aws_access_key_id: str = ""
    aws_secret_access_key: str = ""
    aws_region: str = "us-east-1"

    # Logging
    log_level: str = "INFO"

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"


@lru_cache()
def get_settings() -> Settings:
    return Settings()


# ---------------------------------------------------------------------------
# Jurisdiction policy loader
# ---------------------------------------------------------------------------
_POLICY_PATH = Path(__file__).parent.parent.parent / "config" / "jurisdiction_policy.json"


@lru_cache()
def load_jurisdiction_policy() -> dict:
    """Return the full jurisdiction policy dict, keyed by two-letter state code."""
    with open(_POLICY_PATH, "r") as f:
        return json.load(f)


def get_state_policy(state_code: str) -> dict:
    """Return policy for a specific state, or the default fallback."""
    policy = load_jurisdiction_policy()
    return policy.get(state_code.upper(), policy["DEFAULT"])
