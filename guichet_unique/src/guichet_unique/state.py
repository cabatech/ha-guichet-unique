"""Persistance de l'état pour le diff intelligent.

On stocke pour chaque (date, service) une clé stable dérivée des données du
portail. Au prochain run, on compare l'état actuel avec l'état stocké pour
détecter les ajouts, suppressions et modifications.

Format du fichier ``data/state_<person_id>.json`` :

.. code-block:: json

    {
      "version": 1,
      "person_id": 4,
      "last_extracted_at": "2026-08-31T23:40:10",
      "events": {
        "2026-09-07:1": {
          "uid": "2026-09-07-cantine-1@guichet-unique",
          "service": "cantine",
          "service_label": "Cantine",
          "lib_unite": "Repas",
          "ecole": "ELEMENTAIRE THOMAS PESQUET",
          "statut": null,
          "nb_conso": 1,
          "fingerprint": "a1b2c3..."
        },
        ...
      }
    }

La *fingerprint* est un hash court du contenu : si elle change entre deux
runs, on considère l'événement comme **modifié**.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .models import Inscription

logger = logging.getLogger(__name__)


STATE_VERSION = 1


def _event_key(jour_iso: str, id_unite: Any) -> str:
    """Clé stable d'un événement dans l'état."""
    return f"{jour_iso}:{id_unite}"


def _fingerprint(ins: Inscription) -> str:
    """Hash court du contenu utile d'une inscription."""
    raw = ins.raw or {}
    payload = {
        "service": ins.service.value,
        "statut": ins.statut,
        "lib_unite": raw.get("lib_unite"),
        "ecole": raw.get("ecole"),
        "nb_conso": raw.get("nb_conso"),
        "notifications": sorted(raw.get("notifications", []) or []),
    }
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()[:12]


def make_uid(jour_iso: str, ins: Inscription) -> str:
    """Génère un UID stable pour un événement iCal."""
    raw = ins.raw or {}
    id_unite = raw.get("id_unite", ins.service.value)
    return f"{jour_iso}-{ins.service.value}-{id_unite}@guichet-unique"


@dataclass(slots=True)
class StoredEvent:
    """Événement tel que stocké dans l'état."""

    uid: str
    service: str
    service_label: str
    lib_unite: str
    ecole: str
    statut: str | None
    nb_conso: int
    fingerprint: str

    @classmethod
    def from_inscription(cls, ins: Inscription) -> StoredEvent:
        raw = ins.raw or {}
        return cls(
            uid=make_uid(ins.jour.isoformat(), ins),
            service=ins.service.value,
            service_label=ins.service.label,
            lib_unite=raw.get("lib_unite", ""),
            ecole=raw.get("ecole", ""),
            statut=ins.statut,
            nb_conso=raw.get("nb_conso", 1) or 1,
            fingerprint=_fingerprint(ins),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class State:
    """État complet pour une personne."""

    version: int = STATE_VERSION
    person_id: int = 0
    last_extracted_at: str = ""
    events: dict[str, StoredEvent] = field(default_factory=dict)

    @classmethod
    def empty(cls, person_id: int) -> State:
        return cls(person_id=person_id)

    def add_inscription(self, ins: Inscription) -> None:
        key = _event_key(ins.jour.isoformat(), (ins.raw or {}).get("id_unite"))
        self.events[key] = StoredEvent.from_inscription(ins)

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "person_id": self.person_id,
            "last_extracted_at": self.last_extracted_at,
            "events": {k: v.to_dict() for k, v in self.events.items()},
        }


def load_state(path: Path, person_id: int) -> State:
    """Charge l'état depuis ``path``. Retourne un état vide si le fichier n'existe pas."""
    if not path.exists():
        logger.debug("Pas de state existant (%s), démarrage à vide.", path)
        return State.empty(person_id)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("State illisible (%s) : %s — on repart de zéro.", path, exc)
        return State.empty(person_id)

    events_raw = data.get("events", {})
    events = {k: StoredEvent(**v) for k, v in events_raw.items()}
    return State(
        version=data.get("version", STATE_VERSION),
        person_id=data.get("person_id", person_id),
        last_extracted_at=data.get("last_extracted_at", ""),
        events=events,
    )


def save_state(state: State, path: Path) -> None:
    """Persiste l'état sur disque."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        json.dumps(state.to_dict(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    tmp.replace(path)
    logger.debug("State sauvegardé : %s (%d events)", path, len(state.events))


def default_state_path(output_dir: Path, person_id: int) -> Path:
    """Chemin par défaut du state pour une personne."""
    return output_dir / f"state_{person_id}.json"
