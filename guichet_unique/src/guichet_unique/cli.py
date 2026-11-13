"""CLI pour guichet-unique."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import date, datetime, time as _time
from pathlib import Path

from rich.console import Console
from rich.logging import RichHandler
from rich.table import Table

from .config import Config, ConfigError
from .ha_calendar import publish_to_ha
from .models import Enfant, ExtractionResult, Inscription, Service
from .pipeline import run_extraction


def _setup_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(message)s",
        datefmt="[%X]",
        handlers=[RichHandler(rich_tracebacks=True, show_path=False)],
    )


def _display(result: ExtractionResult, console: Console) -> None:
    """Affiche un résumé lisible dans la console."""
    console.rule("[bold green]Extraction terminée")
    nom_complet = " ".join(
        p for p in (result.enfant.prenom, result.enfant.nom) if p
    ) or "(inconnu)"
    console.print(
        f"[bold]Enfant :[/bold] {nom_complet} (id={result.enfant.id})"
    )

    if result.links:
        link_table = Table(title="Services détectés", show_lines=False)
        link_table.add_column("Service", style="cyan")
        link_table.add_column("École", style="dim")
        link_table.add_column("Période d'inscription", style="magenta")
        link_table.add_column("Période d'affichage")
        for lk in result.links:
            plage = ""
            if lk.debut_periode and lk.fin_periode:
                plage = f"{lk.debut_periode} → {lk.fin_periode}"
            link_table.add_row(lk.service_label, lk.ecole, lk.periode, plage)
        console.print(link_table)

    console.print(
        f"[bold]Inscriptions détectées :[/bold] {len(result.enfant.inscriptions)}"
    )
    if not result.enfant.inscriptions:
        console.print(
            "[yellow]Aucune inscription détectée. Vérifiez les calendriers "
            "bruts dans data/cal_*.json.[/yellow]"
        )
        return

    by_service: dict[str, int] = {}
    for ins in result.enfant.inscriptions:
        by_service[ins.service.label] = by_service.get(ins.service.label, 0) + 1

    summary = Table(title="Résumé par service", show_lines=False)
    summary.add_column("Service", style="cyan")
    summary.add_column("Nombre de jours", justify="right", style="magenta")
    for label, count in sorted(by_service.items()):
        summary.add_row(label, str(count))
    console.print(summary)


def cmd_sync(args: argparse.Namespace) -> int:
    """Sous-commande `sync` : extraction ponctuelle + exports locaux."""
    _setup_logging(args.verbose)
    console = Console()

    try:
        config = Config.from_env(person_id=args.person_id, output_dir=args.output_dir)
    except ConfigError as exc:
        console.print(f"[red]Configuration invalide :[/red] {exc}")
        return 2

    try:
        result = run_extraction(
            config,
            keep_raw_payloads=not args.no_raw,
            output_json=not args.no_json,
            output_csv=not args.no_csv,
            output_ical=not args.no_ical,
            use_diff=not args.no_diff,
        )
    except Exception as exc:  # noqa: BLE001
        console.print(f"[red]Échec de l'extraction :[/red] {exc}")
        if args.verbose:
            console.print_exception()
        return 1

    _display(result, console)
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    """Sous-commande `check` : vérifie la configuration sans faire d'appel réseau."""
    _setup_logging(args.verbose)
    console = Console()
    try:
        config = Config.from_env(person_id=args.person_id)
    except ConfigError as exc:
        console.print(f"[red]Configuration invalide :[/red] {exc}")
        return 2

    console.print("[green]Configuration valide :[/green]")
    console.print(f"  URL de base : {config.base_url}")
    console.print(f"  Personne ciblée : id={config.person_id}")
    console.print(f"  Répertoire de sortie : {config.output_dir}")
    console.print(f"  Identifiant : {config.username!r}")
    if config.ha_calendar_entity:
        console.print(f"  Agenda HA : {config.ha_calendar_entity} ({config.ha_url})")
    else:
        console.print("  Agenda HA : [yellow]non configuré[/yellow]")
    return 0


def cmd_daemon(args: argparse.Namespace) -> int:
    """Sous-commande `daemon` : synchronisation périodique via APScheduler."""
    _setup_logging(args.verbose)
    console = Console()

    try:
        from apscheduler.schedulers.blocking import BlockingScheduler
        from apscheduler.triggers.cron import CronTrigger
    except ImportError:
        console.print(
            "[red]APScheduler n'est pas installé.[/red] "
            "Installez l'extra `scheduler` : [cyan]uv sync --extra scheduler[/cyan]"
        )
        return 2

    try:
        config = Config.from_env(person_id=args.person_id, output_dir=args.output_dir)
    except ConfigError as exc:
        console.print(f"[red]Configuration invalide :[/red] {exc}")
        return 2

    console.print(
        f"[green]Démarrage du daemon[/green] — cron : {args.cron!r} "
        f"(fuseau Europe/Paris)"
    )

    scheduler = BlockingScheduler(timezone="Europe/Paris")

    def job() -> None:
        try:
            result = run_extraction(config)
            if config.ha_calendar_entity:
                publish_to_ha(result, config)
        except Exception as exc:  # noqa: BLE001
            console.print(f"[red]Échec : {exc}[/red]")

    scheduler.add_job(job, CronTrigger.from_crontab(args.cron))
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        console.print("[yellow]Daemon arrêté.[/yellow]")
    return 0


def _load_result_from_json(path: Path) -> ExtractionResult:
    """Reconstruit un ExtractionResult depuis le fichier JSON local."""
    data = json.loads(path.read_text(encoding="utf-8"))
    inscriptions = []
    for ins in data.get("enfant", {}).get("inscriptions", []):
        jour = date.fromisoformat(ins["jour"])
        h_d = _time.fromisoformat(ins["heure_debut"]) if ins.get("heure_debut") else None
        h_f = _time.fromisoformat(ins["heure_fin"]) if ins.get("heure_fin") else None
        inscriptions.append(
            Inscription(
                service=Service(ins["service"]),
                jour=jour,
                heure_debut=h_d,
                heure_fin=h_f,
                statut=ins.get("statut"),
                raw=ins.get("raw", {}),
            )
        )
    return ExtractionResult(
        extracted_at=datetime.fromisoformat(data["extracted_at"]),
        enfant=Enfant(
            id=data["enfant"]["id"],
            nom=data["enfant"].get("nom", ""),
            prenom=data["enfant"].get("prenom", ""),
            inscriptions=inscriptions,
        ),
        source_url=data["source_url"],
    )


def cmd_web(args: argparse.Namespace) -> int:
    """Sous-commande `web` : lance la WebUI FastAPI."""
    _setup_logging(args.verbose)
    console = Console()
    try:
        config = Config.from_env(person_id=args.person_id, output_dir=args.output_dir)
    except ConfigError as exc:
        console.print(f"[red]Configuration invalide :[/red] {exc}")
        return 2

    try:
        from .web import run_web
    except ImportError:
        console.print(
            "[red]Dépendances WebUI manquantes.[/red] "
            "Installez l'extra `web` : [cyan]uv sync --extra web[/cyan]"
        )
        return 2

    console.print(f"[green]WebUI :[/green] http://{args.host}:{args.port}")
    run_web(host=args.host, port=args.port, config=config)
    return 0


def cmd_publish(args: argparse.Namespace) -> int:
    """Sous-commande `publish` : synchronise les inscriptions vers l'agenda HA."""
    _setup_logging(args.verbose)
    console = Console()

    try:
        config = Config.from_env(person_id=args.person_id, output_dir=args.output_dir)
    except ConfigError as exc:
        console.print(f"[red]Configuration invalide :[/red] {exc}")
        return 2

    if not config.ha_calendar_entity:
        console.print(
            "[red]HA_CALENDAR_ENTITY manquant.[/red] Hors add-on, ajoutez dans .env :\n"
            "  HA_URL=http://homeassistant.local:8123\n"
            "  HA_TOKEN=<jeton longue durée>\n"
            "  HA_CALENDAR_ENTITY=calendar.agenda_enfant"
        )
        return 2

    if args.sync:
        console.print("Sync préalable depuis le portail…")
        try:
            run_extraction(config)
        except Exception as exc:  # noqa: BLE001
            console.print(f"[red]Échec du sync :[/red] {exc}")
            return 1

    json_path = config.output_dir / f"inscriptions_{config.person_id}.json"
    if not json_path.exists():
        console.print(
            f"[red]Aucun fichier d'inscription trouvé : {json_path}[/red]\n"
            "[yellow]Lancez d'abord :[/yellow] uv run guichet sync"
        )
        return 1

    result = _load_result_from_json(json_path)
    console.print(
        f"[green]✓ {len(result.enfant.inscriptions)} inscriptions chargées.[/green]"
    )

    console.print(
        f"[yellow]⚠ Les événements Guichet absents du JSON local "
        f"seront supprimés de {config.ha_calendar_entity}.[/yellow]"
    )
    if not args.yes:
        confirm = console.input("Confirmer ? [y/N] ")
        if confirm.lower() not in ("y", "yes", "oui", "o"):
            console.print("[yellow]Annulé.[/yellow]")
            return 0

    stats = publish_to_ha(result, config)
    console.print(
        f"[green]✓ Agenda HA :[/green] "
        f"+{stats.created} créés, ~{stats.updated} maj, "
        f"-{stats.deleted} supprimés, {stats.errors} erreurs."
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="guichet",
        description=(
            "Extraction des inscriptions périscolaire/cantine depuis "
            "espace-citoyens.net et publication vers un agenda Home Assistant"
        ),
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true", help="Active les logs détaillés (DEBUG)"
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    # sync -------------------------------------------------------------
    sync = subparsers.add_parser("sync", help="Lancer une extraction ponctuelle")
    sync.add_argument(
        "--person-id", type=int, default=None, help="ID de la personne (override .env)"
    )
    sync.add_argument(
        "--output-dir", type=Path, default=None, help="Répertoire de sortie (override .env)"
    )
    sync.add_argument(
        "--no-raw", action="store_true", help="Ne pas sauvegarder les calendriers JSON bruts"
    )
    sync.add_argument("--no-json", action="store_true", help="Ne pas exporter en JSON")
    sync.add_argument("--no-csv", action="store_true", help="Ne pas exporter en CSV")
    sync.add_argument("--no-ical", action="store_true", help="Ne pas exporter en iCal")
    sync.add_argument(
        "--no-diff",
        action="store_true",
        help="Désactive le diff intelligent (régénère tout, ignore le state)",
    )
    sync.set_defaults(func=cmd_sync)

    # check ------------------------------------------------------------
    check = subparsers.add_parser("check", help="Vérifier la configuration")
    check.add_argument(
        "--person-id", type=int, default=None, help="ID de la personne (override .env)"
    )
    check.set_defaults(func=cmd_check)

    # daemon -----------------------------------------------------------
    daemon = subparsers.add_parser(
        "daemon",
        help="Synchronisation périodique (nécessite l'extra `scheduler`)",
    )
    daemon.add_argument(
        "--cron",
        default="0 6 * * *",
        help="Expression cron (défaut : '0 6 * * *' = tous les jours à 6h)",
    )
    daemon.add_argument(
        "--person-id", type=int, default=None, help="ID de la personne (override .env)"
    )
    daemon.add_argument(
        "--output-dir", type=Path, default=None, help="Répertoire de sortie (override .env)"
    )
    daemon.set_defaults(func=cmd_daemon)

    # web --------------------------------------------------------------
    web = subparsers.add_parser(
        "web",
        help="Lance la WebUI FastAPI (status / sync manuel / tableau / calendrier)",
    )
    web.add_argument(
        "--host", default="0.0.0.0",
        help="Interface d'écoute (défaut : 0.0.0.0)",
    )
    web.add_argument(
        "--port", type=int, default=8236,
        help="Port d'écoute (défaut : 8236)",
    )
    web.add_argument(
        "--person-id", type=int, default=None, help="ID de la personne (override .env)",
    )
    web.add_argument(
        "--output-dir", type=Path, default=None, help="Répertoire de sortie (override .env)",
    )
    web.set_defaults(func=cmd_web)
    publish = subparsers.add_parser(
        "publish",
        help="Synchronise les inscriptions vers l'agenda Home Assistant",
    )
    publish.add_argument(
        "--person-id", type=int, default=None, help="ID de la personne"
    )
    publish.add_argument(
        "--output-dir", type=Path, default=None, help="Répertoire de sortie"
    )
    publish.add_argument(
        "--sync",
        action="store_true",
        help="Lancer un sync portail avant la publication",
    )
    publish.add_argument(
        "--yes", "-y",
        action="store_true",
        help="Skip la confirmation interactive",
    )
    publish.set_defaults(func=cmd_publish)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
