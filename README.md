# AIKUB Hermes Telemetry Agent Script

Ce repo contient le **script d'instructions** que les bots Hermes/Aikub lisent quand leur cron de self-logging est actif.

Ce n'est pas un tutoriel pour installer le logging. Ce n'est pas un script qui demande au bot de recevoir toutes ses infos en `.env`.

Le but : donner au bot la marche à suivre pour documenter dans **Aikub Telemetry** ce que Hermes sait déjà localement.

## Étape 1

Fichier principal :

```text
telemetry_cron_script.md
```

À cette étape, le bot doit logger seulement :

| Donnée | Source |
|---|---|
| identité | les 4 variables `.env` + runtime local |
| modèle d'intelligence | config Hermes locale |
| skills disponibles | inventaire Hermes local |

Rien d'autre.

## Les 4 seules variables `.env`

```env
AIKUB_TELEMETRY_BASE_URL=...
AIKUB_TELEMETRY_BOT_ID=...
AIKUB_TELEMETRY_SOURCE=...
AIKUB_TELEMETRY_API_KEY=...
```

Les bots ne doivent pas recevoir le modèle, les skills ou le display name en `.env`. Ils doivent les découvrir eux-mêmes localement.

## Event envoyé

```text
bot_inventory_snapshot
```

Via :

```text
POST <AIKUB_TELEMETRY_BASE_URL>/v1/telemetry/events
```

Avec :

```http
content-type: application/json
x-api-key: <AIKUB_TELEMETRY_API_KEY>
x-aikub-bot-id: <AIKUB_TELEMETRY_BOT_ID>
```

## Sécurité

- Aucun secret dans le repo.
- Aucun `.env` réel dans le repo.
- Le bot n'écrit jamais directement dans la DB.
- Le bot écrit seulement via l'API Aikub Telemetry.
