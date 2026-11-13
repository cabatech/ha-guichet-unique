"""Client HTTP pour le portail espace-citoyens.net.

Le portail expose deux familles d'endroits utiles :

1. **Pages HTML** rendues côté serveur (accueil, fiche personne, réservation)
   qui nécessitent le token CSRF et gèrent la session via cookie.

2. **Endpoints AJAX/JSON** pour charger dynamiquement les calendriers :

   - ``GET /FichePersonne/DetailPersonneGetCalendrier?idDynamic={id}``
     → calendrier global de la personne (inscriptions, événements).
   - ``GET /DemandeEnfance/NouvelleDemandeReservationGetCalendrier
            ?idPer=&idIns=&idLie=&idClg=``
     → calendrier jour-par-jour d'une réservation spécifique
       (cantine = unité "Repas", périscolaire = "Garderie matin/soir", etc.).
"""

from __future__ import annotations

import logging
import re
from typing import Any
from urllib.parse import urljoin

import requests
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from .config import Config

logger = logging.getLogger(__name__)


class AuthenticationError(RuntimeError):
    """Échec d'authentification."""


class HTTPError(RuntimeError):
    """Erreur HTTP récupérable."""


class EspaceCitoyensClient:
    """Client HTTP session-aware pour le portail."""

    LOGIN_PATH = "/Home/Logon"
    DETAIL_PATH_TEMPLATE = "/FichePersonne/DetailPersonne?idDynamic={person_id}"
    DETAIL_CALENDAR_PATH = "/FichePersonne/DetailPersonneGetCalendrier"
    RESERVATION_PATH_TEMPLATE = (
        "/DemandeEnfance/NouvelleDemandeReservation"
        "/{person_id}/{id_ins}/{id_lie}/{id_clg}"
    )
    RESERVATION_CALENDAR_PATH = "/DemandeEnfance/NouvelleDemandeReservationGetCalendrier"

    def __init__(self, config: Config, *, session: requests.Session | None = None):
        self.config = config
        self.session = session or requests.Session()
        self.session.headers.update(
            {
                "User-Agent": config.user_agent,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.5",
            }
        )

    # ---------- helpers --------------------------------------------------

    def _url(self, path: str) -> str:
        """Construit une URL absolue à partir d'un chemin.

        Un chemin ``"/"`` renvoie l'URL de base (sans rien derrière).
        Un chemin ``"/foo"`` ou ``"foo"`` est concaténé à l'URL de base.
        """
        if not path or path == "/":
            return self.config.base_url.rstrip("/") + "/"
        return urljoin(self.config.base_url + "/", path.lstrip("/"))

    @staticmethod
    def _extract_csrf(html: str) -> str:
        match = re.search(r'__RequestVerificationToken[^>]+value="([^"]+)"', html)
        if not match:
            raise AuthenticationError(
                "Jeton CSRF introuvable. La page a peut-être changé."
            )
        return match.group(1)

    def _get(self, path: str, **kwargs) -> requests.Response:
        return self.session.get(
            self._url(path),
            timeout=self.config.request_timeout,
            **kwargs,
        )

    def _post(self, path: str, **kwargs) -> requests.Response:
        return self.session.post(
            self._url(path),
            timeout=self.config.request_timeout,
            **kwargs,
        )

    # ---------- login ----------------------------------------------------

    def login(self) -> None:
        """Authentifie le client. Lève AuthenticationError en cas d'échec.

        Le serveur renvoie une redirection 302 vers ``CompteCitoyen`` quand
        l'authentification réussit, et la page de login (HTML, 200) sinon. La
        méthode force le type de réponse JSON via ``Accept`` pour stabiliser le
        comportement.
        """
        logger.info("Récupération de la page de login pour le token CSRF…")
        home = self._get("/")
        home.raise_for_status()
        token = self._extract_csrf(home.text)

        payload = {
            "__RequestVerificationToken": token,
            "username": self.config.username,
            "password": self.config.password,
        }

        # POST AJAX — le serveur renvoie 302 vers /CompteCitoyen en cas de succès.
        logger.info("Tentative d'authentification…")
        resp = self._post(
            self.LOGIN_PATH,
            data=payload,
            headers={
                "X-Requested-With": "XMLHttpRequest",
                "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                "Accept": "application/json, text/javascript, */*; q=0.01",
            },
            allow_redirects=True,
        )
        resp.raise_for_status()

        # Si la réponse contient le formulaire de login, l'auth a échoué.
        if 'id="username"' in resp.text and 'name="password"' in resp.text:
            if "incorrect" in resp.text.lower() or "nok" in resp.text.lower():
                raise AuthenticationError(
                    "Identifiants invalides ou compte non activé."
                )
            raise AuthenticationError(
                "Authentification échouée : le serveur a renvoyé la page de login."
            )

        # Si on est sur /Home/Logon, l'auth a échoué.
        if "/Home/Logon" in resp.url:
            raise AuthenticationError(
                "Authentification échouée (URL inchangée après POST)."
            )

        logger.info("Authentification réussie (URL finale : %s)", resp.url)

    # ---------- pages ----------------------------------------------------

    @retry(
        retry=retry_if_exception_type(HTTPError),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=10),
        reraise=True,
    )
    def fetch_detail_personne(self, person_id: int | None = None) -> str:
        """Récupère la page HTML de la fiche personne."""
        pid = person_id if person_id is not None else self.config.person_id
        resp = self._get(self.DETAIL_PATH_TEMPLATE.format(person_id=pid))
        if resp.status_code != 200:
            raise HTTPError(f"HTTP {resp.status_code} sur DetailPersonne")
        # Détection d'une vraie page de login (le bandeau "session expirée"
        # peut apparaître comme overlay sans empêcher le contenu de charger).
        if 'id="username"' in resp.text and 'name="password"' in resp.text:
            raise HTTPError("La page rendue est la page de login (session perdue).")
        # On NE lève PAS d'erreur si la chaîne "Votre session a expiré" est
        # présente : c'est un overlay JS inoffensif (cf. <div id="expirationSession">).
        return resp.text

    def fetch_reservation_page(
        self,
        person_id: int,
        id_ins: int,
        id_lie: int,
        id_clg: int,
    ) -> tuple[str, str]:
        """Visite la page de réservation pour amorcer l'état serveur.

        Retourne ``(id_per, csrf_token)``. L'id_per est différent de l'idDynamic :
        il s'agit de l'identifiant interne de la personne côté serveur de
        réservation. Le CSRF est requis pour les appels AJAX suivants.
        """
        path = self.RESERVATION_PATH_TEMPLATE.format(
            person_id=person_id, id_ins=id_ins, id_lie=id_lie, id_clg=id_clg
        )
        logger.info("GET %s", path)
        resp = self._get(path)
        resp.raise_for_status()

        id_per_m = re.search(r"var\s+idPer\s*=\s*(\d+);", resp.text)
        if not id_per_m:
            raise HTTPError(f"idPer introuvable sur {path}")
        id_per = id_per_m.group(1)

        csrf = self._extract_csrf(resp.text)
        return id_per, csrf

    # ---------- JSON endpoints -------------------------------------------

    def fetch_detail_calendar(self, person_id: int) -> dict[str, Any]:
        """Calendrier global de la personne (inscriptions, événements)."""
        path = f"{self.DETAIL_CALENDAR_PATH}?idDynamic={person_id}"
        logger.info("GET %s", path)
        resp = self._get(
            path,
            headers={"X-Requested-With": "XMLHttpRequest"},
        )
        resp.raise_for_status()
        return resp.json()

    def fetch_reservation_calendar(
        self,
        id_per: str,
        id_ins: int,
        id_lie: int,
        id_clg: int,
        csrf: str,
        referer: str,
    ) -> dict[str, Any]:
        """Calendrier jour-par-jour d'une réservation."""
        path = (
            f"{self.RESERVATION_CALENDAR_PATH}"
            f"?idPer={id_per}&idIns={id_ins}&idLie={id_lie}&idClg={id_clg}"
        )
        logger.info("GET %s", path)
        resp = self._get(
            path,
            headers={
                "X-Requested-With": "XMLHttpRequest",
                "RequestVerificationToken": csrf,
                "Referer": referer,
            },
        )
        if resp.status_code != 200:
            raise HTTPError(
                f"Calendrier réservation HTTP {resp.status_code}: {resp.text[:200]}"
            )
        data = resp.json()
        if data.get("error"):
            raise HTTPError(f"Calendrier réservation en erreur : {data}")
        return data
