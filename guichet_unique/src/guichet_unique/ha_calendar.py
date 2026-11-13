"""Publication des inscriptions vers une entité ``calendar.*`` de Home Assistant.

Fonctionne avec toute intégration qui sait créer/supprimer des événements
(Google Calendar natif, Local Calendar, …) :

- lecture : ``GET /api/calendars/<entity>`` (REST, renvoie les ``uid``)
- création : service ``calendar.create_event`` (REST)
- suppression : commande WebSocket ``calendar/event/delete`` (pas de service)

HA ne laisse pas choisir l'UID à la création : chaque événement porte donc une
ligne marqueur ``guichet-id: <clé>`` dans sa description. Seuls les événements
marqués sont modifiés ; une mise à jour = suppression + recréation (Google
n'expose pas l'update dans HA).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

import requests
from websockets.sync.client import connect

from .config import Config
from .exporters import DEFAULT_SCHEDULE, PARIS_TZ, display_label
from .models import ExtractionResult, Inscription, Service

logger = logging.getLogger(__name__)

MARKER = "guichet-id: "


def make_uid(ins: Inscription) -> str:
    """Clé stable d'une inscription (inchangée depuis la v1 CalDAV)."""
    raw = ins.raw or {}
    id_unite = raw.get("id_unite", ins.service.value)
    return f"guichet-{ins.jour.isoformat()}-{ins.service.value}-{id_unite}@guichet-unique"


def event_payload(ins: Inscription, *, child_name: str = "", location: str = "") -> dict:
    """Données ``calendar.create_event`` pour une inscription."""
    raw = ins.raw or {}
    defaults = DEFAULT_SCHEDULE.get(ins.service, DEFAULT_SCHEDULE[Service.AUTRE])
    start = datetime.combine(ins.jour, ins.heure_debut or time(*defaults[0]), PARIS_TZ)
    end = datetime.combine(ins.jour, ins.heure_fin or time(*defaults[1]), PARIS_TZ)
    lines = [f"Service : {ins.service.label}", f"Libellé : {display_label(ins, raw)}"]
    if child_name:
        lines.insert(0, f"Enfant : {child_name}")
    if location:
        lines.append(f"Lieu : {location}")
    if ins.statut:
        lines.append(f"Statut : {ins.statut}")
    lines.append(MARKER + make_uid(ins))
    payload = {
        "summary": display_label(ins, raw),
        "description": "\n".join(lines),
        "start_date_time": start.isoformat(),
        "end_date_time": end.isoformat(),
    }
    if location:
        payload["location"] = location
    return payload


def marker_of(description: str | None) -> str | None:
    """Extrait la clé Guichet d'une description, ou None si l'événement n'est pas à nous."""
    for line in (description or "").splitlines():
        if line.startswith(MARKER):
            return line[len(MARKER):].strip()
    return None


def _fingerprint(summary: str, description: str, start: str, end: str) -> tuple:
    # Compare les instants, pas les chaînes : HA/Google peuvent reformater l'offset.
    return (
        summary,
        "\n".join(description.strip().splitlines()),
        datetime.fromisoformat(start).timestamp(),
        datetime.fromisoformat(end).timestamp(),
    )


@dataclass(slots=True)
class PublishStats:
    """Résultat d'une synchronisation vers l'agenda HA."""

    created: int = 0
    updated: int = 0
    deleted: int = 0
    errors: int = 0


def plan_sync(wanted: dict[str, dict], existing: list[dict]) -> tuple[list[dict], list[str]]:
    """Calcule (payloads à créer, uids HA à supprimer).

    ``existing`` = événements renvoyés par ``/api/calendars``. Les événements sans
    marqueur sont ignorés ; les doublons et versions obsolètes sont supprimés.
    """
    to_delete: list[str] = []
    kept: set[str] = set()
    for ev in existing:
        key = marker_of(ev.get("description"))
        if key is None:
            continue
        want = wanted.get(key)
        start, end = ev["start"].get("dateTime"), ev["end"].get("dateTime")
        if want and key not in kept and start and end and _fingerprint(
            ev.get("summary", ""), ev.get("description", ""), start, end,
        ) == _fingerprint(
            want["summary"], want["description"],
            want["start_date_time"], want["end_date_time"],
        ):
            kept.add(key)
        else:
            to_delete.append(ev["uid"])
    to_create = [p for k, p in wanted.items() if k not in kept]
    return to_create, to_delete


class HomeAssistant:
    """Client minimal REST + WebSocket vers HA."""

    def __init__(self, config: Config) -> None:
        if not config.ha_token:
            raise ValueError("Jeton HA manquant (SUPERVISOR_TOKEN ou HA_TOKEN).")
        if not config.ha_calendar_entity:
            raise ValueError("Entité agenda manquante (HA_CALENDAR_ENTITY).")
        self.url = config.ha_url.rstrip("/")
        self.token = config.ha_token
        self.entity = config.ha_calendar_entity
        self.timeout = config.request_timeout
        self.session = requests.Session()
        self.session.headers["Authorization"] = f"Bearer {self.token}"

    def list_events(self, start: date, end: date) -> list[dict]:
        r = self.session.get(
            f"{self.url}/api/calendars/{self.entity}",
            params={"start": start.isoformat(), "end": end.isoformat()},
            timeout=self.timeout,
        )
        r.raise_for_status()
        return r.json()

    def create_event(self, payload: dict) -> None:
        r = self.session.post(
            f"{self.url}/api/services/calendar/create_event",
            json={"entity_id": self.entity, **payload},
            timeout=self.timeout,
        )
        r.raise_for_status()

    def delete_events(self, uids: list[str]) -> int:
        """Supprime via WebSocket ; retourne le nombre d'échecs."""
        if not uids:
            return 0
        ws_url = self.url.replace("http", "ws", 1) + "/websocket"
        errors = 0
        with connect(ws_url, open_timeout=self.timeout) as ws:
            json.loads(ws.recv())  # auth_required
            ws.send(json.dumps({"type": "auth", "access_token": self.token}))
            if json.loads(ws.recv()).get("type") != "auth_ok":
                raise PermissionError("Authentification WebSocket HA refusée.")
            for i, uid in enumerate(uids, start=1):
                ws.send(json.dumps({
                    "id": i, "type": "calendar/event/delete",
                    "entity_id": self.entity, "uid": uid,
                }))
                res = json.loads(ws.recv(timeout=self.timeout))
                if not res.get("success"):
                    logger.warning("Échec suppression %s : %s", uid, res.get("error"))
                    errors += 1
        return errors


def publish_to_ha(result: ExtractionResult, config: Config) -> PublishStats:
    """Synchronise les inscriptions vers l'entité agenda HA configurée."""
    ha = HomeAssistant(config)
    child = result.enfant.prenom
    wanted = {
        make_uid(i): event_payload(i, child_name=child, location=config.default_location)
        for i in result.enfant.inscriptions
    }
    # shortcut: fenêtre fixe ±400 j autour d'aujourd'hui, orphelins plus anciens ignorés.
    today = date.today()
    existing = ha.list_events(today - timedelta(days=400), today + timedelta(days=400))
    to_create, to_delete = plan_sync(wanted, existing)
    logger.info(
        "Agenda %s : %d à créer, %d à supprimer", ha.entity, len(to_create), len(to_delete),
    )

    stats = PublishStats()
    delete_errors = ha.delete_events(to_delete)
    stats.errors += delete_errors
    deleted_keys = {marker_of(e.get("description")) for e in existing if e.get("uid") in to_delete}
    for payload in to_create:
        try:
            ha.create_event(payload)
        except requests.RequestException as exc:
            logger.warning("Échec création %s : %s", payload["summary"], exc)
            stats.errors += 1
            continue
        if marker_of(payload["description"]) in deleted_keys:
            stats.updated += 1
        else:
            stats.created += 1
    stats.deleted = max(0, len(to_delete) - delete_errors - stats.updated)
    logger.info(
        "Agenda HA : +%d, ~%d, -%d, %d erreur(s)",
        stats.created, stats.updated, stats.deleted, stats.errors,
    )
    return stats
