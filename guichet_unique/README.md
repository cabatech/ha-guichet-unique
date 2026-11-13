# HA - Guichet Unique

Add-on Home Assistant qui extrait les inscriptions **périscolaire** et
**cantine** d'un portail [`espace-citoyens.net`](https://www.espace-citoyens.net)
et :

- les écrit dans un **agenda Home Assistant** : agenda Google (intégration
  native *Google Calendar*) ou agenda local (*Local Calendar*) ;
- expose des **entités** (via MQTT) : cantine/périscolaire aujourd'hui et
  demain, nombre de jours et coût du mois par service, coût total du mois et du
  mois précédent, dernière synchro.

## Fonctionnement

```
espace-citoyens.net ──HTTP──> add-on (sync planifiée) ──API HA──> calendar.xxx
                                     │            └─────MQTT────> entités guichet_*
                               WebUI (Ingress)
```

- Chaque inscription devient un événement ; sa description se termine par
  `guichet-id: …` — c'est ce marqueur qui permet de retrouver, mettre à jour
  et supprimer **uniquement** les événements de l'add-on. Les autres
  événements de l'agenda ne sont jamais touchés.
- Une modification = suppression + recréation (l'intégration Google n'expose
  pas la mise à jour dans HA).
- L'agenda cible doit accepter création **et** suppression
  (`supported_features` ≥ 3) : c'est le cas de Google Calendar et Local Calendar.

## Installation

1. Paramètres → Modules complémentaires → Boutique → ⋮ → **Dépôts** → ajouter
   `https://github.com/cabatech/ha-guichet-unique`.
2. Installer **HA - Guichet Unique**, puis renseigner les options :

| Option            | Description                                               |
| ----------------- | --------------------------------------------------------- |
| `base_url`        | Portail de la commune, ex. `https://www.espace-citoyens.net/<commune>/espace-citoyens` |
| `username`        | Identifiant espace-citoyens                               |
| `password`        | Mot de passe espace-citoyens                              |
| `person_id`       | ID de l'enfant sur le portail (`idDynamic=` dans l'URL de sa fiche) |
| `calendar_entity` | Agenda cible, ex. `calendar.agenda_enfant`                |
| `cron`            | Planification de la sync (défaut `0 6 * * *`, Europe/Paris) |
| `school_address`  | *(optionnel, masqué)* Adresse de l'école, lieu des événements |
| `public_url`      | *(optionnel)* URL publique pour les liens d'abonnement iCal |
| `mqtt_host`       | *(optionnel)* Broker MQTT, ex. `core-mosquitto` ; vide = pas d'entités |
| `mqtt_port`, `mqtt_username`, `mqtt_password` | Accès au broker      |

3. Démarrer. La WebUI (tableau, calendrier, facturation, tarifs, sync
   manuelle) est accessible depuis la barre latérale via Ingress. Les coûts
   utilisent les tarifs saisis dans l'onglet **Tarifs**.

## Entités (MQTT discovery)

Regroupées sous l'appareil **Guichet Unique**, une série par service présent
dans les inscriptions (`cantine`, `periscolaire_matin`, `periscolaire_soir`, …) :

| Entité                                  | Type          |
| --------------------------------------- | ------------- |
| `binary_sensor.guichet_<service>_aujourdhui` / `_demain` | on/off |
| `sensor.guichet_<service>_mois`         | jours ce mois |
| `sensor.guichet_<service>_cout_mois`    | € ce mois     |
| `sensor.guichet_cout_mois` / `_cout_mois_precedent` | € total |
| `sensor.guichet_derniere_sync`          | horodatage    |
| `button.guichet_synchroniser`           | sync manuelle (portail + agenda) |

Mises à jour après chaque sync, au démarrage et à 00:01 (bascule
aujourd'hui/demain). Si l'add-on est arrêté, les valeurs restent figées.

Le port 8236 est fermé par défaut ; ne le mapper que pour exposer le flux iCal
`/ical/<token>` (le reste de la WebUI exige alors le Basic Auth
`GUICHET_BASIC_USER/PASS`, ou un mot de passe aléatoire affiché dans les logs).

## Développement local

Voir [`docs/USAGE.md`](docs/USAGE.md). Hors add-on, `HA_URL` et `HA_TOKEN`
(jeton longue durée) remplacent le jeton Supervisor.

```bash
uv sync --extra web --extra scheduler --extra dev
uv run pytest
uv run guichet publish --sync
```
