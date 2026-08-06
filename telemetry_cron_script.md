# AIKUB Telemetry Cron Script - Étape 1

Tu es un bot Hermes/Aikub. Ce fichier est le script d'instructions que tu lis à chaque fois que ton cron de self-logging est actif.

Objectif unique de cette étape : documenter dans Aikub Telemetry ton inventaire de base : identité, modèle d'intelligence, skills disponibles.

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

## 2. Documente ton identité

Tu dois logger ton identité à partir de :

- `AIKUB_TELEMETRY_BOT_ID` pour le slug technique du bot.
- `AIKUB_TELEMETRY_SOURCE` pour la source déclarée du bot.
- Ton runtime local pour confirmer que tu roules sous Hermes.

Format attendu dans l'event :

```json
{
  "identity": {
    "technicalSlug": "<AIKUB_TELEMETRY_BOT_ID>",
    "name": "<AIKUB_TELEMETRY_BOT_ID>",
    "displayName": "<AIKUB_TELEMETRY_BOT_ID>",
    "runtime": "hermes"
  }
}
```

Important : n'utilise pas `Hermes Agent` comme nom du bot. Le nom du bot est le slug.

## 3. Documente ton modèle d'intelligence

Tu dois logger le modèle que tu utilises en lisant ta configuration Hermes locale.

Source principale :

```text
~/.hermes/config.yaml
```

Champs à extraire si disponibles :

- provider du modèle
- nom du modèle
- context length

Exemple de format attendu :

```json
{
  "model": {
    "provider": "openai-codex",
    "name": "gpt-5.5",
    "contextLength": 80000
  }
}
```

Si une valeur n'est pas trouvée, mets `null`. N'invente jamais un modèle.

## 4. Documente tes skills disponibles

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
  "category": "<categorie-si-disponible>"
}
```

Ajoute aussi :

```json
{
  "skillCount": <nombre_total_de_skills>
}
```

## 5. Envoie un seul event à Aikub Telemetry

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

Payload minimal attendu :

```json
{
  "eventType": "bot_inventory_snapshot",
  "botId": "<AIKUB_TELEMETRY_BOT_ID>",
  "source": "<AIKUB_TELEMETRY_SOURCE>",
  "identity": {
    "technicalSlug": "<AIKUB_TELEMETRY_BOT_ID>",
    "name": "<AIKUB_TELEMETRY_BOT_ID>",
    "displayName": "<AIKUB_TELEMETRY_BOT_ID>",
    "runtime": "hermes"
  },
  "model": {
    "provider": "<provider-ou-null>",
    "name": "<model-ou-null>",
    "contextLength": <nombre-ou-null>
  },
  "skills": [
    {
      "name": "<skill-name>",
      "description": "<description-si-disponible>",
      "category": "<categorie-si-disponible>"
    }
  ],
  "skillCount": <nombre_total_de_skills>
}
```

## 6. Limite stricte de l'étape 1

Pour cette version, ne log pas encore :

- specs machine
- fichiers locaux
- sessions
- messages
- logs complets
- plugins
- tools
- crons
- users
- secrets
- variables `.env` complètes
- tokens
- clés API
- données client

On ajoute ces sections plus tard, une étape à la fois.

## 7. Résumé de ce que tu dois faire

À chaque cron actif :

1. Lire les 4 variables `.env`.
2. Découvrir localement ton identité Hermes.
3. Découvrir localement ton modèle Hermes.
4. Découvrir localement tes skills Hermes.
5. Envoyer un seul `bot_inventory_snapshot` à Aikub Telemetry.
6. Ne rien logger d'autre.
