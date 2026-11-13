"""Carnet de prix par service, versionné par date.

Chaque tarif est valable sur une plage ``[start_date, end_date]``. Lors d'un
changement de prix, on ferme l'ancien (end_date = veille de la nouvelle
entrée) et on crée la nouvelle entrée sans date de fin.

Persistance : ``output_dir / prices.json`` (un fichier plat lisible / versionable).
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import date
from pathlib import Path

from .models import Service

logger = logging.getLogger(__name__)


DEFAULT_PRICES_PATH = "prices.json"


@dataclass(slots=True)
class PriceEntry:
    """Un tarif applicable entre ``start_date`` (inclus) et ``end_date`` (inclus).

    ``end_date`` vaut ``None`` tant que le tarif est en cours. Quand un
    nouveau tarif arrive pour le même service, on ``close_entry`` sur
    l'ancien (end_date = start_date nouveau − 1) avant d'ajouter le nouveau.
    """

    service: Service
    amount_eur: float
    start_date: date
    end_date: date | None = None
    comment: str | None = None
    id: int = 0  # clé numérique unique (timestamp epoch ms)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["service"] = self.service.value
        d["start_date"] = self.start_date.isoformat()
        d["end_date"] = self.end_date.isoformat() if self.end_date else None
        return d

    def contains(self, jour: date) -> bool:
        if jour < self.start_date:
            return False
        if self.end_date and jour > self.end_date:
            return False
        return True


class PriceBook:
    """Liste d'entrées triées par service puis date de début."""

    def __init__(self, entries: list[PriceEntry] | None = None) -> None:
        self.entries: list[PriceEntry] = sorted(
            entries or [], key=lambda e: (e.service.value, e.start_date)
        )
        self._next_id = (max((e.id for e in self.entries), default=0) + 1)

    # -- sérialisation --------------------------------------------------------

    def to_dict_list(self) -> list[dict]:
        return [e.to_dict() for e in self.entries]

    @classmethod
    def from_dict_list(cls, payload: list[dict]) -> "PriceBook":
        entries: list[PriceEntry] = []
        for raw in payload:
            entry = PriceEntry(
                id=raw.get("id", 0),
                service=Service(raw["service"]),
                amount_eur=float(raw["amount_eur"]),
                start_date=date.fromisoformat(raw["start_date"]),
                end_date=date.fromisoformat(raw["end_date"]) if raw.get("end_date") else None,
                comment=raw.get("comment"),
            )
            entries.append(entry)
        return cls(entries)

    # -- opérations -----------------------------------------------------------

    def add_entry(
        self,
        service: Service,
        amount_eur: float,
        start_date: date,
        comment: str | None = None,
    ) -> PriceEntry:
        """Ajoute un nouveau tarif pour ``service``.

        Ferme automatiquement l'entrée ouverte précédente (end_date = veille)
        pour ce même service, s'il y en a une.
        """
        if amount_eur < 0:
            raise ValueError("Le tarif ne peut pas être négatif.")

        # Ferme l'entrée ouverte précédente pour ce service
        for entry in reversed(self.entries):
            if entry.service == service and entry.end_date is None:
                # Calcule la veille de la nouvelle entrée
                yesterday = start_date.replace()  # simple copier
                from datetime import timedelta

                yesterday = start_date - timedelta(days=1)
                entry.end_date = yesterday
                break

        new_entry = PriceEntry(
            id=self._next_id,
            service=service,
            amount_eur=round(amount_eur, 2),
            start_date=start_date,
            comment=comment,
        )
        self._next_id += 1
        self.entries.append(new_entry)
        self._sort()
        return new_entry

    def remove_entry(self, entry_id: int) -> bool:
        """Supprime une entrée par id (uniquement si ce n'est pas la dernière pour ce service).

        La logique métier refuse la suppression de l'entrée ouverte d'un service
        : il faut d'abord en ajouter une nouvelle ou laisser le tarif vide.
        """
        for i, e in enumerate(self.entries):
            if e.id == entry_id:
                # Refuse de supprimer une entrée encore active
                if e.end_date is None:
                    # Vérifie que ce n'est pas la seule entrée du service
                    others = [x for x in self.entries if x.service == e.service and x.id != entry_id]
                    if others:
                        # La dernière entrée du carnet sera ouverte par construction : refus
                        raise ValueError(
                            "Impossible de supprimer le tarif en cours. "
                            "Définissez un nouveau tarif ou laissez-le expirer."
                        )
                # Si on supprime et qu'une entrée précédente n'a pas de date de fin,
                # c'est désormais cette nouvelle qui doit être ouverte.
                del self.entries[i]
                self._ensure_open_entry_open()
                self._sort()
                return True
        return False

    def _ensure_open_entry_open(self) -> None:
        """Garantit qu'au plus une entrée par service est ouverte (end_date=None)."""
        per_service_open: dict[Service, list[int]] = {}
        for i, e in enumerate(self.entries):
            if e.end_date is None:
                per_service_open.setdefault(e.service, []).append(i)
        for indices in per_service_open.values():
            # Garde la plus récente ouverte, ferme les autres
            for idx in indices[:-1]:
                # À ce stade (avant une nouvelle entrée), on s'autorise à fermer
                # s'il reste d'autres entrées
                if self.entries[idx].end_date is None and len(indices) > 1:
                    self.entries[idx].end_date = self.entries[indices[-1]].start_date.replace()

    def _sort(self) -> None:
        self.entries.sort(key=lambda e: (e.service.value, e.start_date, e.id))

    # -- lookup ---------------------------------------------------------------

    def for_service(self, service: Service) -> list[PriceEntry]:
        return [e for e in self.entries if e.service == service]

    def price_for(self, service: Service, jour: date) -> float | None:
        """Tarif applicable pour (service, jour). Renvoie None si aucun."""
        for entry in self.entries:
            if entry.service == service and entry.contains(jour):
                return entry.amount_eur
        return None

    def current(self, service: Service) -> PriceEntry | None:
        """Tarif en cours (end_date=None) pour un service."""
        for entry in reversed(self.entries):
            if entry.service == service and entry.end_date is None:
                return entry
        return None

    # -- E/S ------------------------------------------------------------------

    def save(self, path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(self.to_dict_list(), ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
        logger.info("Carnet de prix écrit : %s (%d entrées)", path, len(self.entries))
        return path

    @classmethod
    def load(cls, path: Path) -> "PriceBook":
        if not path.exists():
            return cls([])
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"prices.json corrompu : {exc}") from exc
        return cls.from_dict_list(payload)


def default_prices_path(output_dir: Path) -> Path:
    return Path(output_dir) / DEFAULT_PRICES_PATH
