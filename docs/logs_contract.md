# Contrat figé - Hermes agent.log vers AIKUB Telemetry

Ce document fige la section **logs** du script AIKUB Hermes Telemetry Agent.

Objectif : envoyer les nouvelles lignes de `~/.hermes/logs/agent.log` à AIKUB Telemetry pour que le ERP puisse afficher un viewer de logs comme le dashboard Hermes.

Exécution planifiée : cron Linux natif light à `0 */2 * * *`, installé uniquement via `./run_telemetry.sh install-cron`, sans invocation d'agent. Le cron sessions séparé est décrit dans `sessions_contract.md`. Lors d'une migration, l'opérateur doit désactiver explicitement les anciens crons Hermes pour éviter les doublons; aucun cron Hermes n'est créé ou modifié automatiquement.

## Principe important

Le bot envoie les logs comme des lignes brutes Hermes. Le ERP doit afficher :

```text
payload.logs.items[].raw
```

Le ERP peut utiliser les champs parsés (`level`, `component`, `timestamp`, etc.) pour filtrer/colorer, mais il ne doit pas reconstruire ni transformer la ligne affichée.

## Source locale

```text
~/.hermes/logs/agent.log
```

## Variables `.env`

Même contrat que l'inventaire : URL + token bot seulement.

```env
AIKUB_TELEMETRY_BASE_URL=...
AIKUB_TELEMETRY_BOTOPS_TOKEN=...
```

`AIKUB_TELEMETRY_API_KEY` reste accepté comme alias legacy du token. `AIKUB_TELEMETRY_BOT_ID` et `AIKUB_TELEMETRY_SOURCE` sont optionnels.

Aucun secret ne doit être affiché ou committé.

## Nettoyage anti-500

Avant l'envoi, chaque ligne doit être nettoyée comme dans le script sessions :

- supprimer les bytes `NUL` / `\x00`;
- remplacer les autres caractères de contrôle non imprimables par un espace;
- garder seulement `\n`, `\r`, `\t` parmi les caractères de contrôle;
- appliquer ensuite la redaction des secrets.

But : éviter qu'une seule ligne corrompue de `agent.log` fasse répondre `500 INTERNAL_ERROR` au backend et laisse le curseur logs bloqué sur le même chunk.

## Isolation/quarantaine anti-gel

Si un chunk est encore refusé par l'API après nettoyage, le script doit :

1. couper le chunk en deux;
2. réessayer chaque moitié;
3. répéter jusqu'à isoler une seule ligne fautive;
4. écrire cette ligne déjà redactionnée dans `~/.hermes/aikub_telemetry_state/failed_log_entries.jsonl`;
5. continuer avec les autres lignes;
6. avancer le curseur seulement si le run est complété ou si les seules erreurs restantes ont été quarantinées.

Protection : `--max-quarantine` limite le nombre de lignes isolées par run. Si trop de lignes échouent, le script stoppe sans avancer le curseur pour éviter de masquer une panne backend générale.

## Endpoint

```text
POST <AIKUB_TELEMETRY_BASE_URL>/v1/telemetry/events
```

Headers :

```http
content-type: application/json
x-aikub-botops-token: ***
```

Ne pas envoyer `x-api-key` ni `x-aikub-bot-id`; l'API associe le bot via Vault.

## Event actuel

Pour compatibilité avec l'API/ERP actuel, les logs sont envoyés sous :

```text
eventType: bot_inventory_snapshot
```

Les vraies données de logs sont dans :

```text
payload.logs
```

Si AIKUB Telemetry ajoute plus tard un event type dédié, le nom recommandé sera :

```text
bot_log_incremental
```

Mais tant que le ERP consomme le contrat actuel, ne pas changer l'event type sans migration coordonnée.

Le run light envoie un seul événement d'inventaire, puis des POST logs distincts, potentiellement multiples selon les chunks et les réessais. L'utilisation du même event type ne signifie donc pas qu'un seul POST couvre inventaire et logs.

## Payload logs

Structure figée :

```json
{
  "payload": {
    "identity": {},
    "model": {},
    "logs": {
      "source": "hermes_agent_log_file",
      "logName": "agent.log",
      "path": "~/.hermes/logs/agent.log",
      "batchId": "agent-log-incremental-<utc>",
      "chunkIndex": 1,
      "chunkCount": 1,
      "transportChunkingOnly": true,
      "dedupeStrategy": "persistent_file_cursor_offset_inode",
      "mode": "incremental_since_cursor",
      "lineStart": 5680,
      "lineEnd": 5729,
      "returnedCount": 50,
      "totalReturnedAcrossChunks": 50,
      "redactionApplied": true,
      "items": []
    }
  }
}
```

## Item de log

Chaque item représente une ligne logique de `agent.log`.

```json
{
  "line": 5729,
  "timestamp": "2026-08-20 11:43:29,668",
  "level": "INFO",
  "component": "agent.tool_executor",
  "message": "tool terminal completed...",
  "raw": "2026-08-20 11:43:29,668 INFO [session] agent.tool_executor: tool terminal completed...",
  "parsed": true,
  "isContinuation": false,
  "parentLine": null,
  "parentTimestamp": null,
  "parentLevel": null,
  "parentComponent": null
}
```

### Champs importants pour le ERP

- `raw` : texte exact à afficher dans le terminal log viewer.
- `level` : seulement pour couleur/filtre.
- `line` : numéro de ligne dans `agent.log`, utilisé pour déduplication et tri.
- `isContinuation` : true quand la ligne ne commence pas par timestamp/level.
- `parentLine`, `parentLevel`, `parentComponent` : aident à rattacher une continuation line à la vraie ligne précédente.

## Déduplication / append

Le script est incrémental : il sauvegarde un curseur local et envoie seulement les nouvelles lignes appendées depuis le dernier envoi.

Côté ERP, il ne faut jamais supprimer les anciens logs quand un nouveau batch arrive.

Règle recommandée :

```text
dedupe key = botId + logName + line
```

Quand un nouveau batch arrive :

1. insérer les lignes jamais vues;
2. ignorer les doublons si la même clé existe déjà;
3. ne pas effacer les anciennes lignes;
4. afficher les dernières N lignes selon le dropdown UI.

## Rendu ERP attendu

Afficher `raw` tel quel.

Filtres :

```text
All | Debug | Info | Warning | Error
```

Couleurs :

```text
DEBUG = gris
INFO = blanc/gris clair
WARNING = jaune/orange
ERROR = rouge
CRITICAL/FATAL = rouge fort
Continuation = gris pâle ou couleur du parent
```

CSS recommandé :

```css
.log-container {
  overflow-x: auto;
}

.log-line {
  white-space: pre;
  font-family: monospace;
}
```

À éviter :

```css
white-space: normal;
word-break: break-word;
overflow-wrap: anywhere;
```

## Sécurité

Le script applique une redaction de patterns secrets avant l'envoi :

- api key;
- token;
- password/passwd;
- secret;
- authorization/bearer;
- cookie;
- credentials dans URL.

Dans le payload logs, le bot ne doit jamais envoyer :

- `.env` complet;
- clés API;
- tokens;
- cookies;
- secrets;
- prompts complets;
- contenu de fichiers arbitraires;
- sessions complètes.

Le snapshot sessions relève exclusivement du contrat `bot_sessions_snapshot` et du cron quotidien séparé. Tout envoi vers l'ERP passe par l'API, jamais par un accès direct à sa DB; la lecture locale de `state.db` Hermes reste autorisée pour ce snapshot.

## Commandes

Dry-run :

```bash
python3 scripts/aikub_telemetry_logs_incremental.py --dry-run --chunk-size 50
```

Envoi réel incrémental :

```bash
python3 scripts/aikub_telemetry_logs_incremental.py --chunk-size 50
```

Premier run contrôlé pour test sans doublons, en sautant les lignes déjà vues :

```bash
python3 scripts/aikub_telemetry_logs_incremental.py --baseline-line <last-line-already-imported> --chunk-size 50
```
