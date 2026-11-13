"""Calcul du diff entre l'état précédent et les inscriptions courantes.

Trois types de changements :

- **added** : nouvelle réservation (clé inconnue dans le state)
- **removed** : réservation supprimée sur le portail (clé absente)
- **modified** : réservation présente mais avec une fingerprint différente
  (ex : statut "Hors délai" → vide, changement de service, etc.)
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date

from .models import Inscription
from .state import State, StoredEvent, _event_key

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class EventChange:
    """Description d'un changement sur un événement."""

    uid: str
    key: str
    change_type: str  # "added" | "removed" | "modified"
    jour: date
    current: StoredEvent | None = None  # None pour "removed"
    previous: StoredEvent | None = None  # None pour "added"


@dataclass(slots=True)
class Diff:
    """Résultat d'un diff."""

    added: list[EventChange] = field(default_factory=list)
    removed: list[EventChange] = field(default_factory=list)
    modified: list[EventChange] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.added) + len(self.removed) + len(self.modified)

    @property
    def is_empty(self) -> bool:
        return self.total == 0

    def summary(self) -> str:
        return (
            f"+{len(self.added)} ajout(s), "
            f"-{len(self.removed)} suppression(s), "
            f"~{len(self.modified)} modification(s)"
        )


def compute_diff(
    current: Iterable[Inscription],
    previous: State,
) -> Diff:
    """Calcule le diff entre les inscriptions courantes et l'état précédent."""
    current_keys: dict[str, StoredEvent] = {}
    for ins in current:
        raw = ins.raw or {}
        key = _event_key(ins.jour.isoformat(), raw.get("id_unite"))
        stored = StoredEvent.from_inscription(ins)
        current_keys[key] = stored

    diff = Diff()

    # Détection des ajouts et modifications
    for key, stored_now in current_keys.items():
        prev = previous.events.get(key)
        if prev is None:
            diff.added.append(
                EventChange(
                    uid=stored_now.uid,
                    key=key,
                    change_type="added",
                    jour=date.fromisoformat(key.split(":")[0]),
                    current=stored_now,
                )
            )
        elif prev.fingerprint != stored_now.fingerprint:
            diff.modified.append(
                EventChange(
                    uid=stored_now.uid,
                    key=key,
                    change_type="modified",
                    jour=date.fromisoformat(key.split(":")[0]),
                    current=stored_now,
                    previous=prev,
                )
            )

    # Détection des suppressions
    for key, prev_stored in previous.events.items():
        if key not in current_keys:
            try:
                d = date.fromisoformat(key.split(":")[0])
            except (ValueError, IndexError):
                continue
            diff.removed.append(
                EventChange(
                    uid=prev_stored.uid,
                    key=key,
                    change_type="removed",
                    jour=d,
                    previous=prev_stored,
                )
            )

    logger.info(
        "Diff calculé : %d events courants, %d précédents, %s",
        len(current_keys),
        len(previous.events),
        diff.summary(),
    )
    return diff


def make_removed_uid(removed: EventChange) -> str:
    """UID à utiliser pour un événement supprimé (doit être stable entre runs)."""
    return removed.uid
