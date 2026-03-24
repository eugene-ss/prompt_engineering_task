from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

import yaml
from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict
from rich.logging import RichHandler

logger = logging.getLogger(__name__)

PACKAGE_ROOT = Path(__file__).resolve().parent

def _build_version_map() -> dict[str, str]:
    """Build version map from prompts/<version>.md files in the package. New files are picked up automatically."""
    prompts_dir = PACKAGE_ROOT / "prompts"
    versions: list[tuple[int, Path]] = []
    if prompts_dir.exists():
        for path in prompts_dir.glob("v*.md"):
            try:
                n = int(path.stem[1:])
                versions.append((n, path))
            except ValueError:
                pass
    versions.sort(key=lambda x: x[0])
    if not versions:
        return {"v1": "prompts/v1.md"}
    return {f"v{n}": f"prompts/v{n}.md" for n, _ in versions}


VERSION_MAP: dict[str, str] = _build_version_map()

class AppConfig(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    API_KEY: SecretStr = Field(
        ...,
        description="API key for the LLM endpoint",
    )
    ENDPOINT_URL: str = Field(
        ...,
        description="Base URL for the LLM proxy",
    )
    MODEL_NAME: str = Field(
        default="gpt-4",
        description="Config file selector: load config/{MODEL_NAME}.yaml (defines model, api, temperature, max_tokens)",
    )
    MAX_CONCURRENT_REQUESTS: int = Field(default=50, ge=1)
    REQUEST_TIMEOUT_SECONDS: float = Field(default=120.0, gt=0)
    LOG_LEVEL: str = Field(default="INFO")

@dataclass
class LLMConfig:
    model: str
    api: str
    temperature: float
    max_tokens: int

def load_llm_config(model_name: str) -> LLMConfig:
    """Load LLM config from config/{model_name}.yaml.

    Raises FileNotFoundError if the config file is missing, and
    KeyError if required keys are absent — the YAML file is the
    single source of truth for LLM parameters (model, api, temperature, max_tokens).
    """
    path = Path("config") / f"{model_name}.yaml"
    if not path.exists():
        raise FileNotFoundError(
            f"LLM config not found: {path}. "
            f"Create config/{model_name}.yaml with 'model', 'api', 'temperature', and 'max_tokens'."
        )
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    required = {"model", "api", "temperature", "max_tokens"}
    missing = required - data.keys()
    if missing:
        raise KeyError(
            f"Missing required keys in {path}: {', '.join(sorted(missing))}"
        )
    return LLMConfig(
        model=str(data["model"]).strip(),
        api=str(data["api"]).strip(),
        temperature=float(data["temperature"]),
        max_tokens=int(data["max_tokens"]),
    )

def setup_logging(level: str = "INFO") -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[RichHandler(rich_tracebacks=True, show_time=True, show_path=False)],
        force=True,
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)