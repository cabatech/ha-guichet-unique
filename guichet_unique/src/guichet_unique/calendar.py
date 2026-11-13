"""Parse les calendriers JSON de réservation pour en extraire les jours concrets.

Structure d'un calendrier (cf. ``/DemandeEnfance/NouvelleDemandeReservationGetCalendrier``) :

.. code-block:: json

    {
      "datDebutAffichageInscr": "/Date(1788213600000)/",
      "datFinAffichageInscr": "/Date(1814479200000)/",
      ...
      "listeUnitesInscr": [
        {"idUnite": 1, "libUnite": "Repas", "nbMaxConso": 1, ...}
      ],
      "listeSemainesAffichees": [
        {"numSemaine": 36, "listeJoursAffiches": [
            {"idJour": 20260901, "listeUnitesJour": [
                {"idUnite": 1, "nbConsoBase": 1, "isActif": true, ...}
            ], ...},
            ...
        ]},
        ...
      ]
    }

Un jour est considéré comme **réservé** pour une unité donnée si ``nbConsoBase > 0``.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any
from zoneinfo import ZoneInfo

from .models import Inscription, Service

logger = logging.getLogger(__name__)

PARIS_TZ = ZoneInfo("Europe/Paris")


# Heuristique de mapping libellé d'unité → Service
# Ordre important : le premier match gagne.
_UNIT_KEYWORDS: tuple[tuple[Service, tuple[str, ...]], ...] = (
    (Service.CANTINE, ("repas", "restauration", "cantine")),
    (Service.PERISCOLAIRE_MATIN, ("matin",)),
    (Service.ESPACE_DETENTE, ("espace détente", "espace detente")),
    (Service.PERISCOLAIRE_SOIR, ("soir", "étude", "etude")),
    (Service.ACCUEIL_LOISIRS, ("journée", "al ", "demi-journée", "1/2 journée")),
)


def _map_unit_to_service(unit_libelle: str) -> Service:
    """Mappe un libellé d'unité vers notre enum ``Service``."""
    label = unit_libelle.lower().strip()
    for service, keywords in _UNIT_KEYWORDS:
        if any(kw in label for kw in keywords):
            # Si "matin" est dans le libellé → matin
            if "matin" in label:
                return Service.PERISCOLAIRE_MATIN
            # Si "soir" ou "étude" → périscolaire soir
            if any(kw in label for kw in ("soir", "étude", "etude")):
                return Service.PERISCOLAIRE_SOIR
            return service
    return Service.AUTRE


@dataclass(slots=True)
class CalendarMetadata:
    """Métadonnées d'un calendrier."""

    service_label: str
    ecole: str
    debut_periode: date | None
    fin_periode: date | None
    debut_modifications: date | None
    fin_modifications: date | None


def _parse_fr_date(value: str) -> date | None:
    """Parse une date dans l'un des formats du portail :

    - ``JJ/MM/AAAA`` (français)
    - ``/Date(<timestamp_ms>)/`` (Microsoft JSON date, en UTC, à convertir en heure de Paris)
    - ``AAAAMMJJ`` (entier compact)
    """
    if not value:
        return None
    s = str(value).strip()
    # Format JJ/MM/AAAA
    m = re.match(r"^(\d{2})/(\d{2})/(\d{4})$", s)
    if m:
        try:
            return date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
        except ValueError:
            return None
    # Format Microsoft /Date(ms)/  (UTC → on convertit en Europe/Paris)
    m = re.match(r"^/Date\((-?\d+)([+-]\d+)?\)/$", s)
    if m:
        try:
            return (
                datetime.fromtimestamp(int(m.group(1)) / 1000, tz=UTC)
                .astimezone(PARIS_TZ)
                .date()
            )
        except (ValueError, OSError):
            return None
    # Format AAAAMMJJ
    m = re.match(r"^(\d{4})(\d{2})(\d{2})$", s)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None
    return None


def _parse_compact_date(value: int | str) -> date | None:
    """Parse une date au format AAAAMMJJ (entier ou chaîne)."""
    s = str(value)
    if len(s) != 8:
        return None
    try:
        return date(int(s[:4]), int(s[4:6]), int(s[6:8]))
    except ValueError:
        return None


def parse_metadata(
    calendar: dict[str, Any],
    *,
    service_label: str = "",
    ecole: str = "",
) -> CalendarMetadata:
    """Extrait les métadonnées d'un calendrier JSON."""
    return CalendarMetadata(
        service_label=service_label,
        ecole=ecole,
        debut_periode=_parse_fr_date(calendar.get("datDebutAffichageInscr", "") or ""),
        fin_periode=_parse_fr_date(calendar.get("datFinAffichageInscr", "") or ""),
        debut_modifications=_parse_fr_date(calendar.get("datDebutModificationsInscr", "") or ""),
        fin_modifications=_parse_fr_date(calendar.get("datFinModificationsInscr", "") or ""),
    )


def extract_inscriptions(
    calendar: dict[str, Any],
    *,
    service_label: str = "",
    ecole: str = "",
) -> list[Inscription]:
    """Extrait la liste des inscriptions jour-par-jour.

    Une inscription = un couple (date, service) pour lequel ``nbConsoBase > 0``.
    On ignore :
      - les jours fériés / fermés (``isFermeOuFerie``) ;
      - les jours hors délai de modification (mais on les conserve comme "hors délai") ;
      - les jours en liste d'attente (``isListeAttente``).

    Le service est déduit du ``idUnite`` grâce à ``listeUnitesInscr``.
    """
    unites_by_id: dict[int, dict[str, Any]] = {
        u["idUnite"]: u for u in calendar.get("listeUnitesInscr", [])
    }
    inscriptions: list[Inscription] = []

    nb_jours_total = 0
    nb_jours_feries = 0
    nb_jours_unite_manquante = 0

    for week in calendar.get("listeSemainesAffichees", []):
        for day in week.get("listeJoursAffiches", []):
            nb_jours_total += 1
            jour = _parse_compact_date(day.get("idJour", 0))
            if jour is None:
                continue

            if day.get("isFermeOuFerie"):
                nb_jours_feries += 1
                continue

            for unit_jour in day.get("listeUnitesJour", []) or []:
                nb_conso = unit_jour.get("nbConsoBase") or 0
                if nb_conso <= 0:
                    continue

                id_unite = unit_jour.get("idUnite")
                unite = unites_by_id.get(id_unite)
                if unite is None:
                    nb_jours_unite_manquante += 1
                    logger.debug(
                        "Unité inconnue id=%s sur %s (jour ignoré)", id_unite, jour
                    )
                    continue

                libelle_unite = unite.get("libUnite", "")
                service = _map_unit_to_service(libelle_unite)

                statut: str | None = None
                notifications = unit_jour.get("listeNotifications") or []
                if "Hors délai" in notifications:
                    statut = "Hors délai"
                elif unit_jour.get("isConsoAttente"):
                    statut = "En attente"
                elif unit_jour.get("isListeAttente"):
                    statut = "Liste d'attente"

                inscriptions.append(
                    Inscription(
                        service=service,
                        jour=jour,
                        statut=statut,
                        raw={
                            "id_unite": id_unite,
                            "lib_unite": libelle_unite,
                            "nb_conso": nb_conso,
                            "notifications": notifications,
                            "service_label": service_label,
                            "ecole": ecole,
                        },
                    )
                )

    logger.info(
        "Calendrier '%s' (%s) : %d jours scannés, %d fériés, %d sans unité connue, "
        "%d inscriptions extraites",
        service_label,
        ecole or "n/a",
        nb_jours_total,
        nb_jours_feries,
        nb_jours_unite_manquante,
        len(inscriptions),
    )

    # Tri chronologique puis par service
    inscriptions.sort(key=lambda i: (i.jour, i.service.value))
    return inscriptions
