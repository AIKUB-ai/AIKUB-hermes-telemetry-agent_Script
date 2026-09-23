# AIKUB Telemetry Cron Script - Workflow Vault token-only

Ce fichier décrit le contrat de collecte Hermes/Aikub exécuté par les scripts sous cron Linux natif. Ce n'est pas un prompt à exécuter par un agent à chaque déclenchement.

Objectif des étapes figées :

1. à chaque cron régulier, documenter dans AIKUB_Telemetry ton inventaire de base : identité, modèle d'intelligence, skills disponibles, crons et plugins enabled;
2. à chaque cron régulier, envoyer les nouvelles lignes de `~/.hermes/logs/agent.log` pour alimenter la section logs du ERP;
3. envoyer les sessions/messages utiles de `~/.hermes/state.db` seulement via un cron quotidien séparé;

Ne log rien d'autre pour l'instant.

## 0. Séparation obligatoire des crons

### 0.1 Installation explicite des crons Linux

La planification officielle utilise **cron Linux natif**, sans invocation d'agent/LLM ni prompt Hermes. Depuis le dossier d'installation, utiliser uniquement :

```bash
./run_telemetry.sh install-cron
```

Cette commande est idempotente et installe les deux entrées Linux. Le bot ne doit ni créer ni modifier automatiquement des crons Hermes, y compris lors d'une collecte ou d'un auto-update.

**Migration :** l'opérateur doit désactiver explicitement les anciens crons Hermes de télémétrie pour éviter les doublons. L'installateur ne les modifie pas. Ne pas conserver les deux planifications actives.

### 0.2 Auto-update au début de chaque collecte

Le runner conserve une copie locale stable et tente sa mise à jour via `scripts/aikub_telemetry_self_update.sh` avant chaque collecte. Utiliser ce helper pour la mise à jour Git ou la réparation d'une installation non Git, sans recopier une deuxième procédure shell dans un prompt.

Règles importantes :

- Ne montre jamais le contenu de `.env`, token, clé API ou password.
- Si la mise à jour échoue par auth/réseau, signale l'échec sans credential et continue seulement avec une copie locale exploitable; sinon arrête proprement.
- Ne prétends pas que la copie locale est à jour après un échec de mise à jour.
- L'auto-update ne remplace pas l'installation explicite via `install-cron`.

Pour éviter les erreurs de lourdeur, les sessions ne doivent plus partir dans le même run que l'inventaire/logs.

Cadence cible (fuseau horaire du cron système) :

| Cron Linux | Fréquence | Contenu |
|---|---:|---|
| AIKUB Telemetry Light | aux 2h à `0 */2 * * *` | inventaire/modèle/provider/crons/plugins/skills + logs incrémentaux seulement |
| AIKUB Telemetry Sessions | 1x/jour à `30 3 * * *` | snapshot complet sessions/messages seulement |

Règles d'exécution :

- Le cron régulier/light exécute `./run_telemetry.sh light`. N'exécute pas `scripts/aikub_telemetry_sessions_snapshot.py` dans ce run.
- Dans le cron régulier/light, l'inventaire doit rester indépendant des logs. Si les logs retournent une erreur API temporaire, note un warning mais ne considère pas que tout le cron est mort; le prochain run doit continuer à envoyer l'inventaire et réessayer les logs.
- Le cron quotidien sessions exécute `./run_telemetry.sh sessions`. N'envoie pas l'inventaire, les logs, le modèle, les plugins ni aucun autre event light dans ce run.

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
```

Règles :

- Ne demande pas de variable supplémentaire.
- Ne demande pas `AIKUB_BOT_DISPLAY_NAME`.
- Ne demande pas `AIKUB_BOT_MODEL_PROVIDER`.
- Ne demande pas `AIKUB_BOT_MODEL_NAME`.
- Ne demande pas `AIKUB_BOT_SKILLS_JSON`.
- Ne lis ni n'écris directement dans la DB ERP/telemetry; passe par l'API. La lecture de la DB locale Hermes `~/.hermes/state.db` est autorisée pour le snapshot sessions.
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

## 9. Envoie un seul event d'inventaire à AIKUB_Telemetry

Cette limite concerne uniquement l'inventaire du run light, pas le nombre total de POST. Les logs sont envoyés séparément, en un ou plusieurs POST sous le même event type de compatibilité `bot_inventory_snapshot`, avec `payload.logs`. Le run sessions envoie un ou plusieurs POST `bot_sessions_snapshot` selon le chunking.

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
9. Envoyer un seul `bot_inventory_snapshot` d'inventaire à AIKUB_Telemetry avec identité, modèle/provider, crons, plugins et skills.
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

Script gelé et exécuté seulement par le cron Linux quotidien `AIKUB Telemetry Sessions` :

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

Il s'agit d'un **snapshot complet** des sessions/messages admissibles à chaque run, pas d'un envoi incrémental : aucun curseur de sessions n'est utilisé. La limite existante de contenu reste **4000 caractères par message** (`--max-content-chars 4000`). Le script seul utilise 100 messages par chunk par défaut; le runner utilise `--chunk-size 50`. Le chunking ne change que le transport, pas la sélection complète.

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
