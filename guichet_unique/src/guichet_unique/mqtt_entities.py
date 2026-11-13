"""Entités Home Assistant via MQTT discovery (capteurs cantine/périscolaire, coûts).

Un seul topic d'état JSON (retenu) ; chaque entité lit sa clé via ``value_template``.
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime, timedelta

from paho.mqtt import publish

from ._version import __version__
from .config import Config
from .models import Service

logger = logging.getLogger(__name__)

STATE_TOPIC = "guichet_unique/state"
DEVICE = {
    "identifiers": ["guichet_unique"],
    "name": "Guichet Unique",
    "manufacturer": "espace-citoyens.net",
    "model": "HA - Guichet Unique",
    "sw_version": __version__,
}


def compute_states(
    inscriptions: list[dict], current_month, previous_month, last_sync: str | None, today: date,
) -> tuple[dict, dict]:
    """Retourne (entités, état) ; ``*_month`` = objets ``_Breakdown`` de web.py.

    entités : clé -> (composant, nom, attributs de discovery supplémentaires).
    """
    tomorrow = today + timedelta(days=1)
    by_day = {(i["service"], i["jour"]) for i in inscriptions}
    month = {s["service_value"]: s for s in current_month.by_service}
    euro = {"unit_of_measurement": "€", "device_class": "monetary"}

    entities: dict[str, tuple[str, str, dict]] = {}
    state: dict[str, object] = {}
    for svc in sorted({Service(i["service"]) for i in inscriptions}, key=lambda s: s.label):
        v, label = svc.value, svc.label
        entities[f"{v}_aujourdhui"] = ("binary_sensor", f"{label} aujourd'hui", {})
        entities[f"{v}_demain"] = ("binary_sensor", f"{label} demain", {})
        entities[f"{v}_mois"] = ("sensor", f"{label} ce mois", {"unit_of_measurement": "j"})
        entities[f"{v}_cout_mois"] = ("sensor", f"{label} coût ce mois", euro)
        state[f"{v}_aujourdhui"] = "ON" if (v, today.isoformat()) in by_day else "OFF"
        state[f"{v}_demain"] = "ON" if (v, tomorrow.isoformat()) in by_day else "OFF"
        state[f"{v}_mois"] = month.get(v, {}).get("count", 0)
        state[f"{v}_cout_mois"] = round(month.get(v, {}).get("total", 0.0), 2)

    entities["cout_mois"] = ("sensor", "Coût ce mois", euro)
    entities["cout_mois_precedent"] = ("sensor", "Coût mois précédent", euro)
    state["cout_mois"] = round(current_month.total, 2)
    state["cout_mois_precedent"] = round(previous_month.total, 2)
    if last_sync:
        entities["derniere_sync"] = ("sensor", "Dernière synchro", {"device_class": "timestamp"})
        # Le conteneur tourne en TZ=Europe/Paris : astimezone() ajoute l'offset local.
        state["derniere_sync"] = datetime.fromisoformat(last_sync).astimezone().isoformat()
    return entities, state


def publish_entities(config: Config, entities: dict, state: dict) -> None:
    """Publie discovery + état (retenus) sur le broker configuré."""
    msgs = []
    for key, (component, name, extra) in entities.items():
        cfg = {
            "name": name,
            "unique_id": f"guichet_unique_{key}",
            "default_entity_id": f"{component}.guichet_{key}",
            "state_topic": STATE_TOPIC,
            "value_template": f"{{{{ value_json.{key} }}}}",
            "device": DEVICE,
            **extra,
        }
        topic = f"homeassistant/{component}/guichet_unique/{key}/config"
        msgs.append({"topic": topic, "payload": json.dumps(cfg), "retain": True, "qos": 1})
    msgs.append({"topic": STATE_TOPIC, "payload": json.dumps(state), "retain": True, "qos": 1})
    auth = (
        {"username": config.mqtt_username, "password": config.mqtt_password}
        if config.mqtt_username else None
    )
    publish.multiple(msgs, hostname=config.mqtt_host, port=config.mqtt_port, auth=auth)
    logger.info("MQTT : %d entités publiées sur %s", len(entities), config.mqtt_host)
