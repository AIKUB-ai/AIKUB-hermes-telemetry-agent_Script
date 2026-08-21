# AIKUB Telemetry Cron Script - Étapes 1 et 2

Tu es un bot Hermes/Aikub. Ce fichier est le script d'instructions que tu lis à chaque fois que ton cron de self-logging est actif.

Objectif des étapes figées :

1. documenter dans AIKUB_Telemetry ton inventaire de base : identité, modèle d'intelligence, skills disponibles, crons et plugins enabled;
2. envoyer les nouvelles lignes de `~/.hermes/logs/agent.log` pour alimenter la section logs du ERP;
3. envoyer les sessions/messages utiles de `~/.hermes/state.db` pour alimenter la section sessions du ERP.

Ne log rien d'autre pour l'instant.

## 1. Lis seulement les 4 variables `.env`

Les seules variables fournies par ton `.env` sont :

```env
AIKUB_TELEMETRY_BASE_URL=...
AIKUB_TELEMETRY_BOT_ID=...
AIKUB_TELEMETRY_SOURCE=...
AIKUB_TELEMETRY_API_KEY=...
```

Règles :

- Ne demande pas de variable supplémentaire.
- Ne demande pas `AIKUB_BOT_DISPLAY_NAME`.
- Ne demande pas `AIKUB_BOT_MODEL_PROVIDER`.
- Ne demande pas `AIKUB_BOT_MODEL_NAME`.
- Ne demande pas `AIKUB_BOT_SKILLS_JSON`.
- Ne lis jamais la base de données directement.
- N'affiche jamais `AIKUB_TELEMETRY_API_KEY`; si tu dois montrer la config, écris `[REDACTED]`.

## 2. Respecte le contrat JSON AIKUB_Telemetry

L'API accepte seulement ces champs à la racine :

```text
botId, eventType, severity, source, traceId, sessionId, payload, occurredAt
```

Donc toutes les données découvertes doivent être dans `payload`.

Ne mets jamais à la racine :

```text
identity, model, skills, skillCount, crons, cronCount, plugins, pluginCount, enabledCount
```

## 3. Documente ton identité

Tu dois logger ton identité à partir de :

- `AIKUB_TELEMETRY_BOT_ID` pour le slug technique du bot.
- `AIKUB_TELEMETRY_SOURCE` pour la source déclarée du bot.
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

## 9. Envoie un seul event à AIKUB_Telemetry

Endpoint :

```text
<AIKUB_TELEMETRY_BASE_URL>/v1/telemetry/events
```

Headers requis :

```http
content-type: application/json
x-api-key: <AIKUB_TELEMETRY_API_KEY>
x-aikub-bot-id: <AIKUB_TELEMETRY_BOT_ID>
```

Type d'event :

```text
bot_inventory_snapshot
```

Format complet attendu :

```json
{
  "botId": "<AIKUB_TELEMETRY_BOT_ID>",
  "eventType": "bot_inventory_snapshot",
  "severity": "INFO",
  "source": "<AIKUB_TELEMETRY_SOURCE>",
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

## 11. Résumé de ce que tu dois faire - inventaire

À chaque cron actif pour la section inventaire :

1. Lire les 4 variables `.env`.
2. Découvrir localement ton identité Hermes.
3. Découvrir localement ton modèle Hermes.
4. Découvrir localement tes skills Hermes.
5. Découvrir localement tes crons Hermes en version safe.
6. Découvrir localement seulement tes plugins Hermes enabled/actifs.
7. Construire un JSON avec seulement les champs racine acceptés par AIKUB_Telemetry.
8. Mettre tout l'inventaire dans `payload`.
9. Envoyer un seul `bot_inventory_snapshot` à AIKUB_Telemetry.
10. Ne rien logger d'autre dans l'event d'inventaire.

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

Script gelé :

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
- sessions avec au moins 1 message actif non vide
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
