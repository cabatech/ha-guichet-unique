"""Modèle de données pour les inscriptions périscolaire/cantine."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date, datetime, time
from enum import StrEnum
from pathlib import Path
from typing import Any


class Service(StrEnum):
    """Type de service."""

    CANTINE = "cantine"
    PERISCOLAIRE_MATIN = "periscolaire_matin"
    PERISCOLAIRE_SOIR = "periscolaire_soir"
    ESPACE_DETENTE = "espace_detente"
    PERISCOLAIRE = "periscolaire"  # générique si matin/soir non distingué
    ACCUEIL_LOISIRS = "accueil_loisirs"
    AUTRE = "autre"

    @property
    def label(self) -> str:
        labels = {
            self.CANTINE: "Cantine",
            self.PERISCOLAIRE_MATIN: "Périscolaire matin",
            self.PERISCOLAIRE_SOIR: "Périscolaire soir",
            self.ESPACE_DETENTE: "Espace détente",
            self.PERISCOLAIRE: "Périscolaire",
            self.ACCUEIL_LOISIRS: "Accueil de loisirs",
            self.AUTRE: "Autre",
        }
        return labels[self]


@dataclass(slots=True)
class Inscription:
    """Une inscription unitaire à un service."""

    service: Service
    jour: date
    heure_debut: time | None = None
    heure_fin: time | None = None
    statut: str | None = None
    prix: float | None = None
    commentaire: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["service"] = self.service.value
        d["jour"] = self.jour.isoformat()
        if self.heure_debut:
            d["heure_debut"] = self.heure_debut.isoformat()
        if self.heure_fin:
            d["heure_fin"] = self.heure_fin.isoformat()
        return d


def display_label(ins: Inscription, raw: dict[str, Any] | None = None) -> str:
    """Label affichable (SUMMARY iCal + ligne « Libellé »).

    Le portail expose l'unité cantine sous le nom « Repas » (Meal) — on préfère
    afficher « Cantine » pour la lisibilité côté calendrier / HA.
    """
    raw = raw if raw is not None else (ins.raw or {})
    if ins.service == Service.CANTINE:
        return Service.CANTINE.label
    return raw.get("lib_unite") or ins.service.label


@dataclass(slots=True)
class InscriptionLink:
    """Lien vers une réservation spécifique (cantine, périscolaire, etc.)."""

    id_ins: int
    id_lie: int
    id_clg: int
    service_label: str
    ecole: str
    periode: str
    debut_periode: date | None = None
    fin_periode: date | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id_ins": self.id_ins,
            "id_lie": self.id_lie,
            "id_clg": self.id_clg,
            "service_label": self.service_label,
            "ecole": self.ecole,
            "periode": self.periode,
            "debut_periode": self.debut_periode.isoformat() if self.debut_periode else None,
            "fin_periode": self.fin_periode.isoformat() if self.fin_periode else None,
        }


@dataclass(slots=True)
class Enfant:
    """Fiche d'un enfant."""

    id: int
    nom: str = ""
    prenom: str = ""
    date_naissance: date | None = None
    inscriptions: list[Inscription] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "nom": self.nom,
            "prenom": self.prenom,
            "date_naissance": (
                self.date_naissance.isoformat() if self.date_naissance else None
            ),
            "inscriptions": [i.to_dict() for i in self.inscriptions],
        }


@dataclass(slots=True)
class ExtractionResult:
    """Résultat complet d'une extraction."""

    extracted_at: datetime
    enfant: Enfant
    source_url: str
    raw_payloads: list[Path] = field(default_factory=list)
    links: list[InscriptionLink] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "extracted_at": self.extracted_at.isoformat(),
            "source_url": self.source_url,
            "raw_payloads": [str(p) for p in self.raw_payloads],
            "links": [lk.to_dict() for lk in self.links],
            "enfant": self.enfant.to_dict(),
        }
