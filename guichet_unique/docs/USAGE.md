# Guide d'utilisation

## Pré-requis

- **Python ≥ 3.11**
- [`uv`](https://docs.astral.sh/uv/) (gestionnaire de paquets Python)
- Un compte citoyen sur le portail `espace-citoyens.net` de votre commune
- L'identifiant de votre enfant dans l'URL `FichePersonne/DetailPersonne?idDynamic=X`

## Comment ça marche

Le script exploite deux endpoints AJAX/JSON internes au portail (la fiche
personne est en HTML, mais les calendriers détaillés sont en JSON) :

```
                ┌────────────────────────────────────┐
                │  1. Login (POST /Home/Logon)        │
                └─────────────────┬──────────────────┘
                                  ▼
            ┌─────────────────────────────────────┐
            │ 2. GET /FichePersonne/DetailPersonne│
            │    → liste des services et lien     │
            │      vers chaque calendrier         │
            └─────────────────┬───────────────────┘
                              ▼
            ┌─────────────────────────────────────────────┐
            │ Pour chaque service :                       │
            │ 3. GET /DemandeEnfance/                     │
            │        NouvelleDemandeReservation/{pers}/.. │
            │    → récupère idPer et token CSRF            │
            │ 4. GET /DemandeEnfance/                     │
            │        NouvelleDemandeReservationGetCalendrier│
            │    → calendrier jour-par-jour (JSON)        │
            └─────────────────────────────────────────────┘
                              ▼
            ┌─────────────────────────────────────┐
            │ 5. Export JSON + CSV + iCal         │
            └─────────────────────────────────────┘
```

Pour chaque jour, on obtient une unité réservée (`Repas`, `Garderie du matin`,
`Garderie récréative soir`, etc.) que le script mappe vers notre enum `Service`.

## Installation

```bash
cd ~/projets/guichet-unique/guichet_unique
uv sync                       # dépendances de base
uv sync --extra scheduler     # ajoute APScheduler pour le mode daemon
```

## Configuration

```bash
cp .env.example .env
$EDITOR .env
```

| Variable                       | Obligatoire | Description                                              |
| ------------------------------ | ----------- | -------------------------------------------------------- |
| `ESPACE_CITOYENS_USERNAME`     | oui         | Identifiant de votre compte famille                      |
| `ESPACE_CITOYENS_PASSWORD`     | oui         | Mot de passe associé                                     |
| `ESPACE_CITOYENS_BASE_URL`     | oui         | URL du portail, ex. `https://www.espace-citoyens.net/<commune>/espace-citoyens` |
| `ESPACE_CITOYENS_PERSON_ID`    | non         | ID dynamique de la personne (défaut : `4`)               |
| `GUICHET_OUTPUT_DIR`           | non         | Répertoire de sortie (défaut : `./data`)                 |
| `HA_URL`                       | non         | URL de HA (défaut add-on : `http://supervisor/core`)     |
| `HA_TOKEN`                     | hors add-on | Jeton longue durée HA (l'add-on utilise `SUPERVISOR_TOKEN`) |
| `HA_CALENDAR_ENTITY`           | pour publier | Entité cible, ex. `calendar.agenda_enfant`      |
| `GUICHET_DEFAULT_LOCATION`     | non         | Adresse de l'école (lieu des événements)                 |
| `MQTT_HOST` / `MQTT_PORT`      | non         | Broker MQTT pour les entités HA (port défaut `1883`)     |
| `MQTT_USERNAME` / `MQTT_PASSWORD` | non      | Identifiants du broker                                   |

> **⚠ Sécurité** : ne committez jamais le fichier `.env`. Il est dans `.gitignore`.

## Commandes

### Vérifier la configuration

```bash
uv run guichet check
```

### Lancer une extraction ponctuelle

```bash
uv run guichet sync
```

Produit dans `./data/` :

- `inscriptions_<id>.json` — données structurées avec métadonnées (périodes, services, école)
- `inscriptions_<id>.csv` — tableur (Excel/LibreOffice) avec BOM UTF-8, séparateur `;`
- `inscriptions_<id>.ics` — calendrier iCal importable dans Google/Apple Calendar
- `cal_<id>_<service>.json` — calendriers bruts pour debug

Options :

```bash
uv run guichet sync --person-id 7 --output-dir /tmp/dump --no-raw
```

### Synchronisation périodique (daemon)

```bash
uv sync --extra scheduler
uv run guichet daemon --cron "0 6 * * *"
```

Quelques crons utiles :

| Cron              | Signification                                |
| ----------------- | -------------------------------------------- |
| `0 6 * * *`       | Tous les jours à 06:00                       |
| `0 */4 * * *`     | Toutes les 4 heures                          |
| `0 6 * * 1-5`     | Du lundi au vendredi à 06:00                 |

En production, utilisez l'add-on Home Assistant (voir le [README](../README.md)).

## Importer le calendrier dans Google Calendar

1. `uv run guichet sync`
2. Ouvrir [Google Calendar](https://calendar.google.com) → ⚙️ → **Importer et exporter**
3. Sélectionner le fichier `data/inscriptions_4.ics`

## Structure des données

`inscriptions_4.json` :

```json
{
  "extracted_at": "2026-08-31T23:40:10",
  "source_url": "https://www.espace-citoyens.net/macommune/espace-citoyens/FichePersonne/DetailPersonne?idDynamic=4",
  "links": [
    {
      "id_ins": 80342, "id_lie": 104, "id_clg": 1392,
      "service_label": "Restauration Scolaire",
      "ecole": "ELEMENTAIRE THOMAS PESQUET",
      "periode": "du 01/09/2026 au 02/07/2027",
      "debut_periode": "2026-09-01",
      "fin_periode": "2027-07-02"
    }
  ],
  "enfant": {
    "id": 4,
    "prenom": "Camille",
    "inscriptions": [
      {
        "service": "cantine",
        "jour": "2026-09-07",
        "statut": null,
        "raw": {
          "id_unite": 1,
          "lib_unite": "Repas",
          "nb_conso": 1,
          "service_label": "Restauration Scolaire",
          "ecole": "ELEMENTAIRE THOMAS PESQUET"
        }
      }
    ]
  }
}
```

Le champ `statut` peut valoir :

- `null` — réservation active dans la fenêtre de modification
- `"Hors délai"` — date hors fenêtre de modification (cas des premiers jours
  de septembre avant l'ouverture du portail aux parents)

## Dépannage

### Aucune inscription détectée

1. Vérifiez l'id de la personne dans l'URL de la fiche (`idDynamic=...`)
2. Lancez en mode verbeux : `uv run guichet sync -v`
3. Inspectez les fichiers `data/cal_*.json` — la structure JSON doit contenir
   `listeSemainesAffichees[].listeJoursAffiches[].listeUnitesJour[].nbConsoBase > 0`

### Erreurs « Identifiants invalides » répétées

Le serveur applique un rate-limit. Patientez 1 à 2 minutes puis réessayez.

### Session expirée immédiatement après login

Cela arrive parfois après de nombreux appels rapides. Réduisez la fréquence
du daemon (cron toutes les 6h au lieu de toutes les heures) ou utilisez un
delai aléatoire dans vos tests.
