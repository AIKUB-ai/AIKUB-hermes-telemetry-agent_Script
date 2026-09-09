# AIKUB Hermes Telemetry Agent Script

Ce repo contient les scripts/runbook que chaque bot Hermes utilise pour envoyer sa telemetry vers **AIKUB_Telemetry**.

## Workflow actuel validé

Le bot a seulement besoin de :

```env
AIKUB_TELEMETRY_BASE_URL=https://aikubtelemetry-production.up.railway.app
AIKUB_TELEMETRY_BOTOPS_TOKEN=<token du bot provenant de Vault>
```

Compatibilité : les scripts acceptent encore `AIKUB_TELEMETRY_API_KEY` comme alias legacy du token, pour ne pas briser les bots déjà installés. Le nom recommandé est maintenant `AIKUB_TELEMETRY_BOTOPS_TOKEN`.

Le bot POST vers :

```text
<AIKUB_TELEMETRY_BASE_URL>/v1/telemetry/events
```

avec :

```http
content-type: application/json
x-aikub-botops-token: <AIKUB_TELEMETRY_BOTOPS_TOKEN>
```

Le script **n’envoie plus** `x-api-key` ni `x-aikub-bot-id`. L’API AIKUB_Telemetry valide le token via Vault, associe le bot côté serveur, puis écrit en DB telemetry.

## Fichiers principaux

```text
telemetry_cron_script.md
scripts/aikub_telemetry_logger.py
scripts/aikub_telemetry_logs_incremental.py
scripts/aikub_telemetry_sessions_snapshot.py
docs/logs_contract.md
docs/sessions_contract.md
```

## Étapes envoyées par cron

### Cron régulier/light, aux 2h

Le cron régulier envoie seulement :

1. inventaire bot;
2. logs Hermes incrémentaux.

Il ne doit pas envoyer les sessions.

### Cron sessions, 1x/jour à 03:30

Le cron sessions envoie seulement `bot_sessions_snapshot` depuis `~/.hermes/state.db`. Il ne doit pas envoyer l'inventaire ni les logs.

À l'auto-update, le bot doit vérifier qu'un cron nommé `AIKUB Telemetry Sessions` existe. S'il est absent, il doit le créer une seule fois :

```bash
hermes cron create '30 3 * * *' \
  --name 'AIKUB Telemetry Sessions' \
  --deliver local \
  "Auto-update le repo/script AIKUB telemetry, puis exécute seulement scripts/aikub_telemetry_sessions_snapshot.py pour envoyer bot_sessions_snapshot. Ne lance pas l'inventaire ni logs dans ce cron."
```

Ne jamais créer de doublon si ce nom existe déjà.

### 1. Inventaire bot

Le bot documente localement :

| Donnée | Source |
|---|---|
| identité | token résolu côté API + runtime local; `AIKUB_TELEMETRY_BOT_ID` optionnel si présent |
| modèle d'intelligence | config Hermes locale |
| skills disponibles | inventaire Hermes local |
| crons | `~/.hermes/cron/jobs.json`, sanitized |
| plugins enabled | plugins actifs/visibles dans Hermes Dashboard |

### 2. Logs Hermes

Le bot envoie les nouvelles lignes de :

```text
~/.hermes/logs/agent.log
```

Le script est incrémental : il garde un curseur local et envoie seulement les nouvelles lignes depuis le dernier envoi. Le ERP doit append/dédupliquer les lignes reçues et ne jamais supprimer les anciennes quand un nouveau batch arrive.

Le rendu ERP doit afficher exactement :

```text
payload.logs.items[].raw
```

### 3. Sessions Hermes

Le bot envoie les sessions/messages utiles depuis :

```text
~/.hermes/state.db
```

Recette figée pour le ERP :

```text
- eventType: bot_sessions_snapshot
- sessions avec au moins 1 message actif non vide et non compacted
- messages active=1 avec content non vide
- pas les sessions à 0 message
- pas les messages compacted/inactive
- transport en chunks si nécessaire
```

Le ERP doit regrouper les chunks par :

```text
payload.sessions.batchId
```

Et dédupliquer :

```text
sessions: botId + sessionId
messages: botId + sessionId + messageId
```

Même si le bot n’envoie plus `botId` à la racine, le backend peut utiliser l’identité bot résolue depuis le token Vault pour compléter/dédupliquer côté serveur.

## Variables `.env`

Requises :

```env
AIKUB_TELEMETRY_BASE_URL=...
AIKUB_TELEMETRY_BOTOPS_TOKEN=...
```

Optionnelles :

```env
AIKUB_TELEMETRY_BOT_ID=...
AIKUB_TELEMETRY_SOURCE=hermes
AIKUB_TELEMETRY_API_KEY=... # alias legacy du token; éviter pour les nouvelles installs
```

Les bots ne doivent pas recevoir le modèle, les skills, le display name ou les crons en `.env`. Ils doivent les découvrir eux-mêmes localement.

Les bots ne doivent plus scanner ni envoyer l'inventaire des fichiers (`fileInventory`) : trop lourd et inutile pour l'ERP à cette étape.

## Contrat JSON AIKUB_Telemetry

L'API accepte seulement ces champs à la racine :

```text
eventType, severity, source, traceId, sessionId, payload, occurredAt
```

`botId` est optionnel/legacy côté script. Avec le workflow Vault, l’API doit résoudre le bot à partir de `x-aikub-botops-token`.

Exemple inventory :

```json
{
  "eventType": "bot_inventory_snapshot",
  "severity": "INFO",
  "source": "hermes",
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

## Sécurité

- Aucun secret dans le repo.
- Aucun `.env` réel dans le repo.
- Aucun token de bot dans les env vars du service AIKUB_Telemetry/Railway.
- Les vrais tokens bot restent dans Vault; le bot possède seulement son propre token client.
- Le bot écrit seulement via l’API AIKUB_Telemetry.
- Le bot n’écrit jamais directement dans la DB.
- Aucun inventaire de fichiers (`fileInventory`).
- Aucun contenu de fichier arbitraire.
- Aucun plugin not enabled/inactif.
- Aucun prompt complet de cron.
- Sessions/messages envoyés seulement via le contrat `bot_sessions_snapshot`, sanitized/redacted, actifs et non vides.
- Aucun token, clé API, credential, cookie, password dans les payloads/logs.
