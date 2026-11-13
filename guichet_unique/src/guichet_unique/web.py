"""Interface WebUI FastAPI : status / sync manuel / tableau filtrable / calendrier."""

from __future__ import annotations

import base64
import calendar
import hashlib
import hmac
import ipaddress
import json
import logging
import os
import secrets
import threading
from collections import defaultdict
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from ._version import __version__
from .cli import _load_result_from_json
from .config import Config, ConfigError
from .exporters import DEFAULT_SCHEDULE
from .ha_calendar import publish_to_ha
from .mqtt_entities import compute_states, publish_entities
from .ical_tokens import ICalTokenStore, default_tokens_path
from .models import Service
from .pipeline import run_extraction
from .pricing import PriceBook, default_prices_path

logger = logging.getLogger("guichet.web")

# ----------------------------------------------------------------------------
# Constantes de chemin
# ----------------------------------------------------------------------------

_PKG_DIR = Path(__file__).resolve().parent
_TEMPLATES_DIR = _PKG_DIR / "web" / "templates"
_STATIC_DIR = _PKG_DIR / "web" / "static"

DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 8236


# ----------------------------------------------------------------------------
# État global : Config figée + verrous pour sync manuel
# ----------------------------------------------------------------------------


class _RuntimeState:
    """État mutable partagé par les endpoints (et le template)."""

    def __init__(self, config: Config) -> None:
        self.config = config
        self._lock = threading.Lock()
        self._sync_thread: threading.Thread | None = None
        self.last_sync: dict[str, Any] | None = None
        self.last_error: str | None = None
        # Carnet de prix : chargé à chaque accès depuis le disque (concurrence
        # faible côté UI, pas besoin de cache mémoire).
        self._prices_lock = threading.Lock()
        self.prices_path = default_prices_path(config.output_dir)
        # Magasin de tokens iCal pour l'intégration externe (Google, Apple…).
        self._tokens_lock = threading.Lock()
        self.tokens_path = default_tokens_path(config.output_dir)
        # Référence au scheduler d'auto-sync (posée par lifespan).
        self.scheduler: Any | None = None

    def load_prices(self) -> PriceBook:
        with self._prices_lock:
            return PriceBook.load(self.prices_path)

    def save_prices(self, book: PriceBook) -> None:
        with self._prices_lock:
            book.save(self.prices_path)

    def load_tokens(self) -> ICalTokenStore:
        with self._tokens_lock:
            return ICalTokenStore.load(self.tokens_path)

    def save_tokens(self, store: ICalTokenStore) -> None:
        with self._tokens_lock:
            store.save(self.tokens_path)

    # -- sync manuel ---------------------------------------------------------
    def is_syncing(self) -> bool:
        return self._sync_thread is not None and self._sync_thread.is_alive()

    def start_sync(self, *, also_publish: bool, trigger: str = "manual") -> bool:
        """Lance un sync dans un thread. Retourne False si un sync est déjà en cours."""
        with self._lock:
            if self.is_syncing():
                return False
            self.last_error = None
            self._sync_thread = threading.Thread(
                target=self._run_sync, args=(also_publish, trigger), daemon=True,
                name=f"guichet-{trigger}-sync",
            )
            self._sync_thread.start()
            return True

    def _run_sync(self, also_publish: bool, trigger: str) -> None:
        try:
            result = run_extraction(self.config)
            stats = None
            if also_publish and self.config.ha_calendar_entity:
                stats = publish_to_ha(result, self.config)
            self.publish_entities()
            self.last_sync = {
                "at": datetime.now().isoformat(),
                "ok": True,
                "trigger": trigger,
                "inscriptions": len(result.enfant.inscriptions),
                "person": f"{result.enfant.prenom} {result.enfant.nom}".strip(),
                "publish": stats and {
                    "created": stats.created,
                    "updated": stats.updated,
                    "deleted": stats.deleted,
                    "errors": stats.errors,
                },
            }
        except Exception as exc:  # noqa: BLE001
            logger.exception("Sync %s échoué", trigger)
            self.last_error = f"{type(exc).__name__}: {exc}"
            self.last_sync = {
                "at": datetime.now().isoformat(),
                "ok": False,
                "trigger": trigger,
                "error": self.last_error,
            }

    def publish_entities(self) -> None:
        """Met à jour les entités MQTT (no-op sans broker configuré)."""
        if not self.config.mqtt_host:
            return
        try:
            data, _ = self.load_current_inscriptions()
            today = date.today()
            prev = date(today.year, today.month, 1) - timedelta(days=1)
            entities, values = compute_states(
                (data or {}).get("enfant", {}).get("inscriptions", []),
                _monthly_breakdown(self, today.year, today.month),
                _monthly_breakdown(self, prev.year, prev.month),
                (data or {}).get("extracted_at"),
                today,
            )
            publish_entities(self.config, entities, values)
        except Exception:  # noqa: BLE001 — un broker absent ne doit pas casser la sync
            logger.exception("Publication MQTT échouée")

    # -- chargement local ----------------------------------------------------
    def load_current_inscriptions(self) -> tuple[dict[str, Any] | None, str | None]:
        """Lit le dernier JSON d'inscriptions en local (sans appel réseau)."""
        json_path = self.config.output_dir / f"inscriptions_{self.config.person_id}.json"
        if not json_path.exists():
            return None, f"Aucun JSON trouvé ({json_path.name}). Lancez un sync."
        try:
            data = json.loads(json_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            return None, f"JSON corrompu : {exc}"
        return data, None


# ----------------------------------------------------------------------------
# Modèles de réponse
# ----------------------------------------------------------------------------


class StatusResponse(BaseModel):
    syncing: bool
    last_sync: dict[str, Any] | None
    last_error: str | None
    inscription_count: int
    has_data: bool
    config: dict[str, Any]


# ----------------------------------------------------------------------------
# Fabrique d'application
# ----------------------------------------------------------------------------


def create_app(config: Config | None = None) -> FastAPI:
    """Construit l'application FastAPI. Si `config` est None, charge depuis l'env."""
    if config is None:
        config = Config.from_env()

    state = _RuntimeState(config)

    @asynccontextmanager
    async def _lifespan(_app: FastAPI):
        """Démarre/arrête le scheduler APScheduler pour l'auto-sync."""
        if os.getenv("GUICHET_AUTO_SYNC", "1") != "1":
            logger.info("Auto-sync désactivé (GUICHET_AUTO_SYNC!=1)")
            yield
            return
        try:
            from apscheduler.schedulers.background import BackgroundScheduler
            from apscheduler.triggers.cron import CronTrigger

            cron_expr = os.getenv("GUICHET_CRON", "0 6 * * *")
            scheduler = BackgroundScheduler(timezone="Europe/Paris")

            def _auto_tick() -> None:
                if state.is_syncing():
                    logger.info("Auto-sync ignoré : un sync manuel est déjà en cours.")
                    return
                started = state.start_sync(
                    also_publish=bool(state.config.ha_calendar_entity),
                    trigger="auto",
                )
                if started:
                    logger.info(
                        "Auto-sync lancé (publish=%s)", bool(state.config.ha_calendar_entity),
                    )

            scheduler.add_job(_auto_tick, CronTrigger.from_crontab(cron_expr))
            # Bascule aujourd'hui/demain à minuit + état initial au démarrage.
            scheduler.add_job(state.publish_entities, CronTrigger.from_crontab("1 0 * * *"))
            scheduler.add_job(state.publish_entities)
            scheduler.start()
            state.scheduler = scheduler
            logger.info(
                "Auto-sync scheduler actif : cron=%r (TZ=Europe/Paris)", cron_expr,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Impossible de démarrer le scheduler : %s", exc)
        try:
            yield
        finally:
            sched = getattr(state, "scheduler", None)
            if sched is not None:
                sched.shutdown()
                state.scheduler = None
                logger.info("Auto-sync scheduler arrêté.")

    app = FastAPI(
        title="Guichet Unique — WebUI",
        version=__version__,
        docs_url=None,
        redoc_url=None,
        lifespan=_lifespan,
    )

    # ------------------------------------------------------------------------
    # Authentification HTTP Basic sur les requêtes venant d'Internet
    # ------------------------------------------------------------------------
    # Stratégie :
    # 1. Le flux iCal `/ical/<token>` reste public (token = auth, utilisé par
    #    Google Agenda / Apple / DAVx5 qui ne savent pas présenter un popup
    #    Basic Auth propre).
    # 2. Pour les autres routes, on regarde l'IP source réelle (via
    #    X-Forwarded-For en premier, fallback sur client.host). Si elle est
    #    dans un réseau privé (LAN/NAS), on laisse passer. Sinon, on exige
    #    un couple user/password (HTTP Basic).
    # 3. Les credentials sont lus depuis `GUICHET_BASIC_USER` / `GUICHET_BASIC_PASS`.
    #    S'ils manquent, on génère un couple aléatoire **au démarrage** et on le
    #    logue pour que l'utilisateur puisse le récupérer dans `docker logs`.

    BASIC_USER = os.getenv("GUICHET_BASIC_USER", "")
    BASIC_PASS = os.getenv("GUICHET_BASIC_PASS", "")
    if not (BASIC_USER and BASIC_PASS):
        BASIC_USER = "admin"
        BASIC_PASS = secrets.token_urlsafe(18)
        logger.warning(
            "GUICHET_BASIC_USER/PASS non définis — couple généré pour ce run :\n"
            "    GUICHET_BASIC_USER=%s\n"
            "    GUICHET_BASIC_PASS=%s\n"
            "(persistant dans .env pour qu'il ne change pas à chaque reboot)",
            BASIC_USER, BASIC_PASS,
        )

    # Réseaux strictement "LAN" (restrictif pour éviter les faux positifs).
    # - 127.0.0.0/8 : loopback (healthcheck Docker + accès LAN direct)
    # - 192.168.0.0/16 : LAN domestique classique
    # On exclut volontairement 10.0.0.0/8 (souvent utilisé en CGNAT / sortie
    # NAT / NPM upstream — le XFF vu par FastAPI y ressemble mais ce n'est pas
    # un LAN de confiance). Pour un réseau interne en 10.x, utiliser un Host
    # suffixé (`.local`, `.lan`, etc. — ci-dessous).
    _LAN_NETS = [
        ipaddress.ip_network("127.0.0.0/8"),
        ipaddress.ip_network("192.168.0.0/16"),
        ipaddress.ip_network("::1/128"),
    ]
    # Suffixes de host considérés "LAN direct" (accès conteneur sans reverse proxy).
    # Le hostname public `guichet.example.com` ne matche aucun de ces suffixes
    # → auth requise même si le client est dans le réseau domestique.
    _LAN_HOST_SUFFIXES = (
        ".local", ".lan", ".home", ".internal", ".nas",
    )

    def _real_client_ip(request: Request) -> str:
        """Récupère l'IP réelle derrière un proxy (1er hop de X-Forwarded-For,
        sinon client.host). Empty string si indéterminable.
        """
        xff = request.headers.get("x-forwarded-for")
        if xff:
            return xff.split(",")[0].strip()
        return (request.client.host if request.client else "") or ""

    def _is_lan_ip(ip_str: str) -> bool:
        try:
            ip = ipaddress.ip_address(ip_str)
        except (ValueError, TypeError):
            return False
        return any(ip in net for net in _LAN_NETS)

    def _is_lan_host(request: Request) -> bool:
        """Détecte un accès LAN direct via le Host header (sans passer par NPM).

        Si le client cible directement un Host LAN (IP LAN, .local, .lan, .home,
        .nas, …) sans passer par le DNS public, on bypass l'auth — c'est un
        accès de confiance depuis le réseau domestique.

        IMPORTANT : `guichet.example.com` (= accès via NPM sur Internet) doit
        toujours exiger l'auth, même si la requête vient de chez vous.
        """
        host = request.headers.get("host", "").split(":")[0].lower()
        if not host:
            return False
        # IP LAN
        if _is_lan_ip(host):
            return True
        # Suffixe privé
        return any(host.endswith(sfx) for sfx in _LAN_HOST_SUFFIXES)

    def _check_basic_auth(header_value: str, expected_user: str, expected_pass: str) -> bool:
        if not header_value.startswith("Basic "):
            return False
        try:
            decoded = base64.b64decode(header_value[6:]).decode("utf-8", errors="strict")
        except Exception:  # noqa: BLE001
            return False
        if ":" not in decoded:
            return False
        user, _, pwd = decoded.partition(":")
        return (
            hmac.compare_digest(user, expected_user)
            and hmac.compare_digest(pwd, expected_pass)
        )

    @app.middleware("http")
    async def _basic_auth_middleware(request: Request, call_next):
        # Whitelist iCal (token = auth)
        if request.url.path.startswith("/ical/"):
            return await call_next(request)
        # Ingress HA : déjà authentifié par HA ; le proxy Supervisor a toujours cette IP.
        if request.client and request.client.host == "172.30.32.2":
            return await call_next(request)
        # Whitelist LAN : IP source LAN, OU Host header pointant sur le LAN
        if _is_lan_ip(_real_client_ip(request)) or _is_lan_host(request):
            return await call_next(request)
        # Sinon, exiger Basic Auth
        if not _check_basic_auth(
            request.headers.get("Authorization", ""), BASIC_USER, BASIC_PASS,
        ):
            return Response(
                status_code=401,
                headers={"WWW-Authenticate": 'Basic realm="Guichet Unique"'},
                content="Authentification requise.\n",
                media_type="text/plain",
            )
        return await call_next(request)

    # Templates + statiques
    templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))
    app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")

    # ----------------------------------------------------------------------------
    # Vues HTML
    # ----------------------------------------------------------------------------

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request) -> HTMLResponse:
        data, err = state.load_current_inscriptions()
        nb = len(data["enfant"]["inscriptions"]) if data else 0
        services = sorted({
            ins["service"]
            for ins in (data or {}).get("enfant", {}).get("inscriptions", [])
        })
        summary = _week_summary(state)
        bimonth = _bi_monthly_billing(state)
        prev_year = (
            bimonth["current"].year - 1 if bimonth["current"].month == 1
            else bimonth["current"].year
        )
        prev_month = 12 if bimonth["current"].month == 1 else bimonth["current"].month - 1
        ctx = {
            "request": request,
            "version": __version__,
            "person": {
                "prenom": (data or {}).get("enfant", {}).get("prenom", ""),
                "nom": (data or {}).get("enfant", {}).get("nom", ""),
                "id": state.config.person_id,
            },
            "extracted_at": (data or {}).get("extracted_at"),
            "ha_calendar_entity": state.config.ha_calendar_entity,
            "nb_inscriptions": nb,
            "services": services,
            "is_syncing": state.is_syncing(),
            "last_sync": state.last_sync,
            "last_error": state.last_error,
            "summary": summary,
            "bimonth": bimonth,
            "colors": _service_color_index(),
            "legend": _service_legend(summary),
            "auto_sync_enabled": os.getenv("GUICHET_AUTO_SYNC", "1") == "1",
            "cron_expr": os.getenv("GUICHET_CRON", "0 6 * * *"),
            "now": datetime.now(),
            "prev_month_label": _month_label(prev_year, prev_month),
            "cur_month_label": _month_label(bimonth["current"].year, bimonth["current"].month),
        }
        return templates.TemplateResponse("index.html", ctx)

    @app.get("/inscriptions", response_class=HTMLResponse)
    def inscriptions_table(request: Request) -> HTMLResponse:
        data, err = state.load_current_inscriptions()
        if err:
            return HTMLResponse(f"<p class='error'>{err}</p>", status_code=404)
        inscriptions = data["enfant"]["inscriptions"]
        services = sorted({i["service"] for i in inscriptions})
        book = state.load_prices()
        # Calcul du coût total + prix applicable par ligne
        total = 0.0
        for ins in inscriptions:
            jour = date.fromisoformat(ins["jour"])
            svc = Service(ins["service"])
            price = book.price_for(svc, jour)
            if price is None:
                current = book.current(svc)
                if current:
                    price = current.amount_eur
            ins["applicable_price"] = price
            if price is not None:
                total += price
        ctx = {
            "request": request,
            "inscriptions": inscriptions,
            "services": services,
            "person_label": f"{data['enfant'].get('prenom', '')} {data['enfant'].get('nom', '')}".strip(),
            "total": total,
        }
        return templates.TemplateResponse("table.html", ctx)

    @app.get("/calendrier", response_class=HTMLResponse)
    def calendar_view(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(
            "calendar.html",
            {"request": request, "ics_url": "/calendar.ics"},
        )

    # ----------------------------------------------------------------------------
    # API JSON
    # ----------------------------------------------------------------------------

    @app.get("/api/status", response_model=StatusResponse)
    def api_status() -> StatusResponse:
        data, _ = state.load_current_inscriptions()
        nb = len(data["enfant"]["inscriptions"]) if data else 0
        return StatusResponse(
            syncing=state.is_syncing(),
            last_sync=state.last_sync,
            last_error=state.last_error,
            inscription_count=nb,
            has_data=data is not None,
            config={
                "person_id": state.config.person_id,
                "output_dir": str(state.config.output_dir),
                "ha_calendar_entity": state.config.ha_calendar_entity,
            },
        )

    @app.get("/api/inscriptions")
    def api_inscriptions(
        service: str | None = Query(None, description="Filtrer par service"),
        q: str | None = Query(None, description="Recherche libre (libellé, école, statut)"),
        date_from: str | None = Query(None, description="YYYY-MM-DD"),
        date_to: str | None = Query(None, description="YYYY-MM-DD"),
    ) -> JSONResponse:
        data, err = state.load_current_inscriptions()
        if err:
            return JSONResponse({"error": err}, status_code=404)
        inscriptions = data["enfant"]["inscriptions"]
        if service and service != "all":
            inscriptions = [i for i in inscriptions if i["service"] == service]
        if q:
            needle = q.lower()
            inscriptions = [
                i for i in inscriptions
                if needle in (i.get("statut") or "").lower()
                or needle in json.dumps(i.get("raw") or {}, ensure_ascii=False).lower()
            ]
        if date_from:
            inscriptions = [i for i in inscriptions if i["jour"] >= date_from]
        if date_to:
            inscriptions = [i for i in inscriptions if i["jour"] <= date_to]
        return JSONResponse({"count": len(inscriptions), "items": inscriptions})

    class SyncRequest(BaseModel):
        publish: bool = True

    @app.post("/api/sync")
    def api_sync(body: SyncRequest | None = None) -> JSONResponse:
        body = body or SyncRequest()
        started = state.start_sync(
            also_publish=body.publish and bool(state.config.ha_calendar_entity),
        )
        if not started:
            return JSONResponse(
                {"error": "Un sync est déjà en cours"}, status_code=409,
            )
        return JSONResponse({"started": True, "publish": body.publish})

    # ----------------------------------------------------------------------------
    # Feed iCal (calendrier)
    # ----------------------------------------------------------------------------

    @app.get("/calendar.ics")
    def calendar_ics() -> Response:
        ics_path = state.config.output_dir / f"inscriptions_{state.config.person_id}.ics"
        if not ics_path.exists():
            raise HTTPException(404, f"Calendrier introuvable : {ics_path.name}")
        return FileResponse(
            ics_path,
            media_type="text/calendar",
            filename=ics_path.name,
        )

    # ----------------------------------------------------------------------------
    # Fragments partiels (htmx-friendly)
    # ----------------------------------------------------------------------------

    @app.get("/partials/status", response_class=HTMLResponse)
    def partials_status(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(
            "_status.html",
            {
                "request": request,
                "is_syncing": state.is_syncing(),
                "last_sync": state.last_sync,
                "last_error": state.last_error,
            },
        )

    # ----------------------------------------------------------------------------
    # Settings : carnet de prix
    # ----------------------------------------------------------------------------

    @app.get("/settings", response_class=HTMLResponse)
    def settings_view(request: Request, tab: str = "pricing") -> HTMLResponse:
        if tab not in ("pricing", "configuration", "integration"):
            tab = "pricing"
        book = state.load_prices()
        services = sorted({e.service.value for e in book.entries}) or [s.value for s in Service]
        # Petit récap des variables d'env utilisées (safe : pas de mot de passe).
        env_redacted = {
            "ESPACE_CITOYENS_USERNAME": state.config.username,
            "ESPACE_CITOYENS_PERSON_ID": str(state.config.person_id),
            "GUICHET_CRON": os.getenv("GUICHET_CRON", "0 6 * * *"),
            "GUICHET_AUTO_SYNC": os.getenv("GUICHET_AUTO_SYNC", "1"),
            "GUICHET_OUTPUT_DIR": str(state.config.output_dir),
            "HA_CALENDAR_ENTITY": state.config.ha_calendar_entity or "(non configuré)",
        }
        # Tokens iCal : on envoie un aperçu seulement, pas le token complet
        token_store = state.load_tokens()
        tokens_view = [
            {
                "id": t.id,
                "label": t.label,
                "created_at": t.created_at,
                "last_used_at": t.last_used_at,
                "token_preview": t.token[:8] + "…" + t.token[-4:],
                "full_token": t.token,  # uniquement pour la liste (visible UI),
            }
            for t in token_store.tokens
        ]
        ctx = {
            "request": request,
            "tab": tab,
            "entries": book.to_dict_list(),
            "services": services,
            "all_services": [s.value for s in Service],
            "default_service": "cantine",
            "env_redacted": env_redacted,
            "is_syncing": state.is_syncing(),
            "last_sync": state.last_sync,
            "last_error": state.last_error,
            "version": __version__,
            "ical_tokens": tokens_view,
            "public_base_url": os.getenv("GUICHET_PUBLIC_URL", ""),
        }
        return templates.TemplateResponse("settings.html", ctx)

    @app.post("/api/prices")
    async def api_prices_add(request: Request) -> JSONResponse:
        payload = await _read_form(request)  # accepte JSON ou form-data
        try:
            service = Service(payload["service"])
            amount = float(payload["amount_eur"])
            start = date.fromisoformat(payload["start_date"])
        except (KeyError, ValueError, TypeError) as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)

        book = state.load_prices()
        book.add_entry(
            service=service,
            amount_eur=amount,
            start_date=start,
            comment=payload.get("comment"),
        )
        state.save_prices(book)
        return JSONResponse({"ok": True, "count": len(book.entries)})

    @app.delete("/api/prices/{entry_id}")
    def api_prices_delete(entry_id: int) -> JSONResponse:
        book = state.load_prices()
        try:
            deleted = book.remove_entry(entry_id)
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        if not deleted:
            return JSONResponse({"error": "Entrée introuvable"}, status_code=404)
        state.save_prices(book)
        return JSONResponse({"ok": True, "count": len(book.entries)})

    @app.get("/api/prices", response_class=JSONResponse)
    def api_prices_list() -> JSONResponse:
        book = state.load_prices()
        return JSONResponse({"entries": book.to_dict_list()})

    # ----------------------------------------------------------------------------
    # Tokens iCal (intégration externe : Google Agenda, Apple, etc.)
    # ----------------------------------------------------------------------------

    @app.get("/ical/{token}")
    def ical_feed(token: str) -> Response:
        """Sert le flux iCal si le token est valide. Aucune autre forme d'auth
        (HTTPS via le reverse proxy = encryptage en transit + token long = secret).
        """
        store = state.load_tokens()
        match = store.find(token)
        if match is None:
            # Réponse volontairement minimaliste (pas d'oracle sur l'existence)
            raise HTTPException(status_code=404, detail="Flux iCal introuvable.")

        ics_path = state.config.output_dir / f"inscriptions_{state.config.person_id}.ics"
        if not ics_path.exists():
            raise HTTPException(
                status_code=503,
                detail="Aucune donnée iCal disponible. Lancez d'abord un sync.",
            )
        store.touch(match)
        state.save_tokens(store)
        return FileResponse(
            ics_path,
            media_type="text/calendar; charset=utf-8",
            filename="guichet-perisco.ics",
            headers={
                # Encourage la mise en cache côté client (Google refresh ~12 h).
                "Cache-Control": "public, max-age=3600",
            },
        )

    @app.post("/api/ical-tokens")
    async def api_ical_tokens_create(request: Request) -> JSONResponse:
        """Crée un nouveau token d'abonnement iCal."""
        payload = await _read_form(request)
        label = payload.get("label") or ""
        store = state.load_tokens()
        tok = store.create(label)
        state.save_tokens(store)
        return JSONResponse(
            {
                "ok": True,
                "id": tok.id,
                "label": tok.label,
                "token": tok.token,
                "created_at": tok.created_at,
            }
        )

    @app.delete("/api/ical-tokens/{token_id}")
    def api_ical_tokens_delete(token_id: int) -> JSONResponse:
        store = state.load_tokens()
        deleted = store.revoke(token_id)
        if not deleted:
            return JSONResponse({"error": "Token introuvable"}, status_code=404)
        state.save_tokens(store)
        return JSONResponse({"ok": True})

    @app.get("/api/ical-tokens")
    def api_ical_tokens_list() -> JSONResponse:
        """Liste les tokens existants. Le champ `token` complet n'est PAS renvoyé
        après création (une seule fois à la création) — sécurité minimisation.
        """
        store = state.load_tokens()
        items = []
        for t in store.tokens:
            items.append({
                "id": t.id,
                "label": t.label,
                "created_at": t.created_at,
                "last_used_at": t.last_used_at,
                "token_preview": t.token[:8] + "…" + t.token[-4:],
            })
        return JSONResponse({"tokens": items})

    # ----------------------------------------------------------------------------
    # Facturation : breakdown mensuel
    # ----------------------------------------------------------------------------

    @app.get("/facturation", response_class=HTMLResponse)
    def facturation_view(
        request: Request,
        year: int | None = Query(None),
        month: int | None = Query(None),
    ) -> HTMLResponse:
        today = date.today()
        year = year or today.year
        month = month or today.month
        breakdown = _monthly_breakdown(state, year, month)
        book = state.load_prices()
        ctx = {
            "request": request,
            "year": year,
            "month": month,
            "month_label": _month_label(year, month),
            "breakdown": breakdown,
            "missing_prices": [s.value for s, _ in breakdown.missing],
            "history": _months_with_data(state),
        }
        return templates.TemplateResponse("facturation.html", ctx)

    @app.get("/api/billing")
    def api_billing(year: int = Query(...), month: int = Query(...)) -> JSONResponse:
        breakdown = _monthly_breakdown(state, year, month)
        return JSONResponse(breakdown.to_dict())

    # ----------------------------------------------------------------------------
    # Calendrier (endpoints JSON pour FullCalendar)
    # ----------------------------------------------------------------------------

    @app.get("/api/events")
    def api_events(date_from: str | None = Query(None), date_to: str | None = Query(None)) -> JSONResponse:
        data, err = state.load_current_inscriptions()
        if err:
            return JSONResponse({"error": err}, status_code=404)
        events = _build_fc_events(data)
        # Filtre par date (et non par datetime : comparaison sur la portion YYYY-MM-DD
        # du champ start/end évite les bugs lexicographiques T<space>).
        if date_from:
            events = [e for e in events if e["start"][:10] >= date_from]
        if date_to:
            events = [e for e in events if e["start"][:10] <= date_to]
        return JSONResponse(events)

    return app


# ----------------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------------


async def _read_form(request: Request) -> dict[str, Any]:
    """Lit soit du JSON, soit du form-urlencoded."""
    ctype = (request.headers.get("content-type") or "").split(";", 1)[0].strip().lower()
    if ctype == "application/json":
        return await request.json()
    form = await request.form()
    return dict(form)


def _month_label(year: int, month: int) -> str:
    return date(year, month, 1).strftime("%B %Y").capitalize()


def _months_with_data(state: _RuntimeState) -> list[tuple[int, int]]:
    """Liste les mois où des inscriptions existent."""
    data, _ = state.load_current_inscriptions()
    if not data:
        return []
    months = sorted({
        (date.fromisoformat(i["jour"]).year, date.fromisoformat(i["jour"]).month)
        for i in data["enfant"]["inscriptions"]
    })
    return months


# ----------------------------------------------------------------------------
# Dashboard : semaine en cours + semaine prochaine + facturation bi-mensuelle
# ----------------------------------------------------------------------------


_WEEKDAY_FR_SHORT = ["Lun", "Mar", "Mer", "Jeu", "Ven", "Sam", "Dim"]


@dataclass
class _DayDetail:
    """Détail d'une journée (pour la grille semaine)."""
    date: date
    date_short: str  # "09/09"
    count: int
    total: float
    by_service: list[dict[str, Any]]  # [{"value": "cantine", "count": 17}, ...]
    is_today: bool
    is_past: bool


def _day_detail(state: _RuntimeState, jour: date) -> _DayDetail:
    """Détaille une journée : nombre d'inscriptions, coût total, breakdown service."""
    data, _ = state.load_current_inscriptions()
    book = state.load_prices()
    today = date.today()
    if not data:
        return _DayDetail(
            date=jour, date_short=jour.strftime("%d/%m"),
            count=0, total=0.0, by_service=[],
            is_today=jour == today, is_past=jour < today,
        )

    day_ins = [i for i in data["enfant"]["inscriptions"] if i["jour"] == jour.isoformat()]
    total = 0.0
    raw_counts: dict[str, int] = {}
    for ins in day_ins:
        svc = Service(ins["service"])
        raw_counts[svc.value] = raw_counts.get(svc.value, 0) + 1
        price = book.price_for(svc, jour)
        if price is None:
            current = book.current(svc)
            if current:
                price = current.amount_eur
        if price is not None:
            total += price

    by_service = sorted(
        ({"value": k, "count": v, "label": Service(k).label} for k, v in raw_counts.items()),
        key=lambda x: -x["count"],
    )
    return _DayDetail(
        date=jour,
        date_short=jour.strftime("%d/%m"),
        count=len(day_ins),
        total=total,
        by_service=by_service,
        is_today=jour == today,
        is_past=jour < today,
    )


def _week_summary(
    state: _RuntimeState, today: date | None = None,
) -> dict[str, Any]:
    """Construit les deux semaines (cette semaine, semaine prochaine)."""
    today = today or date.today()
    monday = today - timedelta(days=today.weekday())

    def _build(start: date) -> list[_DayDetail]:
        return [_day_detail(state, start + timedelta(days=i)) for i in range(7)]

    this_monday = monday
    next_monday = monday + timedelta(days=7)

    def _totals(days: list[_DayDetail]) -> dict[str, float | int]:
        return {
            "count": sum(d.count for d in days),
            "total": sum(d.total for d in days),
        }

    this_w = _build(this_monday)
    next_w = _build(next_monday)
    return {
        "this_week": {
            "start": this_monday,
            "start_short": this_monday.strftime("%d/%m/%Y"),
            "end": this_monday + timedelta(days=6),
            "end_short": (this_monday + timedelta(days=6)).strftime("%d/%m/%Y"),
            "days": this_w,
            "totals": _totals(this_w),
            "weekday_labels": _WEEKDAY_FR_SHORT,
        },
        "next_week": {
            "start": next_monday,
            "start_short": next_monday.strftime("%d/%m/%Y"),
            "end": next_monday + timedelta(days=6),
            "end_short": (next_monday + timedelta(days=6)).strftime("%d/%m/%Y"),
            "days": next_w,
            "totals": _totals(next_w),
            "weekday_labels": _WEEKDAY_FR_SHORT,
        },
    }


def _bi_monthly_billing(state: _RuntimeState, today: date | None = None) -> dict[str, Any]:
    """Breakdown mois précédent + mois en cours."""
    today = today or date.today()
    cur = _monthly_breakdown(state, today.year, today.month)
    prev_date = date(today.year, today.month, 1) - timedelta(days=1)
    prev = _monthly_breakdown(state, prev_date.year, prev_date.month)
    return {
        "current": cur,
        "previous": prev,
        "today": today,
    }


def _service_color_index() -> dict[str, str]:
    return _SERVICE_COLORS


def _service_legend(summary: dict[str, Any]) -> list[dict[str, str]]:
    """Construit la légende (couleur → libellé) de tous les services connus.
    Triée par libellé.
    """
    legend: list[dict[str, str]] = []
    for value in sorted({s.value for s in Service}, key=lambda v: Service(v).label):
        legend.append({
            "value": value,
            "label": Service(value).label,
            "color": _SERVICE_COLORS.get(value, "#34495e"),
        })
    return legend


@dataclass
class _Breakdown:
    year: int
    month: int
    total: float
    by_service: list[dict[str, Any]]
    missing: list[tuple[Service, int]]  # services sans prix défini pour ce mois
    total_count: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "year": self.year,
            "month": self.month,
            "total": round(self.total, 2),
            "total_count": self.total_count,
            "by_service": self.by_service,
            "missing": [{"service": s.value, "count": c} for s, c in self.missing],
        }


def _monthly_breakdown(state: _RuntimeState, year: int, month: int) -> _Breakdown:
    data, err = state.load_current_inscriptions()
    book = state.load_prices()
    if err or not data:
        return _Breakdown(year, month, 0.0, [], [], 0)

    # Filtre sur le mois demandé
    start = date(year, month, 1)
    last_day = calendar.monthrange(year, month)[1]
    end = date(year, month, last_day)
    # Si le mois est dans le futur, on calcule quand même (utile à des fins de tests)

    inscriptions = [
        i for i in data["enfant"]["inscriptions"]
        if start <= date.fromisoformat(i["jour"]) <= end
    ]

    total = 0.0
    total_count = len(inscriptions)
    by_service: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"service": "", "count": 0, "total": 0.0, "current_price": None, "missing_price": False}
    )
    missing_counts: dict[Service, int] = defaultdict(int)

    for ins in inscriptions:
        svc = Service(ins["service"])
        jour = date.fromisoformat(ins["jour"])
        price = book.price_for(svc, jour)
        # Si pas de prix défini pour ce jour, tente le prix courant du service
        if price is None:
            current = book.current(svc)
            if current:
                price = current.amount_eur
            else:
                missing_counts[svc] += 1

        slot = by_service[svc.value]
        slot["service"] = svc.label
        slot["service_value"] = svc.value
        slot["count"] += 1
        if price is not None:
            slot["total"] += price
            total += price
        else:
            slot["missing_price"] = True
        # Prix courant (le plus récent) pour l'affichage
        current = book.current(svc)
        if current:
            slot["current_price"] = current.amount_eur

    return _Breakdown(
        year=year,
        month=month,
        total=total,
        by_service=sorted(by_service.values(), key=lambda x: -x["total"]),
        missing=list(missing_counts.items()),
        total_count=total_count,
    )


def _build_fc_events(data: dict[str, Any]) -> list[dict[str, Any]]:
    """Convertit les inscriptions en events FullCalendar (JSON, pas iCal)."""
    events: list[dict[str, Any]] = []
    for ins in data.get("enfant", {}).get("inscriptions", []):
        jour = date.fromisoformat(ins["jour"])
        defaults = DEFAULT_SCHEDULE.get(Service(ins["service"]), ((8, 0), (18, 0)))
        h_debut = ins.get("heure_debut")
        h_fin = ins.get("heure_fin")
        if h_debut:
            hh, mm = h_debut.split(":")[:2]
            start_h, start_m = int(hh), int(mm)
        else:
            start_h, start_m = defaults[0]
        if h_fin:
            hh, mm = h_fin.split(":")[:2]
            end_h, end_m = int(hh), int(mm)
        else:
            end_h, end_m = defaults[1]

        start_dt = f"{jour.isoformat()}T{start_h:02d}:{start_m:02d}:00"
        end_dt = f"{jour.isoformat()}T{end_h:02d}:{end_m:02d}:00"

        raw = ins.get("raw") or {}
        lib_unite = raw.get("lib_unite") or ""
        ecole = raw.get("ecole") or ""
        title = f"{ins['service']} — {lib_unite}" if lib_unite else ins["service"]
        events.append({
            "id": f"{jour.isoformat()}-{ins['service']}-{raw.get('id_unite', '')}",
            "title": title,
            "start": start_dt,
            "end": end_dt,
            "allDay": False,
            "extendedProps": {
                "service": ins["service"],
                "ecole": ecole,
                "libelle": lib_unite,
            },
            # Couleur par service
            "backgroundColor": _service_color(ins["service"]),
            "borderColor": _service_color(ins["service"]),
        })
    return events


_SERVICE_COLORS = {
    "cantine": "#e74c3c",
    "periscolaire_matin": "#3498db",
    "periscolaire_soir": "#9b59b6",
    "espace_detente": "#f39c12",
    "periscolaire": "#3498db",
    "accueil_loisirs": "#27ae60",
    "autre": "#95a5a6",
}


def _service_color(service_value: str) -> str:
    return _SERVICE_COLORS.get(service_value, "#34495e")


# ----------------------------------------------------------------------------
# Point d'entrée CLI (`guichet web`)
# ----------------------------------------------------------------------------


def run_web(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT, config: Config | None = None) -> None:
    """Lance le serveur uvicorn. Bloquant."""
    import uvicorn

    if config is None:
        config = Config.from_env()

    app = create_app(config)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    uvicorn.run(
        app,
        host=host,
        port=port,
        log_level="info",
        access_log=True,
    )
