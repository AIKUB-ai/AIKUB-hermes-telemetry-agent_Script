# AIKUB Telemetry Cron Script - Étape 1

Tu es un bot Hermes/Aikub. Ce fichier est le script d'instructions que tu lis à chaque fois que ton cron de self-logging est actif.

Objectif de l'étape figée : documenter dans AIKUB_Telemetry ton inventaire de base : identité, modèle d'intelligence, skills disponibles, crons, et inventaire de fichiers safe.

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
identity, model, skills, skillCount, crons, cronCount, fileInventory
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

## 7. Documente ton inventaire de fichiers safe

Tu dois logger seulement des métadonnées de fichiers. Jamais le contenu.

Sources safe à scanner pour cette étape :

```text
~/.hermes/scripts
~/.hermes/skills
~/.hermes/cron
```

Pour chaque fichier retourné, log seulement :

- path relatif
- nom
- extension
- sizeBytes
- modifiedAt

Tu dois exclure :

- `.env`
- secrets
- tokens
- credentials
- passwords
- keys
- cookies
- sessions
- cache sensible
- outputs cron complets
- fichiers trop gros
- contenu de fichiers

Format attendu dans `payload` :

```json
{
  "fileInventory": {
    "fileCount": <nombre_total_safe>,
    "returnedCount": <nombre_retourne>,
    "skippedCount": <nombre_exclu>,
    "roots": ["scripts", "skills", "cron"],
    "byExtension": {
      ".md": 363,
      ".py": 36
    },
    "items": [
      {
        "path": "scripts/example.py",
        "name": "example.py",
        "extension": ".py",
        "sizeBytes": 1234,
        "modifiedAt": "<date-utc>"
      }
    ]
  }
}
```

## 8. Envoie un seul event à AIKUB_Telemetry

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
    "fileInventory": {
      "fileCount": <nombre_total_safe>,
      "returnedCount": <nombre_retourne>,
      "skippedCount": <nombre_exclu>,
      "roots": [],
      "byExtension": {},
      "items": []
    }
  }
}
```

## 9. Limite stricte de cette étape

Pour cette version, ne log pas encore :

- specs machine détaillées
- sessions
- messages
- logs complets
- plugins
- tools
- users
- secrets
- variables `.env` complètes
- tokens
- clés API
- données client
- contenu de fichier
- prompt complet de cron
- session/chat complet

On ajoute ces sections plus tard, une étape à la fois.

## 10. Résumé de ce que tu dois faire

À chaque cron actif :

1. Lire les 4 variables `.env`.
2. Découvrir localement ton identité Hermes.
3. Découvrir localement ton modèle Hermes.
4. Découvrir localement tes skills Hermes.
5. Découvrir localement tes crons Hermes en version safe.
6. Découvrir localement tes fichiers en metadata seulement.
7. Construire un JSON avec seulement les champs racine acceptés par AIKUB_Telemetry.
8. Mettre tout l'inventaire dans `payload`.
9. Envoyer un seul `bot_inventory_snapshot` à AIKUB_Telemetry.
10. Ne rien logger d'autre.
