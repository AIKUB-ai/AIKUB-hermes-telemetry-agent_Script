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
  "timestamp": "2026-08-20T15:43:29.668Z",
  "sourceTimezone": "America/Toronto",
  "timezoneSource": "AIKUB_LOG_TIMEZONE",
  "timestampStatus": "normalized",
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

## Horodatage et fuseau source

- `timestamp` : instant UTC ISO 8601 terminé par `Z`, ou `null`; jamais une heure locale sans offset déguisée en UTC. Millisecondes conservées; microsecondes conservées si présentes.
- `sourceTimezone` : zone IANA configurée pour les heures naïves (`UTC`, `America/Toronto`), offset réellement écrit (`-04:00`, `+05:30`), ou `null` si inconnu. Un offset ne permet pas d'inventer une zone géographique.
- `timezoneSource` : `explicit_offset`, `AIKUB_LOG_TIMEZONE` ou `unknown`.
- `timestampStatus` : `normalized`, `unknown_timezone`, `ambiguous_local_time`, `nonexistent_local_time`, `invalid_timestamp` ou `missing`.
- `raw` : ligne originale avec son heure d'origine, après nettoyage/redaction habituels; ce champ n'est pas réécrit pour afficher UTC.
- Continuations : `timestamp=null`, `timestampStatus=missing`; `parentTimestamp`, `parentSourceTimezone`, `parentTimezoneSource`, `parentTimestampStatus` décrivent le dernier en-tête. Le curseur conserve ces seules métadonnées redactionnées entre runs, pas le message/brut. Pas d'héritage à travers une rotation/troncature; un ancien curseur sans parent reste valide.

Les formes `YYYY-MM-DD HH:mm:ss,SSS` et ISO avec `T`, fractions `.`/`,` facultatives, `Z`, `±HH:MM` ou `±HHMM` sont reconnues. L'offset explicite est prioritaire même si l'override configure une autre zone. La détection automatique se limite à cette preuve présente **dans la ligne concernée** : aucun offset observé sur une ligne n'est propagé aux heures naïves d'autres lignes.

Dans `payload.logs`, `sourceTimezone`/`timezoneSource` décrivent la configuration de repli pour les heures naïves (pas nécessairement toutes les lignes d'un fichier mixte). Les métadonnées **par item** font autorité; `timestampFormat=UTC_ISO8601_Z_or_null` indique le format de sortie.

Premier run : seuls les instants connus et antérieurs à la limite UTC sont exclus, avec leurs continuations. Les heures inconnues, invalides, ambiguës/inexistantes et les continuations orphelines restent transmises. Un premier run sans fuseau connu peut donc envoyer davantage que trois jours. Une heure répétée au changement d'heure d'automne ne choisit jamais arbitrairement un `fold`; une heure inexistante au printemps ne subit aucun décalage inventé.

### Déploiement pratique

1. Vérifier le formatter réellement utilisé par le processus Hermes qui écrit **ce fichier**, ainsi que son environnement de service/conteneur. Un formatter UTC explicite (`gmtime`) prouve UTC pour ce processus; un formatter local nécessite aussi de vérifier le fuseau effectif du processus à l'époque des lignes. Le `TZ` du collecteur, `/etc/localtime` de l'hôte, la localisation du bot et le fuseau des crons ou de l'affichage Hermes ne prouvent pas celui du logger. Ne pas déduire cette information d'un simple fichier source installé, qui peut différer du code exécuté ou de l'historique.
2. Seulement si ce fuseau est vérifié et cohérent sur la période collectée, ajouter au `.env` de l'installation, par exemple `AIKUB_LOG_TIMEZONE=America/Toronto` (été UTC−4, hiver UTC−5) ou `AIKUB_LOG_TIMEZONE=UTC`. Sinon laisser absent/vide. Le runner charge cette clé sans sourcer le `.env`; une valeur exportée non vide prévaut. En appel Python direct, exporter la variable explicitement : le script ne charge pas `.env` lui-même.
3. La base IANA doit être installée (`tzdata` système, ou paquet Python `tzdata` dans l'environnement utilisé). Une valeur invalide/non disponible interrompt la collecte logs avant envoi/avancement du curseur, y compris pour un fichier vide; pas de repli silencieux vers UTC. `localtime`, `posixrules`, chemins absolus et règles POSIX ne sont pas des overrides admis.
4. Si le logger a changé de zone, ne pas appliquer une nouvelle zone globale à un historique mixte naïf. Laisser ces instants inconnus ou traiter séparément une période dont la zone est prouvée. Préférer à l'avenir un formatter Hermes avec offset explicite lorsque cette configuration est supportée et vérifiée.
5. Garder le curseur existant lors de la mise à jour : aucune réémission/correction historique automatique. Ne pas supprimer l'état pour « réparer » les dates déjà ingérées. Le transport, les chunks, la quarantaine, les secrets redactionnés et le type d'événement restent inchangés. Le consommateur doit accepter `timestamp=null` et ne pas remplacer une date inconnue par `occurredAt` (heure d'envoi).

Les autres chemins ont été inspectés : l'inventaire génère `occurredAt` en UTC; les sessions convertissent les epochs SQLite avec `datetime.fromtimestamp(..., tz=timezone.utc)`. Ils ne doivent pas recevoir cet override de normalisation des logs.

Tests hors ligne, sans accès ERP ni données de production :

```bash
python3 -m unittest discover -s tests -v
```

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
