"""Export des résultats en JSON, CSV et iCal."""

from __future__ import annotations

import csv
import json
import logging
from datetime import datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from icalendar import (
    Calendar,
    Event,
    Timezone,
    TimezoneDaylight,
    TimezoneStandard,
)

from .diff import Diff, EventChange
from .models import ExtractionResult, Inscription, Service, display_label

logger = logging.getLogger(__name__)


# Fuseau Europe/Paris — utilisé pour tagger tous les datetimes iCal avec
# TZID=Europe/Paris et générer un bloc VTIMEZONE complet (CET/CEST + DST).
PARIS_TZ = ZoneInfo("Europe/Paris")
TZID_PARIS = "Europe/Paris"


def _attach_tz(event, prop_name: str, dt: datetime) -> None:
    """Tagge un datetime timezone-aware avec le paramètre TZID=Europe/Paris."""
    event.add(prop_name, dt)
    event[prop_name].params["TZID"] = TZID_PARIS


def add_paris_vtimezone(cal: Calendar) -> None:
    """Ajoute un bloc VTIMEZONE complet pour Europe/Paris (CET/CEST avec
    règles DST officielles : dernier dimanche d'octobre → dernier dimanche de
    mars). Sans ce bloc, Google Calendar (et autres clients RFC 5545 stricts)
    refusent de comprendre les datetimes avec TZID=Europe/Paris.
    """
    tz = Timezone()
    tz.add("TZID", TZID_PARIS)
    tz.add("X-LIC-LOCATION", TZID_PARIS)

    # STANDARD = CET (UTC+1), actif du dernier DIMANCHE d'octobre au dernier
    # dimanche de mars. (Bug précédent : "-1MO" faisait dériver le calendrier
    # d'une semaine tous les ans. Le passage à l'heure d'hiver européenne se
    # fait strictement sur le dernier dimanche.)
    standard = TimezoneStandard()
    standard.add("DTSTART", datetime(1970, 10, 25, 3, 0, 0))
    standard.add("TZOFFSETFROM", timedelta(hours=2))
    standard.add("TZOFFSETTO", timedelta(hours=1))
    standard.add("TZNAME", "CET")
    standard.add("RRULE", {"freq": "yearly", "bymonth": 10, "byday": ["-1SU"]})

    # DAYLIGHT = CEST (UTC+2), dernier dimanche de mars à 02h00 heure locale.
    daylight = TimezoneDaylight()
    daylight.add("DTSTART", datetime(1970, 3, 29, 2, 0, 0))
    daylight.add("TZOFFSETFROM", timedelta(hours=1))
    daylight.add("TZOFFSETTO", timedelta(hours=2))
    daylight.add("TZNAME", "CEST")
    daylight.add("RRULE", {"freq": "yearly", "bymonth": 3, "byday": ["-1SU"]})

    tz.add_component(standard)
    tz.add_component(daylight)
    cal.add_component(tz)


# ---------- JSON -----------------------------------------------------------


def export_json(result: ExtractionResult, path: Path) -> Path:
    """Sérialise le résultat en JSON (indenté, UTF-8)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(result.to_dict(), ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    logger.info("JSON écrit : %s", path)
    return path


# ---------- CSV ------------------------------------------------------------


CSV_FIELDS = (
    "enfant_id",
    "enfant_nom",
    "enfant_prenom",
    "service",
    "service_label",
    "lib_unite",
    "ecole",
    "jour",
    "nb_conso",
    "statut",
    "notifications",
)


def export_csv(result: ExtractionResult, path: Path) -> Path:
    """Exporte les inscriptions au format CSV (séparateur ';', UTF-8 BOM)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=CSV_FIELDS, delimiter=";")
        writer.writeheader()
        for ins in result.enfant.inscriptions:
            writer.writerow(_inscription_to_row(result, ins))
    logger.info("CSV écrit : %s", path)
    return path


def _inscription_to_row(result: ExtractionResult, ins: Inscription) -> dict[str, str]:
    raw = ins.raw or {}
    return {
        "enfant_id": result.enfant.id,
        "enfant_nom": result.enfant.nom,
        "enfant_prenom": result.enfant.prenom,
        "service": ins.service.value,
        "service_label": ins.service.label,
        "lib_unite": raw.get("lib_unite", ""),
        "ecole": raw.get("ecole", ""),
        "jour": ins.jour.isoformat(),
        "nb_conso": raw.get("nb_conso", 1),
        "statut": ins.statut or "",
        "notifications": ";".join(raw.get("notifications", []) or []),
    }


# ---------- iCal -----------------------------------------------------------


# Horaires par défaut si non précisés sur la page
# Horaires par défaut si non précisés sur le portail.
# `Garderie récréative soir` (PERISCOLAIRE_SOIR) = 16h30 → 17h30 (1 h).
# `Espace détente` (ESPACE_DETENTE) = 17h30 → 18h30 (1 h, prolonge la garderie
# si l'enfant reste jusqu'à 18h30). Les deux unités peuvent se cumuler le
# même jour sur des créneaux consécutifs.
DEFAULT_SCHEDULE: dict[Service, tuple[tuple[int, int], tuple[int, int]]] = {
    Service.CANTINE: ((11, 30), (13, 30)),
    Service.PERISCOLAIRE_MATIN: ((7, 30), (8, 30)),
    Service.PERISCOLAIRE_SOIR: ((16, 30), (17, 30)),
    Service.ESPACE_DETENTE: ((17, 30), (18, 30)),
    Service.PERISCOLAIRE: ((16, 30), (17, 30)),
    Service.ACCUEIL_LOISIRS: ((8, 0), (18, 0)),
    Service.AUTRE: ((0, 0), (23, 59)),
}


def _event_summary(ins: Inscription, raw: dict) -> str:
    """Construit le titre de l'événement."""
    label = display_label(ins, raw)
    if label != ins.service.label:
        return f"{ins.service.label} – {label}"
    return ins.service.label


def _event_description(result: ExtractionResult, ins: Inscription) -> str:
    raw = ins.raw or {}
    desc_lines = [
        f"Enfant : {result.enfant.prenom} {result.enfant.nom}".strip(),
        f"Service : {ins.service.label}",
    ]
    if (label := display_label(ins, raw)):
        desc_lines.append(f"Libellé : {label}")
    if raw.get("ecole"):
        desc_lines.append(f"École : {raw['ecole']}")
    if ins.statut:
        desc_lines.append(f"Statut : {ins.statut}")
    return "\n".join(desc_lines)


def _build_event(
    result: ExtractionResult,
    ins: Inscription,
    *,
    sequence: int = 0,
    location: str = "",
) -> Event:
    """Construit un événement iCal pour une inscription active."""
    raw = ins.raw or {}
    defaults = DEFAULT_SCHEDULE.get(ins.service, DEFAULT_SCHEDULE[Service.AUTRE])
    start = ins.heure_debut or time(*defaults[0])
    end = ins.heure_fin or time(*defaults[1])

    event = Event()
    event.add("summary", _event_summary(ins, raw))
    dtstart = datetime.combine(ins.jour, start).replace(tzinfo=PARIS_TZ)
    dtend = datetime.combine(ins.jour, end).replace(tzinfo=PARIS_TZ)
    _attach_tz(event, "dtstart", dtstart)
    _attach_tz(event, "dtend", dtend)
    event.add("dtstamp", datetime.now(tz=PARIS_TZ))
    uid = raw.get("uid") or (
        f"{ins.jour.isoformat()}-{ins.service.value}"
        f"-{raw.get('id_unite', '')}@guichet-unique"
    )
    event.add("uid", uid)
    event.add("sequence", sequence)
    event.add("last-modified", datetime.now(tz=PARIS_TZ))
    event.add("description", _event_description(result, ins))
    event.add("categories", [ins.service.label])
    if location:
        event.add("location", location)
    return event


def _build_cancelled_event(
    change: EventChange, result: ExtractionResult, *, location: str = "",
) -> Event:
    """Construit un événement avec STATUS:CANCELLED pour signaler une suppression.

    Les clients iCal (Home Assistant, Google Calendar, Apple Calendar, …)
    comprennent ``STATUS:CANCELLED`` et retirent l'événement de l'affichage.
    """
    prev = change.previous
    assert prev is not None
    service = Service(prev.service)
    defaults = DEFAULT_SCHEDULE.get(service, DEFAULT_SCHEDULE[Service.AUTRE])

    event = Event()
    event.add("summary", f"{prev.service_label} (annulé)")
    dtstart = datetime.combine(change.jour, time(*defaults[0])).replace(tzinfo=PARIS_TZ)
    dtend = datetime.combine(change.jour, time(*defaults[1])).replace(tzinfo=PARIS_TZ)
    _attach_tz(event, "dtstart", dtstart)
    _attach_tz(event, "dtend", dtend)
    event.add("dtstamp", datetime.now(tz=PARIS_TZ))
    event.add("uid", change.uid)
    event.add("status", "CANCELLED")
    event.add("sequence", 999)  # séquence élevée pour forcer le retrait
    event.add("last-modified", datetime.now(tz=PARIS_TZ))
    event.add(
        "description",
        f"Réservation annulée sur le portail (précédemment : {prev.lib_unite})",
    )
    if location:
        event.add("location", location)
    return event


def export_ical(
    result: ExtractionResult,
    path: Path,
    *,
    diff: Diff | None = None,
    default_location: str = "",
) -> Path:
    """Exporte en calendrier iCal (.ics).

    Si ``diff`` est fourni, les événements supprimés sont inclus avec
    ``STATUS:CANCELLED`` pour que les clients iCal les retirent de
    l'affichage.

    Les UIDs sont stables (dérivés de la date + service + id_unite) pour
    permettre aux clients de mettre à jour les événements existants au
    lieu d'en créer de nouveaux.

    Le fichier émis contient un bloc ``VTIMEZONE`` complet pour Europe/Paris.
    Tous les ``DTSTART``/``DTEND`` sont taggés ``TZID=Europe/Paris``. Google
    Calendar, Apple Calendar, Thunderbird et CalDAV clients respectent
    correctement le fuseau d'été/hiver.
    """
    path.parent.mkdir(parents=True, exist_ok=True)

    cal = Calendar()
    cal.add("prodid", "-//Guichet Unique//espace-citoyens.net//FR")
    cal.add("version", "2.0")
    cal.add("calscale", "GREGORIAN")
    cal.add("method", "PUBLISH")
    cal.add(
        "x-wr-calname",
        f"Inscriptions {result.enfant.prenom or ''} {result.enfant.nom or ''}".strip(),
    )
    add_paris_vtimezone(cal)

    # Événements actifs
    for ins in result.enfant.inscriptions:
        cal.add_component(_build_event(result, ins, location=default_location))

    # Événements supprimés (STATUS:CANCELLED)
    if diff is not None:
        for change in diff.removed:
            cal.add_component(
                _build_cancelled_event(change, result, location=default_location),
            )

    path.write_bytes(cal.to_ical())
    logger.info("iCal écrit : %s", path)
    return path
