# AIKUB Hermes Telemetry Agent Script

Ce repo contient le **script d'instructions** que les bots Hermes/Aikub lisent quand leur cron de self-logging est actif.

Ce n'est pas un tutoriel pour installer le logging. Ce n'est pas un script qui demande au bot de recevoir toutes ses infos en `.env`.

Le but : donner au bot la marche à suivre pour documenter dans **AIKUB_Telemetry** ce que Hermes sait déjà localement.

## Fichier principal

```text
telemetry_cron_script.md
```

## Étape figée maintenant

À cette étape, le bot doit logger seulement :

| Donnée | Source |
|---|---|
| identité | les 4 variables `.env` + runtime local |
| modèle d'intelligence | config Hermes locale |
| skills disponibles | inventaire Hermes local |
| crons | `~/.hermes/cron/jobs.json`, sanitized |
| files | metadata safe seulement |

## Les 4 seules variables `.env`

```env
AIKUB_TELEMETRY_BASE_URL=...
AIKUB_TELEMETRY_BOT_ID=...
AIKUB_TELEMETRY_SOURCE=...
AIKUB_TELEMETRY_API_KEY=...
```

Les bots ne doivent pas recevoir le modèle, les skills, le display name, les crons ou les fichiers en `.env`. Ils doivent les découvrir eux-mêmes localement.

## Contrat JSON AIKUB_Telemetry

L'API accepte seulement ces champs à la racine :

```text
botId, eventType, severity, source, traceId, sessionId, payload, occurredAt
```

Donc l'inventaire doit être sous `payload` :

```json
{
  "botId": "chopchop",
  "eventType": "bot_inventory_snapshot",
  "severity": "INFO",
  "source": "chopchop",
  "occurredAt": "<date-utc>",
  "payload": {
    "identity": {},
    "model": {},
    "skillCount": 0,
    "skills": [],
    "crons": {
      "cronCount": 0,
      "cronNames": [],
      "items": []
    },
    "fileInventory": {
      "fileCount": 0,
      "returnedCount": 0,
      "skippedCount": 0,
      "roots": [],
      "byExtension": {},
      "items": []
    }
  }
}
```

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
- Le bot écrit seulement via l'API AIKUB_Telemetry.
- Aucun contenu de fichier.
- Aucun prompt complet de cron.
- Aucun chat/session complet.
- Aucun token, clé API, credential, cookie, password.
