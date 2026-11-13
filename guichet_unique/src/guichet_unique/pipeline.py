"""Orchestration : login → fiche personne → calendriers JSON → diff → export."""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path

from rich.console import Console
from rich.table import Table

from .calendar import extract_inscriptions, parse_metadata
from .client import EspaceCitoyensClient
from .config import Config
from .diff import compute_diff
from .exporters import export_csv, export_ical, export_json
from .models import Enfant, ExtractionResult, InscriptionLink
from .reservations import extract_person_name, extract_reservation_links
from .state import (
    State,
    default_state_path,
    load_state,
    save_state,
)

logger = logging.getLogger(__name__)


def run_extraction(
    config: Config,
    *,
    keep_raw_payloads: bool = True,
    output_json: bool = True,
    output_csv: bool = True,
    output_ical: bool = True,
    use_diff: bool = True,
) -> ExtractionResult:
    """Exécute le cycle complet : login → fiche personne → calendriers → diff → export."""
    output_dir: Path = config.output_dir
    output_dir.mkdir(parents=True, exist_ok=True)

    # 0. Charger l'état précédent
    state_path = default_state_path(output_dir, config.person_id)
    if use_diff:
        previous_state = load_state(state_path, config.person_id)
    else:
        previous_state = State.empty(config.person_id)

    client = EspaceCitoyensClient(config)
    client.login()

    # 1. Fiche personne → liste des services de réservation
    logger.info("Récupération de la fiche personne id=%s…", config.person_id)
    detail_html = client.fetch_detail_personne(config.person_id)
    links = extract_reservation_links(detail_html, config.person_id)
    if not links:
        logger.warning(
            "Aucun lien de réservation détecté sur la fiche personne. "
            "Vérifiez que l'enfant a bien des inscriptions actives."
        )

    # 2. Pour chaque service, on récupère le calendrier JSON jour-par-jour
    raw_payloads: list[Path] = []
    all_inscriptions: list = []
    inscription_links: list[InscriptionLink] = []

    for link in links:
        logger.info(
            "Service '%s' (%s) : récupération du calendrier…",
            link.label,
            link.ecole,
        )

        try:
            id_per, csrf = client.fetch_reservation_page(
                person_id=config.person_id,
                id_ins=link.id_ins,
                id_lie=link.id_lie,
                id_clg=link.id_clg,
            )
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "Impossible de charger la page de réservation '%s' : %s",
                link.label,
                exc,
            )
            continue

        referer = client._url(
            client.RESERVATION_PATH_TEMPLATE.format(
                person_id=config.person_id,
                id_ins=link.id_ins,
                id_lie=link.id_lie,
                id_clg=link.id_clg,
            )
        )

        try:
            calendar = client.fetch_reservation_calendar(
                id_per=id_per,
                id_ins=link.id_ins,
                id_lie=link.id_lie,
                id_clg=link.id_clg,
                csrf=csrf,
                referer=referer,
            )
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "Calendrier '%s' indisponible : %s", link.label, exc
            )
            continue

        if keep_raw_payloads:
            label_safe = (
                link.label.replace(" ", "_").replace("/", "-") or f"ins_{link.id_ins}"
            )
            raw_path = output_dir / f"cal_{config.person_id}_{label_safe}.json"
            raw_path.write_text(
                json.dumps(calendar, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            raw_payloads.append(raw_path)
            logger.debug("Calendrier brut sauvegardé : %s", raw_path)

        meta = parse_metadata(calendar, service_label=link.label, ecole=link.ecole)
        inscriptions = extract_inscriptions(
            calendar, service_label=link.label, ecole=link.ecole
        )
        all_inscriptions.extend(inscriptions)

        inscription_links.append(
            InscriptionLink(
                id_ins=link.id_ins,
                id_lie=link.id_lie,
                id_clg=link.id_clg,
                service_label=link.label,
                ecole=link.ecole,
                periode=link.periode,
                debut_periode=meta.debut_periode,
                fin_periode=meta.fin_periode,
            )
        )

    # 3. Déduplication et tri
    seen: set[tuple] = set()
    unique_inscriptions = []
    for ins in all_inscriptions:
        key = (ins.jour, ins.service)
        if key in seen:
            continue
        seen.add(key)
        unique_inscriptions.append(ins)
    unique_inscriptions.sort(key=lambda i: (i.jour, i.service.value))

    # 4. Calcul du diff avec l'état précédent
    diff = compute_diff(unique_inscriptions, previous_state) if use_diff else None

    # 5. Construction du résultat final
    prenom = extract_person_name(detail_html, config.person_id)
    nom = ""

    result = ExtractionResult(
        extracted_at=datetime.now(),
        enfant=Enfant(
            id=config.person_id,
            nom=nom,
            prenom=prenom,
            inscriptions=unique_inscriptions,
        ),
        source_url=(
            f"{config.base_url}/FichePersonne/DetailPersonne"
            f"?idDynamic={config.person_id}"
        ),
        raw_payloads=raw_payloads,
        links=inscription_links,
    )

    logger.info(
        "Extraction OK : %d inscriptions uniques sur %d service(s).",
        len(unique_inscriptions),
        len(inscription_links),
    )

    # 6. Exports
    base_name = f"inscriptions_{config.person_id}"
    if output_json:
        export_json(result, output_dir / f"{base_name}.json")
    if output_csv:
        export_csv(result, output_dir / f"{base_name}.csv")
    if output_ical:
        export_ical(
            result, output_dir / f"{base_name}.ics",
            diff=diff, default_location=config.default_location,
        )

    # 7. Sauvegarde du nouvel état
    if use_diff:
        new_state = State.empty(config.person_id)
        new_state.last_extracted_at = datetime.now().isoformat(timespec="seconds")
        for ins in unique_inscriptions:
            new_state.add_inscription(ins)
        save_state(new_state, state_path)
        logger.info(
            "State sauvegardé : %d événements. %s",
            len(new_state.events),
            diff.summary() if diff else "n/a",
        )

    # 8. Affichage détaillé du diff
    if use_diff and diff is not None and not diff.is_empty:
        console = Console()
        table = Table(title="Changements détectés", show_lines=False)
        table.add_column("Type", style="bold")
        table.add_column("Date")
        table.add_column("Détails")
        for change in diff.removed:
            prev = change.previous
            assert prev is not None
            table.add_row(
                "[red]supprimé[/red]",
                change.jour.isoformat(),
                f"{prev.service_label} ({prev.lib_unite})",
            )
        for change in diff.added:
            curr = change.current
            assert curr is not None
            table.add_row(
                "[green]ajouté[/green]",
                change.jour.isoformat(),
                f"{curr.service_label} ({curr.lib_unite})",
            )
        for change in diff.modified:
            curr = change.current
            prev = change.previous
            assert curr is not None and prev is not None
            table.add_row(
                "[yellow]modifié[/yellow]",
                change.jour.isoformat(),
                f"{prev.service_label}: {prev.statut or '-'} → {curr.statut or '-'}",
            )
        console.print(table)

    return result
