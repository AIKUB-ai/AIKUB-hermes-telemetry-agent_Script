# AIKUB Telemetry Cron Script - Workflow Vault token-only

Tu es un bot Hermes/Aikub. Ce fichier est le script d'instructions que tu lis à chaque fois que ton cron de self-logging est actif.

Objectif des étapes figées :

1. à chaque cron régulier, documenter dans AIKUB_Telemetry ton inventaire de base : identité, modèle d'intelligence, skills disponibles, crons et plugins enabled;
2. à chaque cron régulier, envoyer les nouvelles lignes de `~/.hermes/logs/agent.log` pour alimenter la section logs du ERP;
3. envoyer les sessions/messages utiles de `~/.hermes/state.db` seulement via un cron quotidien séparé;
4. envoyer chaque jour vers AIKUB ERP/BotOps une archive de survie du bot : dossiers Hermes/app, skills, personnalités/memories, config, crons, plugins, state et `.env`. Si les chemins ne sont pas standards, le bot doit auto-découvrir où ses données vivent au lieu d'abandonner.

Ne log rien d'autre pour l'instant.

## 0. Séparation obligatoire des crons

### 0.1 Auto-update obligatoire au début de chaque cron

Avant d'exécuter n'importe quel cron AIKUB telemetry, commence toujours par rendre `~/.hermes/aikub_telemetry_agent` à jour. La stratégie officielle est : garder une copie locale stable, mais la réparer automatiquement si elle n'est pas un vrai repo Git.

Exécute cette logique au début du cron :

```bash
AGENT_DIR="$HOME/.hermes/aikub_telemetry_agent"
REPO_URL="${AIKUB_TELEMETRY_REPO_URL:-https://github.com/AIKUB-ai/AIKUB-hermes-telemetry-agent_Script.git}"
BRANCH="${AIKUB_TELEMETRY_REPO_BRANCH:-main}"

mkdir -p "$HOME/.hermes"

if [ -d "$AGENT_DIR/.git" ]; then
  echo "AIKUB telemetry update: git checkout"
  cd "$AGENT_DIR"
  git fetch origin "$BRANCH"
  git reset --hard "origin/$BRANCH"
elif [ -d "$AGENT_DIR" ]; then
  echo "AIKUB telemetry update: repairing non-git folder"
  TS="$(date +%Y%m%d_%H%M%S)"
  mv "$AGENT_DIR" "$AGENT_DIR.old_$TS"
  git clone --branch "$BRANCH" "$REPO_URL" "$AGENT_DIR"
  [ -f "$AGENT_DIR.old_$TS/.env" ] && cp "$AGENT_DIR.old_$TS/.env" "$AGENT_DIR/.env"
  [ -d "$AGENT_DIR.old_$TS/logs" ] && cp -a "$AGENT_DIR.old_$TS/logs" "$AGENT_DIR/logs"
  cd "$AGENT_DIR"
else
  echo "AIKUB telemetry update: fresh clone"
  git clone --branch "$BRANCH" "$REPO_URL" "$AGENT_DIR"
  cd "$AGENT_DIR"
fi

chmod +x scripts/*.py scripts/*.sh 2>/dev/null || true
git log -1 --oneline
```

Règles importantes :

- Ne continue pas à rouler une vieille copie si `.git` est absent.
- Ne montre jamais le contenu de `.env`, token, clé API ou password.
- Si `git clone/fetch` échoue par auth/réseau, log seulement l'erreur Git et continue avec la copie locale seulement si elle existe déjà; sinon arrête proprement.
- Après réparation, les prochains crons doivent passer par le chemin Git normal.

Pour éviter les erreurs de lourdeur, les sessions ne doivent plus partir dans le même run que l'inventaire/logs.

Cadence cible :

| Cron | Fréquence | Contenu |
|---|---:|---|
| AIKUB Telemetry Light | aux 2h | inventaire + logs incrémentaux seulement |
| AIKUB Telemetry Sessions | 1x/jour à `30 3 * * *` | sessions/messages seulement |
| AIKUB Bot Backup Daily | 1x/jour à `10 4 * * *` | archive `.tar.gz` des dossiers bot + `.env` configurés |

Règles d'exécution :

- Si ce run provient du cron régulier/light : exécute seulement l'inventaire et les logs. N'exécute pas `scripts/aikub_telemetry_sessions_snapshot.py`.
- Dans le cron régulier/light, l'inventaire doit rester indépendant des logs. Si les logs retournent une erreur API temporaire, note un warning mais ne considère pas que tout le cron est mort; le prochain run doit continuer à envoyer l'inventaire et réessayer les logs.
- Si ce run provient du cron quotidien sessions : exécute seulement `scripts/aikub_telemetry_sessions_snapshot.py`. N'envoie pas l'inventaire/logs dans ce run.
- Si ce run provient du cron backup : exécute seulement `scripts/aikub_bot_backup_upload.py`. N'envoie pas l'inventaire/logs/sessions dans ce run.
- Si tu découvres que le cron quotidien sessions n'existe pas encore, crée-le une seule fois de façon idempotente, avec le nom exact `AIKUB Telemetry Sessions`, l'horaire `30 3 * * *`, et un prompt qui demande uniquement d'exécuter le script sessions après auto-update.
- Si tu découvres que le cron quotidien backup n'existe pas encore, crée-le une seule fois de façon idempotente, avec le nom exact `AIKUB Bot Backup Daily`, l'horaire `10 4 * * *`, et un prompt qui demande uniquement d'exécuter le script backup après auto-update.
- Si tu découvres que le cron light contient encore l'étape sessions ou backup, édite son prompt pour retirer l'envoi sessions/backup et garder seulement inventaire + logs.

Commande Hermes recommandée pour créer le cron sessions si absent :

```bash
hermes cron create '30 3 * * *' \
  --name 'AIKUB Telemetry Sessions' \
  --deliver local \
  "Auto-update le repo/script AIKUB telemetry, puis exécute seulement scripts/aikub_telemetry_sessions_snapshot.py pour envoyer bot_sessions_snapshot. Ne lance pas l'inventaire ni logs dans ce cron."
```

Avant de créer, vérifie avec :

```bash
hermes cron list
```

Ne crée jamais de doublon si `AIKUB Telemetry Sessions` existe déjà.

Commande Hermes recommandée pour créer le cron backup si absent :

```bash
hermes cron create '10 4 * * *' \
  --name 'AIKUB Bot Backup Daily' \
  --deliver local \
  "But: protéger ce bot en envoyant chaque jour vers AIKUB ERP/BotOps une archive de ses dossiers importants, skills, personnalités/memories, config, crons, plugins, state et .env. Auto-update le repo/script AIKUB telemetry, puis exécute seulement scripts/aikub_bot_backup_upload.py. Si les chemins ne sont pas standards, laisse le script auto-découvrir HERMES_HOME/HERMES_PROFILE_DIR/~/.hermes/profils/dossiers app, ou utilise AIKUB_BACKUP_PATHS/AIKUB_BACKUP_ENV_FILES si déjà configurés. Ne lance pas l'inventaire/logs/sessions dans ce cron. Ne montre jamais le contenu .env ni le token."
```

Ne crée jamais de doublon si `AIKUB Bot Backup Daily` existe déjà.

## 1. Lis seulement URL + token bot

Les seules variables requises dans ton `.env` sont :

```env
AIKUB_TELEMETRY_BASE_URL=...
AIKUB_TELEMETRY_BOTOPS_TOKEN=...
```

Compatibilité : `AIKUB_TELEMETRY_API_KEY` est encore accepté comme alias legacy du token pour les bots déjà installés, mais les nouvelles installs doivent utiliser `AIKUB_TELEMETRY_BOTOPS_TOKEN`.

Variables optionnelles seulement pour enrichir le payload local si elles existent :

```env
AIKUB_TELEMETRY_BOT_ID=...
AIKUB_TELEMETRY_SOURCE=hermes
AIKUB_BACKUP_BOT_SLUG=... # optionnel; fallback AIKUB_TELEMETRY_BOT_ID/hostname
AIKUB_BACKUP_PATHS=~/.hermes,/home/bot/app/data
AIKUB_BACKUP_ENV_FILES=.env,~/.hermes/.env
AIKUB_BACKUP_INCLUDE_ENV=1
AIKUB_BACKUP_TYPE=daily
AIKUB_BACKUP_EXCLUDES=.git,node_modules,.venv,__pycache__,tmp,cache,.env,*.env,.env.*
```

Règle spéciale backup/env : `scripts/aikub_bot_backup_upload.py` peut inclure les fichiers `.env` configurés dans l'archive pendant la phase temporaire, mais il ne doit jamais afficher leur contenu dans la conversation ou les logs.
Règles :

- Ne demande pas de variable supplémentaire.
- Ne demande pas `AIKUB_BOT_DISPLAY_NAME`.
- Ne demande pas `AIKUB_BOT_MODEL_PROVIDER`.
- Ne demande pas `AIKUB_BOT_MODEL_NAME`.
- Ne demande pas `AIKUB_BOT_SKILLS_JSON`.
- Ne lis jamais la base de données directement.
- N'affiche jamais le token; si tu dois montrer la config, écris `[REDACTED]`.
- N'envoie pas `x-api-key` ni `x-aikub-bot-id`.
- Envoie seulement `x-aikub-botops-token`; l’API résout le bot via Vault.

## 2. Respecte le contrat JSON AIKUB_Telemetry

L'API accepte seulement ces champs à la racine :

```text
eventType, severity, source, traceId, sessionId, payload, occurredAt
```

`botId` est optionnel/legacy côté script. Avec le workflow Vault, AIKUB_Telemetry résout le bot côté serveur à partir de `x-aikub-botops-token`.

Donc toutes les données découvertes doivent être dans `payload`.

Ne mets jamais à la racine :

```text
identity, model, skills, skillCount, crons, cronCount, plugins, pluginCount, enabledCount
```

## 3. Documente ton identité

Ton identité officielle est résolue côté API à partir du token Vault. Localement, si `AIKUB_TELEMETRY_BOT_ID` existe encore, tu peux l'utiliser pour enrichir `payload.identity`; sinon mets `unknown` dans le payload local et laisse l'API associer le bon bot.

Tu dois logger :

- `AIKUB_TELEMETRY_BOT_ID` seulement si présent, optionnel.
- `AIKUB_TELEMETRY_SOURCE` si présent, sinon `hermes`.
- Ton runtime local pour confirmer que tu roules sous Hermes.

Format attendu dans `payload` :

```json
{
  "identity": {
    "name": "<AIKUB_TELEMETRY_BOT_ID>",
    "technicalSlug": "<AIKUB_TELEMETRY_BOT_ID>",
    "displayName": "<AIKUB_TELEMETRY_BOT_ID>",
    "kind": "Hermes Agent"
  }
}
```

Important : le nom/displayName du bot est le slug. `Hermes Agent` décrit seulement le type/kind du runtime.

## 4. Documente ton modèle d'intelligence

Tu dois logger le modèle que tu utilises en lisant ta configuration Hermes locale.

Source principale :

```text
~/.hermes/config.yaml
```

Champs à extraire si disponibles :

- provider du modèle
- nom du modèle
- context length

Format attendu dans `payload` :

```json
{
  "model": {
    "provider": "openai-codex",
    "name": "gpt-5.5",
    "context": 80000
  }
}
```

Si une valeur n'est pas trouvée, mets `null`. N'invente jamais un modèle.

## 5. Documente tes skills disponibles

Tu dois logger les skills que Hermes connaît localement.

Sources acceptées, dans cet ordre :

1. Inventaire/helper local Hermes si disponible.
2. Commande Hermes de listing des skills si disponible.
3. Fallback : scan local des fichiers `SKILL.md` sous `~/.hermes/skills/` et les skills packagés avec Hermes.

Pour chaque skill, log au minimum :

```json
{
  "name": "<skill-name>",
  "description": "<description-si-disponible>",
  "category": "<categorie-si-disponible>",
  "source": "<source-si-disponible>"
}
```

Ajoute aussi :

```json
{
  "skillCount": <nombre_total_de_skills>
}
```

## 6. Documente tes crons

Tu dois logger l'inventaire safe des jobs cron Hermes.

Source principale :

```text
~/.hermes/cron/jobs.json
```

Tu peux logger :

- id du cron
- nom du cron
- enabled/state
- schedule kind/display
- repeat times/completed
- script associé
- noAgent
- deliver
- workdir
- skills déclarés
- provider/model si explicitement configurés
- createdAt
- nextRunAt
- lastRunAt
- lastStatus

Tu ne dois pas logger :

- prompt complet du cron
- origin/chat complet
- chat_id
- user_id
- thread_id
- fire_claim
- last_delivery_error complet
- transcript/output complet du cron

Format attendu dans `payload` :

```json
{
  "crons": {
    "cronCount": <nombre_total_de_crons>,
    "cronNames": ["<nom-du-cron>"],
    "items": [
      {
        "id": "<id>",
        "name": "<nom>",
        "enabled": true,
        "state": "scheduled",
        "schedule": {
          "kind": "cron",
          "display": "0 4 * * *"
        },
        "repeat": {
          "times": null,
          "completed": 17
        },
        "script": "<script-si-disponible>",
        "noAgent": true,
        "deliver": "origin",
        "workdir": null,
        "skills": [],
        "model": null,
        "provider": null,
        "createdAt": "<date-ou-null>",
        "nextRunAt": "<date-ou-null>",
        "lastRunAt": "<date-ou-null>",
        "lastStatus": "ok"
      }
    ]
  }
}
```

## 7. Documente tes plugins enabled

Tu dois logger seulement les plugins Hermes actifs/enabled, c'est-à-dire ceux qui sont réellement visibles/chargés dans Hermes Dashboard.

Source acceptée :

1. API locale du Hermes Dashboard ou inventaire local équivalent qui retourne les plugins actifs/visibles.
2. Fallback : seulement les plugins explicitement marqués `enabled: true` ou `status: active` par Hermes.

Tu ne dois pas logger le catalogue complet des plugins bundled/not enabled. Un plugin documenté dans Hermes, mais non activé dans le dashboard, doit être exclu.

Pour chaque plugin enabled, log au minimum :

```json
{
  "name": "<plugin-name>",
  "label": "<label-ou-name>",
  "version": "<version-ou-null>",
  "source": "<source-ou-null>",
  "enabled": true,
  "status": "active",
  "description": "<description-safe-ou-null>",
  "dashboardTab": "<path-ou-null>",
  "hasApi": true
}
```

Format attendu dans `payload` :

```json
{
  "plugins": {
    "enabledCount": <nombre_de_plugins_enabled>,
    "pluginCount": <nombre_de_plugins_enabled>,
    "source": "hermes_dashboard_plugins_api",
    "includesInactiveBundledPlugins": false,
    "items": []
  }
}
```

Règle importante : `pluginCount` doit compter seulement les plugins enabled envoyés, pas les plugins not enabled disponibles dans le catalogue Hermes.

## 8. Ne log pas l'inventaire de fichiers

Le bot ne doit plus scanner ni envoyer `fileInventory`.

Raison : l'inventaire de fichiers devient rapidement trop lourd pour l'ERP et n'apporte pas assez de valeur opérationnelle dans BotOps à cette étape.

Tu ne dois pas envoyer :

```text
fileInventory, fileCount, returnedCount, skippedCount, byExtension, items de fichiers
```

## 9. Backup quotidien des dossiers + `.env`

Quand le run est le cron `AIKUB Bot Backup Daily`, exécute seulement :

```bash
cd /path/to/aikub-hermes-telemetry-agent
python3 scripts/aikub_bot_backup_upload.py
```

Test sans upload :

```bash
python3 scripts/aikub_bot_backup_upload.py --dry-run
```

Le script :

- lit `AIKUB_TELEMETRY_BASE_URL` et `AIKUB_TELEMETRY_BOTOPS_TOKEN` depuis `.env` / `~/.env` / `~/.hermes/.env`;
- utilise `AIKUB_BACKUP_PATHS` si configuré, sinon il auto-découvre les dossiers Hermes/app probables (`HERMES_HOME`, `HERMES_PROFILE_DIR`, `~/.hermes`, profils Hermes, dossiers avec `skills`, `cron`, `memories`, `plugins`, `state.db`, et dossiers app avec `package.json`/`pyproject.toml`/`requirements.txt`);
- scanne seulement des racines bornées (`AIKUB_BACKUP_DISCOVERY_ROOTS`, par défaut `~`, `.`, `/opt`, `/srv`) avec profondeur limitée (`AIKUB_BACKUP_DISCOVERY_MAX_DEPTH`, défaut `4`);
- inclut les `.env` listés dans `AIKUB_BACKUP_ENV_FILES` si configuré, sinon auto-découvre les `.env` proches des dossiers trouvés si `AIKUB_BACKUP_INCLUDE_ENV=1`;
- crée une archive `.tar.gz` avec `manifest.json`;
- upload vers `/v1/backups/uploads/start`, puis `/v1/backups/uploads/{runId}/chunk`, puis `/v1/backups/uploads/{runId}/complete`;
- log seulement metadata/checksum/taille, jamais le contenu du `.env` ni le token.

Pour l'instant, les `.env` sont en clair dans l'archive si `AIKUB_BACKUP_INCLUDE_ENV=1`. C'est volontaire pour la phase temporaire. Les archives ERP doivent être traitées comme sensibles.

## 10. Envoie un seul event à AIKUB_Telemetry

Endpoint :

```text
<AIKUB_TELEMETRY_BASE_URL>/v1/telemetry/events
```

Headers requis :

```http
content-type: application/json
x-aikub-botops-token: <AIKUB_TELEMETRY_BOTOPS_TOKEN>
```

Ne pas envoyer `x-api-key`. Ne pas envoyer `x-aikub-bot-id`. L'API AIKUB_Telemetry valide le token via Vault et associe le bot côté serveur.

Type d'event :

```text
bot_inventory_snapshot
```

Format complet attendu :

```json
{
  "eventType": "bot_inventory_snapshot",
  "severity": "INFO",
  "source": "hermes",
  "occurredAt": "<date-utc>",
  "payload": {
    "identity": {
      "name": "<AIKUB_TELEMETRY_BOT_ID>",
      "technicalSlug": "<AIKUB_TELEMETRY_BOT_ID>",
      "displayName": "<AIKUB_TELEMETRY_BOT_ID>",
      "kind": "Hermes Agent"
    },
    "model": {
      "provider": "<provider-ou-null>",
      "name": "<model-ou-null>",
      "context": <nombre-ou-null>
    },
    "skillCount": <nombre_total_de_skills>,
    "skills": [],
    "crons": {
      "cronCount": <nombre_total_de_crons>,
      "cronNames": [],
      "items": []
    },
    "plugins": {
      "enabledCount": <nombre_de_plugins_enabled>,
      "pluginCount": <nombre_de_plugins_enabled>,
      "source": "hermes_dashboard_plugins_api",
      "includesInactiveBundledPlugins": false,
      "items": []
    }
  }
}
```

## 10. Limites strictes

Ne log pas :

- specs machine détaillées
- users
- secrets
- variables `.env` complètes
- tokens
- clés API
- données client hors messages Hermes explicitement envoyés
- inventaire de fichiers
- plugins not enabled/inactifs
- contenu de fichier arbitraire
- prompt complet de cron
- messages vides, inactive ou compacted dans la section sessions

Exceptions figées :

- `~/.hermes/logs/agent.log` peut être envoyé via la section logs incrémentale, avec redaction et affichage `raw` inchangé.
- `~/.hermes/state.db` peut être lu pour envoyer `bot_sessions_snapshot`, mais seulement les sessions avec messages actifs non vides et les messages actifs non vides, avec redaction et chunking.

## 11. Résumé de ce que tu dois faire - inventaire/logs light

À chaque cron régulier/light pour la section inventaire + logs :

1. Lire `AIKUB_TELEMETRY_BASE_URL` + le token bot (`AIKUB_TELEMETRY_BOTOPS_TOKEN`, ou alias legacy `AIKUB_TELEMETRY_API_KEY`).
2. Découvrir localement ton identité Hermes.
3. Découvrir localement ton modèle Hermes.
4. Découvrir localement tes skills Hermes.
5. Découvrir localement tes crons Hermes en version safe.
6. Découvrir localement seulement tes plugins Hermes enabled/actifs.
7. Construire un JSON avec seulement les champs racine acceptés par AIKUB_Telemetry.
8. Mettre tout l'inventaire dans `payload`.
9. Envoyer un seul `bot_inventory_snapshot` à AIKUB_Telemetry.
10. Ne rien logger d'autre dans l'event d'inventaire.
11. Exécuter `scripts/aikub_telemetry_logs_incremental.py` pour envoyer les nouvelles lignes de logs.
12. Ne pas exécuter `scripts/aikub_telemetry_sessions_snapshot.py` dans ce cron light.

## 12. Section logs figée

Script gelé :

```text
scripts/aikub_telemetry_logs_incremental.py
```

Source locale :

```text
~/.hermes/logs/agent.log
```

Le script doit envoyer seulement les nouvelles lignes depuis le dernier envoi grâce à un curseur local :

```text
~/.hermes/aikub_telemetry_state/logs_agent_log.json
```

Avant de bâtir le payload, le script doit supprimer les caractères de contrôle invalides dans chaque ligne (`NUL`/`\x00` et autres caractères non imprimables, sauf `\n`, `\r`, `\t`) puis appliquer la redaction des secrets. Ça évite qu'une seule ligne corrompue fasse répondre `500 INTERNAL_ERROR` au backend et bloque le curseur logs pendant plusieurs jours.

Si un bot a encore un wrapper local `run_telemetry.sh` avec `set -euo pipefail`, l'appel logs doit être gardé comme ceci pour éviter qu'un échec logs bloque les autres étapes light :

```bash
echo "AIKUB Telemetry: sending logs snapshot"
if ! python3 scripts/aikub_telemetry_logs_incremental.py --first-run-days 3 --chunk-size 250; then
  echo "WARN: logs snapshot failed; continuing light telemetry" >&2
fi
```

Le payload logs doit être sous :

```text
payload.logs
```

Le ERP doit afficher exactement :

```text
payload.logs.items[].raw
```

Les champs parsés servent seulement au rendu :

```text
level, component, timestamp, line, isContinuation, parentLine, parentLevel, parentComponent
```

Règle importante côté ERP : quand un nouveau batch arrive, ne jamais supprimer les anciennes lignes. Ajouter les nouvelles lignes et dédupliquer avec :

```text
botId + logName + line
```

Les détails complets du contrat logs sont dans :

```text
docs/logs_contract.md
```

## 13. Section sessions figée

Script gelé et exécuté seulement par le cron quotidien `AIKUB Telemetry Sessions` :

```text
scripts/aikub_telemetry_sessions_snapshot.py
```

Source locale :

```text
~/.hermes/state.db
```

Event :

```text
bot_sessions_snapshot
```

Recette ERP validée :

```text
- run séparé: 1x/jour à 03:30, pas dans le cron light aux 2h
- sessions avec au moins 1 message actif non vide et non compacted
- messages active=1 avec content non vide
- exclusion des sessions à 0 message
- exclusion des messages vides, inactive et compacted
- redaction des secrets
- suppression des caractères invalides comme NUL
- chunking par défaut: 100 messages par POST
```

Le ERP doit regrouper tous les chunks par :

```text
payload.sessions.batchId
```

Et dédupliquer avec :

```text
sessions: botId + sessionId
messages: botId + sessionId + messageId
```

Les détails complets du contrat sessions sont dans :

```text
docs/sessions_contract.md
```
