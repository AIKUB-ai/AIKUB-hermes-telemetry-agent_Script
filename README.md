# AIKUB Hermes Telemetry Agent Script

Ce repo contient le **script d'instructions** que les bots Hermes/Aikub lisent quand leur cron de self-logging est actif.

Ce n'est pas un tutoriel pour installer le logging. Ce n'est pas un script qui demande au bot de recevoir toutes ses infos en `.env`.

Le but : donner au bot la marche à suivre pour documenter dans **AIKUB_Telemetry** ce que Hermes sait déjà localement.

## Fichiers principaux

```text
telemetry_cron_script.md
scripts/aikub_telemetry_logger.py
scripts/aikub_telemetry_logs_incremental.py
docs/logs_contract.md
```

## Étapes figées maintenant

### Étape 1 — inventaire bot

Le bot documente :

| Donnée | Source |
|---|---|
| identité | les 4 variables `.env` + runtime local |
| modèle d'intelligence | config Hermes locale |
| skills disponibles | inventaire Hermes local |
| crons | `~/.hermes/cron/jobs.json`, sanitized |
| plugins enabled | plugins actifs/visibles dans Hermes Dashboard |

### Étape 2 — logs Hermes

Le bot envoie les nouvelles lignes de :

```text
~/.hermes/logs/agent.log
```

Le script est incrémental : il garde un curseur local et envoie seulement les nouvelles lignes depuis le dernier envoi. Le ERP doit donc **append/dédupliquer** les lignes reçues et ne jamais supprimer les anciennes quand un nouveau batch arrive.

Le rendu ERP doit afficher exactement :

```text
payload.logs.items[].raw
```

Les champs parsés comme `level`, `line`, `component`, `isContinuation` servent seulement aux filtres, couleurs, tri et déduplication.

## Les 4 seules variables `.env`

```env
AIKUB_TELEMETRY_BASE_URL=...
AIKUB_TELEMETRY_BOT_ID=...
AIKUB_TELEMETRY_SOURCE=...
AIKUB_TELEMETRY_API_KEY=...
```

Les bots ne doivent pas recevoir le modèle, les skills, le display name ou les crons en `.env`. Ils doivent les découvrir eux-mêmes localement.

Les bots ne doivent plus scanner ni envoyer l'inventaire des fichiers (`fileInventory`) : trop lourd et inutile pour l'ERP à cette étape.

Les bots doivent documenter seulement les plugins Hermes **enabled/actifs**. Ils ne doivent pas envoyer le catalogue complet des plugins bundled/not enabled.

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
    "plugins": {
      "enabledCount": 0,
      "pluginCount": 0,
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
- Aucun inventaire de fichiers (`fileInventory`).
- Aucun contenu de fichier.
- Aucun plugin not enabled/inactif.
- Aucun prompt complet de cron.
- Aucun chat/session complet.
- Aucun token, clé API, credential, cookie, password.
