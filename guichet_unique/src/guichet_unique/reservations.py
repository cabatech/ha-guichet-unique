"""Extraction des liens de réservation depuis la fiche personne.

La page ``DetailPersonne`` liste les services auxquels l'enfant est inscrit,
chacun avec un lien ``/DemandeEnfance/NouvelleDemandeReservation/{pers}/{ins}/{lie}/{clg}``.
Les segments de l'URL sont nécessaires pour interroger le calendrier JSON
spécifique à chaque service.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from html import unescape

from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)


@dataclass(slots=True, frozen=True)
class ReservationLink:
    """Lien vers une page de réservation."""

    id_ins: int  # identifiant de l'inscription/activité
    id_lie: int  # identifiant du lieu (école)
    id_clg: int  # identifiant du regroupement (classe/groupe)
    label: str  # libellé affiché dans le tableau (ex: "Restauration Scolaire")
    ecole: str  # complément (ex: "ELEMENTAIRE THOMAS PESQUET")
    periode: str  # ex: "du 01/09/2026 au 02/07/2027"


_RESERVATION_URL_RE = re.compile(
    r"/DemandeEnfance/NouvelleDemandeReservation/"
    r"(?P<person>\d+)/(?P<ins>\d+)/(?P<lie>\d+)/(?P<clg>\d+)"
)


def _detect_service_from_label(label: str) -> str:
    """Devine le type de service à partir du libellé pour le log."""
    label = label.lower()
    if "restauration" in label or "cantine" in label or "repas" in label:
        return "cantine"
    if "périscolaire" in label or "periscolaire" in label or "garderie" in label:
        return "periscolaire"
    if "loisirs" in label or "alsh" in label or "mercredi" in label:
        return "accueil_loisirs"
    return "autre"


def extract_reservation_links(html: str, person_id: int) -> list[ReservationLink]:
    """Extrait la liste des services de réservation depuis la page DetailPersonne."""
    soup = BeautifulSoup(html, "lxml")
    links: list[ReservationLink] = []

    # Trouve toutes les lignes du tableau d'inscription
    table = soup.find(id="tblDalleDetail_Reservation")
    if not table:
        # Fallback : cherche tous les liens correspondants, partout
        for a in soup.find_all("a", href=True):
            m = _RESERVATION_URL_RE.search(a["href"])
            if m and int(m.group("person")) == person_id:
                _try_add_link(a, links, "", "", "")
        return links

    for tr in table.find_all("tr"):
        a = tr.find("a", href=_RESERVATION_URL_RE)
        if not a:
            continue
        m = _RESERVATION_URL_RE.search(a["href"])
        if not m:
            continue

        cells_text = [
            unescape(td.get_text(" ", strip=True)) for td in tr.find_all(["td", "th"])
        ]
        # Structure typique : ["", "Accueil de loisirs Mercredis", "ECOLE", "du XX au YY", ""]
        label = next(
            (
                c
                for c in cells_text
                if c
                and "Ecole" not in c
                and "élément" not in c.lower()
                and "date" not in c.lower()
            ),
            "",
        )
        ecole = next(
            (c for c in cells_text if "ECOLE" in c.upper() or "ELEMENT" in c.upper()),
            "",
        )
        periode = next((c for c in cells_text if "/" in c and "du " in c), "")

        links.append(
            ReservationLink(
                id_ins=int(m.group("ins")),
                id_lie=int(m.group("lie")),
                id_clg=int(m.group("clg")),
                label=label,
                ecole=ecole,
                periode=periode,
            )
        )

    logger.info(
        "Détecté %d service(s) de réservation : %s",
        len(links),
        ", ".join(
            f"{lk.label} ({_detect_service_from_label(lk.label)})" for lk in links
        ),
    )
    return links


_PERSON_URL_RE = re.compile(r"idDynamic=(?P<id>\d+)")


def extract_person_name(html: str, person_id: int) -> str:
    """Extrait le nom affiché pour la personne ``person_id``.

    Le portail expose un menu de sélection rapide où chaque personne apparaît
    comme une icône avec un ``alt`` et un ``title`` portant son nom/prénom.
    """
    soup = BeautifulSoup(html, "lxml")
    for a in soup.find_all("a", href=True):
        m = _PERSON_URL_RE.search(a["href"])
        if not m or int(m.group("id")) != person_id:
            continue
        img = a.find("img")
        if img:
            name = img.get("alt") or img.get("title") or ""
            name = name.strip()
            if name:
                return name
    return ""


def _try_add_link(a, links, label, ecole, periode) -> None:
    """Helper pour le mode fallback."""
    href = a.get("href", "")
    m = _RESERVATION_URL_RE.search(href)
    if not m:
        return
    tr = a.find_parent("tr")
    if tr:
        cells = [td.get_text(" ", strip=True) for td in tr.find_all(["td", "th"])]
        label = next(
            (
                c
                for c in cells
                if c
                and "/" not in c
                and "ECOLE" not in c.upper()
                and "ELEMENT" not in c.upper()
            ),
            label,
        )
        ecole = next(
            (c for c in cells if "ECOLE" in c.upper() or "ELEMENT" in c.upper()), ecole
        )
        periode = next((c for c in cells if "du " in c and "au" in c), periode)
    links.append(
        ReservationLink(
            id_ins=int(m.group("ins")),
            id_lie=int(m.group("lie")),
            id_clg=int(m.group("clg")),
            label=label,
            ecole=ecole,
            periode=periode,
        )
    )
