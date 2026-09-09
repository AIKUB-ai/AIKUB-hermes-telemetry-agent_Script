# Contrat figé - Hermes Sessions vers AIKUB Telemetry

Ce document fige la section **Sessions** du script AIKUB Hermes Telemetry Agent.

Objectif : alimenter la page ERP **BotOps → Telemetry → Sessions** avec les sessions visibles et les messages utiles du bot Hermes.

## Source locale

```text
~/.hermes/state.db
```

Tables Hermes utilisées :

```text
sessions
messages
```

## Event envoyé

```text
eventType: bot_sessions_snapshot
```

Endpoint :

```text
POST <AIKUB_TELEMETRY_BASE_URL>/v1/telemetry/events
```

Headers :

```http
content-type: application/json
x-aikub-botops-token: <AIKUB_TELEMETRY_BOTOPS_TOKEN>
```

Ne pas envoyer `x-api-key` ni `x-aikub-bot-id`; l'API associe le bot via Vault.

## Recette validée ERP

Le script envoie :

```text
- sessions avec au moins 1 message actif non vide et non compacted
- messages active=1, compacted=0, avec content non vide
- pas les sessions à 0 message
- pas les messages vides
- pas les messages compacted/inactive
```

Cette recette correspond au visuel ERP validé : afficher seulement les sessions/messages qui ont une valeur.

## Chunking

Le snapshot complet peut être trop gros pour un seul POST. Le script envoie donc plusieurs chunks :

```text
payload.sessions.batchId = même valeur pour tous les chunks
payload.sessions.chunkIndex = 1..N
payload.sessions.chunkCount = N
payload.sessions.messages = messages du chunk
payload.sessions.items = liste des sessions visibles
```

Côté ERP, il faut regrouper tous les events `bot_sessions_snapshot` qui ont le même `payload.sessions.batchId`, puis merger les messages.

Ne pas prendre seulement le dernier chunk.

## Déduplication ERP

```text
sessions: botId résolu côté API + sessionId
messages: botId résolu côté API + sessionId + messageId
```

Le script peut omettre `botId`; AIKUB_Telemetry doit utiliser le bot associé au token Vault.

## Payload principal

```json
{
  "eventType": "bot_sessions_snapshot",
  "payload": {
    "identity": {},
    "sessions": {
      "source": "hermes_state_db",
      "batchId": "hermes-sessions-full-<bot>-<utc>",
      "chunkIndex": 1,
      "chunkCount": 29,
      "transportChunkingOnly": true,
      "window": {
        "type": "full_snapshot_active_non_empty_messages"
      },
      "overview": {},
      "sessionsReturned": 23,
      "messagesReturned": 100,
      "items": [],
      "messages": [],
      "redactionApplied": true
    }
  }
}
```

## Champs session

Chaque item dans `payload.sessions.items[]` représente une session :

```text
sessionId
title
source / platform
chatId
chatType
model
provider
messageCount
toolCallCount
apiCallCount
inputTokens
outputTokens
startedAt
endedAt
lastMessageAt
archived
status
```

## Champs message

Chaque item dans `payload.sessions.messages[]` représente un message utile :

```text
messageId
sessionId
timestamp
role
content
contentPreview
contentTruncated
toolName
tokenCount
platformMessageId
active
compacted
```

## Redaction / sécurité

Le script masque les secrets avant envoi :

```text
API keys
tokens
passwords
Authorization headers
cookies
GitHub tokens
OpenAI-style keys
connection strings avec credentials
```

Les secrets sont remplacés par :

```text
[REDACTED]
```

Le script supprime aussi les caractères de contrôle invalides comme `NUL` pour éviter les erreurs API/DB.

## Commandes

Dry-run :

```bash
python3 scripts/aikub_telemetry_sessions_snapshot.py --dry-run
```

Envoi réel :

```bash
python3 scripts/aikub_telemetry_sessions_snapshot.py
```

Options utiles :

```text
--chunk-size 100
--max-content-chars 4000
```

## Notes ERP

- Overview affiche les stats de `payload.sessions.overview`.
- History affiche `payload.sessions.items` et `payload.sessions.messages`.
- Cacher les sessions à 0 message.
- Ne pas afficher les messages vides.
- Grouper les chunks par `batchId`.
- Dédupliquer par les clés ci-dessus.
