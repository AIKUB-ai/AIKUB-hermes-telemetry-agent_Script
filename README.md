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
run_telemetry.sh
telemetry_cron_script.md
scripts/aikub_telemetry_logger.py
scripts/aikub_telemetry_logs_incremental.py
scripts/aikub_telemetry_sessions_snapshot.py
docs/logs_contract.md
docs/sessions_contract.md
```

## Étapes envoyées par cron

Planification officielle : **cron Linux natif**, sans invocation d'agent/LLM. Depuis le dossier d'installation, utiliser uniquement :

```bash
./run_telemetry.sh install-cron
```

Cette commande installe de façon idempotente les deux entrées Linux : light à `0 */2 * * *`, sessions à `30 3 * * *` (fuseau horaire du cron système). Les runs de collecte et l'auto-update ne doivent pas créer ni modifier automatiquement les crons Hermes.

**Migration :** les anciens crons Hermes de télémétrie doivent être désactivés explicitement par l'opérateur pour éviter les doublons avec Linux. L'installation ne les modifie pas; ne pas laisser les deux planifications actives.

### Cron régulier/light, aux 2h

Le cron régulier envoie seulement :

1. inventaire bot complet light: identité, modèle/provider, crons, plugins et skills;
2. logs Hermes incrémentaux.

Il ne doit pas envoyer les sessions.

Commande officielle :

```bash
~/.hermes/aikub_telemetry_agent/run_telemetry.sh light
```

Sans argument, `run_telemetry.sh` équivaut aussi à `light`.

### Auto-update / réparation de l'agent telemetry

La stratégie officielle est : **chaque cron utilise une copie locale stable** et tente d'abord sa mise à jour depuis GitHub via le helper du runner, sans invocation d'agent. La réparation d'une installation non Git relève de ce helper, pas d'une seconde procédure shell à recopier.

Le repo contient aussi un helper idempotent :

```bash
bash scripts/aikub_telemetry_self_update.sh
```

Le script fait :

- `git fetch/reset` si le dossier est déjà un repo Git;
- réparation via `git clone` si le dossier n'est pas un repo Git;
- préserve `.env`, `logs/` et fichiers d'état locaux sans afficher leur contenu.

Si la mise à jour échoue par auth/réseau, le runner signale l'échec sans montrer de credential et continue seulement si une copie locale exploitable est disponible; sinon il s'arrête proprement. Il ne doit pas prétendre être à jour après cet échec. L'auto-update ne remplace pas l'installation explicite des crons via `install-cron`.

### Cron sessions, 1x/jour à 03:30

Le cron sessions envoie seulement `bot_sessions_snapshot` depuis `~/.hermes/state.db`. Il ne doit pas envoyer l'inventaire ni les logs.

Commande officielle :

```bash
~/.hermes/aikub_telemetry_agent/run_telemetry.sh sessions
```

Le cron Linux sessions est installé par la même commande `./run_telemetry.sh install-cron`; ne pas créer de cron Hermes supplémentaire.

### 1. Inventaire bot

Un seul événement d'inventaire est envoyé par run light. Les logs font l'objet de POST distincts, potentiellement multiples selon les chunks; le run sessions utilise également un ou plusieurs POST séparés.

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

Avant l'envoi, le script nettoie les caractères de contrôle invalides (`NUL`/`\x00` et autres bytes non imprimables) puis redactionne les secrets. Une ligne de log corrompue ne doit pas faire planter tout le batch avec un `500 INTERNAL_ERROR` côté backend.

Si un chunk est encore refusé par l'API, le script le coupe automatiquement en morceaux plus petits pour isoler la ligne fautive. Une fois rendu à une seule ligne refusée, il la met en quarantaine locale dans `~/.hermes/aikub_telemetry_state/failed_log_entries.jsonl`, jusqu'à `--max-quarantine` lignes par run, puis il continue les autres logs et avance le curseur. Ça évite qu'un parc complet de bots reste gelé sur la même ligne pendant des jours.

Si un wrapper shell local lance inventaire + logs avec `set -e`, il doit traiter l'étape logs comme non bloquante pour le reste du cron light. Exemple : `if ! python3 scripts/aikub_telemetry_logs_incremental.py --first-run-days 3 --chunk-size 250; then echo "WARN: logs snapshot failed; continuing" >&2; fi`.

Le rendu ERP doit afficher exactement :

```text
payload.logs.items[].raw
```

### 3. Sessions Hermes

Le bot renvoie à chaque run un **snapshot complet** des sessions/messages admissibles, et non un delta incrémental. Aucun curseur de sessions n'est utilisé. Le contenu de chaque message reste limité par défaut à **4000 caractères** (`--max-content-chars 4000`); « complet » décrit la sélection des sessions/messages, pas l'absence de troncature du contenu.

Source :

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
- Le bot ne lit ni n'écrit directement dans la DB ERP/telemetry. La lecture de la DB locale Hermes `~/.hermes/state.db` est autorisée pour le snapshot sessions.
- Aucun inventaire de fichiers (`fileInventory`).
- Aucun contenu de fichier arbitraire.
- Aucun plugin not enabled/inactif.
- Aucun prompt complet de cron.
- Sessions/messages envoyés seulement via le contrat `bot_sessions_snapshot`, sanitized/redacted, actifs et non vides.
- Aucun token, clé API, credential, cookie, password dans les payloads/logs.
