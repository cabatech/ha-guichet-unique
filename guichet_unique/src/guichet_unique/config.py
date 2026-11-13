"""Configuration de l'application."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Self

from dotenv import load_dotenv

load_dotenv()


DEFAULT_PERSON_ID = 4
DEFAULT_OUTPUT_DIR = Path(__file__).resolve().parents[2] / "data"


@dataclass(frozen=True, slots=True)
class Config:
    """Configuration figée chargée depuis l'environnement ou des arguments."""

    base_url: str = ""
    person_id: int = DEFAULT_PERSON_ID
    username: str = ""
    password: str = ""
    output_dir: Path = field(default_factory=lambda: DEFAULT_OUTPUT_DIR)
    user_agent: str = (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    )
    request_timeout: float = 30.0
    request_retries: int = 3
    school_year_start_month: int = 9

    # Dans l'add-on, le Supervisor fournit SUPERVISOR_TOKEN et le proxy http://supervisor/core.
    ha_url: str = "http://supervisor/core"
    ha_token: str = ""
    ha_calendar_entity: str = ""
    default_location: str = ""
    mqtt_host: str = ""
    mqtt_port: int = 1883
    mqtt_username: str = ""
    mqtt_password: str = ""

    @classmethod
    def from_env(cls, *, person_id: int | None = None, output_dir: Path | None = None) -> Self:
        """Construit la config depuis l'environnement ou des arguments."""
        username = os.getenv("ESPACE_CITOYENS_USERNAME", "")
        password = os.getenv("ESPACE_CITOYENS_PASSWORD", "")
        if not username or not password:
            raise ConfigError(
                "Identifiants manquants. Définissez ESPACE_CITOYENS_USERNAME et "
                "ESPACE_CITOYENS_PASSWORD dans votre fichier .env."
            )
        base_url = os.getenv("ESPACE_CITOYENS_BASE_URL", "")
        if not base_url:
            raise ConfigError(
                "ESPACE_CITOYENS_BASE_URL manquant (ex : "
                "https://www.espace-citoyens.net/<commune>/espace-citoyens)."
            )
        return cls(
            base_url=base_url,
            person_id=person_id or int(os.getenv("ESPACE_CITOYENS_PERSON_ID", DEFAULT_PERSON_ID)),
            username=username,
            password=password,
            output_dir=output_dir or Path(os.getenv("GUICHET_OUTPUT_DIR", DEFAULT_OUTPUT_DIR)),
            ha_url=os.getenv("HA_URL", "http://supervisor/core"),
            ha_token=os.getenv("SUPERVISOR_TOKEN") or os.getenv("HA_TOKEN", ""),
            ha_calendar_entity=os.getenv("HA_CALENDAR_ENTITY", ""),
            default_location=os.getenv("GUICHET_DEFAULT_LOCATION", ""),
            mqtt_host=os.getenv("MQTT_HOST", ""),
            mqtt_port=int(os.getenv("MQTT_PORT", "1883")),
            mqtt_username=os.getenv("MQTT_USERNAME", ""),
            mqtt_password=os.getenv("MQTT_PASSWORD", ""),
        )


class ConfigError(RuntimeError):
    """Erreur de configuration."""
