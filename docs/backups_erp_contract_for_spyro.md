# Handoff Spyro - Backups Hermes dans AIKUB ERP BotOps

> Contexte important : la section existe déjà dans `AIKUB_erp`, dans **BotOps -> aperçu d'un bot -> onglet Backups**. Le travail demandé n'est pas de créer une nouvelle page from scratch, mais de brancher cette section existante sur les nouveaux backups zip envoyés par les bots Hermes via le script GitHub telemetry/backup.

## Objectif produit

Chaque bot Hermes/AIKUB doit pouvoir envoyer automatiquement des archives de restauration vers AIKUB ERP BotOps.

Dans l'onglet **Backups** d'un bot, Félix doit pouvoir télécharger en tout temps :

| Type | Fréquence côté bot | Rétention ERP attendue | But |
|---|---:|---:|---|
| `daily` | 1x/jour | 6 derniers | rollback rapide récent |
| `weekly` | 1x/semaine | 4 derniers | rollback semaine précédente |
| `monthly` | 1x/mois | 3 derniers | rollback plus vieux |

Le bot garde localement seulement le dernier backup de chaque type. Le ERP garde la vraie rétention.

## Ce qu'on restore avec ces backups

Le but n'est pas de restaurer les anciennes conversations/sessions. Le but est de restaurer l'identité opérationnelle du bot :

- ses skills;
- ses crons;
- ses mémoires/personnalité;
- ses plugins;
- sa config Hermes;
- ses logs utiles au diagnostic;
- son `.env`, mais dans une archive séparée chiffrée.

En cas de crash, on veut pouvoir remettre le bot à au moins 90% de ce qu'il était.

## Architecture des fichiers envoyés

Chaque run peut avoir deux fichiers :

### 1. Archive principale non secrète

Exemple :

```text
aikub_bot_backup_chopchop_default_daily_20260909_012000.zip
```

Contenu :

```text
~/.hermes/config.yaml
~/.hermes/skills/
~/.hermes/cron/
~/.hermes/plugins/
~/.hermes/memories/
~/.hermes/logs/
~/.hermes/*.json
~/.hermes/*.yaml
~/.hermes/*.yml
manifest.json
```

Exclusions obligatoires :

```text
~/.hermes/state.db
~/.hermes/cache/
~/.hermes/backups/
__pycache__/
node_modules/
.venv/
venv/
*.pyc
*.tmp
```

Raison : `state.db` contient les sessions/messages et peut devenir lourd. Les sessions sont déjà gérées dans la telemetry quotidienne séparée.

### 2. Archive `.env` séparée et chiffrée

Exemple :

```text
aikub_bot_backup_chopchop_default_daily_20260909_012000.env.zip.enc
```

Contenu :

```text
~/.hermes/.env
```

Règles :

- le ERP stocke ce fichier comme blob opaque;
- le ERP ne lit jamais son contenu;
- le ERP n'affiche jamais les secrets;
- download permis seulement à un admin/BotOps autorisé;
- audit log obligatoire sur chaque download de l'archive `.env`.

## Chiffrement expliqué simple

Le `.env` contient potentiellement des tokens/API keys. Donc on ne veut pas qu'il soit dans le zip principal.

Le bot doit :

1. créer un petit zip avec seulement `.env`;
2. chiffrer ce zip localement;
3. uploader le fichier chiffré au ERP;
4. ne jamais envoyer la clé de chiffrement au ERP dans les payloads.

MVP recommandé : AES-256-GCM.

Variable côté bot :

```text
AIKUB_BACKUP_ENCRYPTION_KEY
```

Si la clé est absente :

- le backup principal doit quand même réussir;
- l'archive `.env` doit être marquée comme `skipped_missing_encryption_key`;
- le UI doit afficher `Env chiffré absent` ou `clé absente`, pas une erreur globale.

## Horaires recommandés côté bots

Pour éviter de se piler dessus avec telemetry/sessions :

```text
Daily backup:   20 1 * * *
Weekly backup:  10 2 * * 0
Monthly backup: 50 2 1 * *
Sessions:       30 3 * * *
```

La telemetry light aux 2h continue séparément et ne doit pas transporter les backups.

## Repo ERP où brancher ça

Repo :

```text
/home/chopchop/aikub-work/AIKUB_erp
```

Section existante :

```text
app/(app)/botops/botops-mvp-panel.tsx
```

Onglet existant dans le modal bot :

```tsx
case "backups": return <BackupLivePanel bot={bot} actions={actions} />;
```

Section globale existante BotOps :

```tsx
{ id: "backups", label: "Backups", description: "Targets, politiques et runs", icon: Database }
```

APIs existantes à respecter/étendre :

```text
GET  /api/botops/backups
GET  /api/botops/bots/:id/backups
GET  /api/botops/backups/runs/:id/download
```

Fichier helper existant :

```text
lib/botops/backups.ts
```

Schéma Prisma existant dans `AIKUB_BOTOPS_DB` :

```text
BotBackupTarget
BotBackupPolicy
BotBackupRun
```

Il y a aussi un ancien/système de snapshots dans :

```text
prisma/backup/schema.prisma
model BotBackupSnapshot
```

Mais pour les backups automatisés des bots, il faut privilégier la section BotOps existante par bot : `BotBackupRun` + stockage fichier.

## Modèle DB existant à étendre

Actuellement `BotBackupRun` contient :

```prisma
model BotBackupRun {
  id             String
  organizationId String
  workspaceId    String?
  botId          String
  targetId       String?
  status         BotBackupRunStatus
  trigger        BotBackupTrigger
  requestedByRef String?
  storageUri     String?
  sizeBytes      BigInt?
  checksum       String?
  logMessage     String?
  errorMessage   String?
  requestedAt    DateTime
  startedAt      DateTime?
  finishedAt     DateTime?
  expiresAt      DateTime?
  metadata       Json?
}
```

Pour éviter une grosse migration compliquée, MVP recommandé : garder les nouveaux détails dans `metadata_json` de `BotBackupRun`.

### Metadata recommandée dans `BotBackupRun.metadata`

```json
{
  "tier": "daily",
  "profile": "default",
  "botSlug": "chopchop",
  "source": "hermes_agent_backup_script",
  "archive": {
    "fileName": "aikub_bot_backup_chopchop_default_daily_20260909_012000.zip",
    "contentType": "application/zip",
    "sizeBytes": 12345678,
    "sha256": "hex-sha256-main",
    "storageUri": "r2://aikub-bot-backups/chopchop/daily/...zip",
    "chunkSizeBytes": 5242880,
    "chunkCount": 3
  },
  "envArchive": {
    "present": true,
    "fileName": "aikub_bot_backup_chopchop_default_daily_20260909_012000.env.zip.enc",
    "contentType": "application/octet-stream",
    "sizeBytes": 1234,
    "sha256": "hex-sha256-env",
    "storageUri": "r2://aikub-bot-backups/chopchop/daily/...env.zip.enc",
    "encryption": "AES-256-GCM",
    "status": "encrypted"
  },
  "includes": ["config", "skills", "cron", "plugins", "memories", "logs", "manifest"],
  "excludes": ["state.db", "cache", "backups", "venv", "node_modules"],
  "localRetention": "latest_only_per_tier",
  "erpRetention": { "daily": 6, "weekly": 4, "monthly": 3 },
  "attempts": 1,
  "manifest": {
    "createdAt": "2026-09-09T01:20:00.000Z",
    "hostname": "chopchop",
    "hermesHome": "/home/chopchop/.hermes",
    "fileCount": 321,
    "totalBytesBeforeZip": 23456789
  }
}
```

Champs directs à remplir sur `BotBackupRun` :

```text
status: REQUESTED/RUNNING/SUCCEEDED/FAILED
trigger: SCHEDULED ou API
storageUri: URI archive principale
sizeBytes: taille archive principale
checksum: sha256 archive principale
startedAt
finishedAt
expiresAt
logMessage / errorMessage
metadata: objet ci-dessus
```

## Si tu préfères une migration propre plus tard

Après MVP, créer une table dédiée `bot_backup_artifacts` serait mieux pour séparer archive principale et env :

```prisma
model BotBackupArtifact {
  id          String   @id @default(uuid()) @db.Uuid
  runId       String   @map("run_id") @db.Uuid
  kind        String   @map("kind") // MAIN | ENV_ENCRYPTED
  fileName    String   @map("file_name")
  storageUri  String   @map("storage_uri")
  sizeBytes   BigInt   @map("size_bytes")
  sha256      String   @map("sha256")
  contentType String   @map("content_type")
  metadata    Json?    @map("metadata_json")
  createdAt   DateTime @default(now()) @map("created_at") @db.Timestamptz(6)

  @@index([runId, kind])
  @@map("bot_backup_artifacts")
}
```

Mais je recommande de commencer avec `metadata_json` pour aller vite et ne pas casser l'onglet Backups existant.

## Endpoints à ajouter pour recevoir les backups des bots

Ces endpoints doivent être côté BotOps/telemetry et utiliser le token bot :

```text
x-aikub-botops-token: <token bot>
```

Le ERP doit résoudre le bot via Vault/token. Ne pas faire confiance à un `botId` envoyé par le bot.

### 1. Start

```http
POST /api/botops/backups/uploads/start
```

Body :

```json
{
  "tier": "daily",
  "profile": "default",
  "fileName": "aikub_bot_backup_chopchop_default_daily_20260909_012000.zip",
  "fileSizeBytes": 12345678,
  "sha256": "hex-sha256-main",
  "chunkSizeBytes": 5242880,
  "chunkCount": 3,
  "envFileName": "aikub_bot_backup_chopchop_default_daily_20260909_012000.env.zip.enc",
  "envFileSizeBytes": 1234,
  "envSha256": "hex-sha256-env",
  "envEncryptionStatus": "encrypted",
  "metadata": {
    "hostname": "chopchop",
    "hermesHome": "/home/chopchop/.hermes",
    "includes": ["config", "skills", "cron", "plugins", "memories", "logs"],
    "excludes": ["state.db", "cache", "backups"]
  }
}
```

Validation :

- `tier` doit être `daily`, `weekly`, ou `monthly`;
- `fileName` doit finir par `.zip`;
- `envFileName`, si présent, doit finir par `.env.zip.enc`;
- `chunkCount` raisonnable;
- taille max configurable;
- token bot valide;
- bot actif/non supprimé.

Response :

```json
{
  "ok": true,
  "runId": "uuid-botBackupRun",
  "status": "RUNNING",
  "chunkSizeBytes": 5242880
}
```

Action serveur : créer un `BotBackupRun` :

```text
status = RUNNING
trigger = SCHEDULED ou API
startedAt = now
expiresAt = selon tier
metadata.tier = daily/weekly/monthly
metadata.archive = infos archive main
metadata.envArchive = infos env archive
```

### 2. Chunk upload

```http
POST /api/botops/backups/uploads/:runId/chunk
Content-Type: multipart/form-data
```

Fields :

```text
kind=main | env
chunkIndex=0
chunkCount=3
sha256=<sha256 du chunk>
file=<binary>
```

Response :

```json
{
  "ok": true,
  "runId": "uuid",
  "kind": "main",
  "chunkIndex": 0,
  "received": true
}
```

Le serveur doit stocker les chunks temporairement dans R2/S3/local tmp selon infra. Ne pas marquer le run `SUCCEEDED` ici.

### 3. Complete

```http
POST /api/botops/backups/uploads/:runId/complete
```

Body :

```json
{
  "sha256": "hex-sha256-main",
  "envSha256": "hex-sha256-env",
  "fileSizeBytes": 12345678,
  "envFileSizeBytes": 1234
}
```

Action serveur obligatoire :

1. vérifier que tous les chunks `main` sont reçus;
2. réassembler ou finaliser l'objet principal;
3. recalculer le SHA256 côté serveur;
4. comparer au SHA256 reçu;
5. faire pareil pour `env` si présent;
6. mettre `BotBackupRun.status = SUCCEEDED` seulement si tout match;
7. remplir `storageUri`, `sizeBytes`, `checksum`, `finishedAt`, `logMessage`;
8. appliquer la rétention ERP.

Response :

```json
{
  "ok": true,
  "runId": "uuid",
  "status": "SUCCEEDED",
  "mainDownloadUrl": "/api/botops/backups/runs/uuid/download?kind=main",
  "envDownloadUrl": "/api/botops/backups/runs/uuid/download?kind=env"
}
```

### 4. Fail

```http
POST /api/botops/backups/uploads/:runId/fail
```

Body :

```json
{
  "stage": "chunk_upload",
  "retryCount": 2,
  "errorMessage": "upload failed after retries"
}
```

Action serveur :

```text
status = FAILED
finishedAt = now
errorMessage = message court
metadata.failure = détails safe sans secrets
```

## Download existant à étendre

Actuel :

```text
GET /api/botops/backups/runs/:id/download
```

À étendre pour supporter :

```text
GET /api/botops/backups/runs/:id/download?kind=main
GET /api/botops/backups/runs/:id/download?kind=env
```

Règles :

- `kind=main` par défaut si absent, pour ne pas casser l'existant;
- `kind=env` télécharge l'archive chiffrée seulement;
- vérifier permissions BotOps admin;
- audit log obligatoire;
- ne jamais streamer/logguer le contenu en console;
- headers download corrects : `Content-Disposition: attachment`.

## UI existante à modifier

Fichier :

```text
app/(app)/botops/botops-mvp-panel.tsx
```

Composant :

```text
BackupLivePanel
```

Actuellement il affiche surtout targets/runs. Il faut le rendre plus concret pour Félix.

### UI attendue dans l'onglet Backups du bot

En haut : 4 métriques :

```text
Santé backup
Dernier daily
Dernier weekly
Dernier monthly
```

Ensuite 3 sections/tabs :

```text
Daily backups (6)
Weekly backups (4)
Monthly backups (3)
```

Chaque ligne doit afficher :

```text
Date/heure
Type: daily/weekly/monthly
Status: SUCCEEDED/RUNNING/FAILED
Taille archive principale
Checksum court: 12 premiers chars
Bouton Download archive
Bouton Download .env chiffré, si présent
Expiration
Erreur courte si failed
```

### Labels UX recommandés

```text
Archive bot
.env chiffré
Restore rapide
Échec upload
Checksum vérifié
```

Le bouton `.env chiffré` doit avoir un warning/tooltip :

```text
Contient des secrets chiffrés. Télécharger seulement pour restauration autorisée.
```

## GET /api/botops/bots/:id/backups à adapter

Actuel : retourne `runs` paginés.

Ajouter une structure groupée :

```json
{
  "bot": { "id": "...", "name": "ChopChop", "slug": "chopchop" },
  "health": "HEALTHY",
  "retention": { "daily": 6, "weekly": 4, "monthly": 3 },
  "latest": {
    "daily": { "id": "...", "status": "SUCCEEDED" },
    "weekly": null,
    "monthly": null
  },
  "groups": {
    "daily": [],
    "weekly": [],
    "monthly": []
  },
  "runs": [],
  "pagination": {}
}
```

Les groupes peuvent être calculés depuis `BotBackupRun.metadata.tier`.

Filtre recommandé :

```ts
where: {
  botId: bot.id,
  status: { in: ["SUCCEEDED", "RUNNING", "FAILED"] },
}
```

Puis grouper :

```ts
const tier = run.metadata?.tier === "weekly" ? "weekly" : run.metadata?.tier === "monthly" ? "monthly" : "daily";
```

Limiter :

```text
daily -> 6 SUCCEEDED + failed/running récents si désiré
weekly -> 4 SUCCEEDED
monthly -> 3 SUCCEEDED
```

## Rétention serveur

Après un `complete` réussi :

Pour le même `botId` + `tier` :

```text
- daily: garder 6 derniers SUCCEEDED
- weekly: garder 4 derniers SUCCEEDED
- monthly: garder 3 derniers SUCCEEDED
```

Les vieux runs doivent :

1. supprimer l'objet principal;
2. supprimer l'objet env chiffré si présent;
3. passer `status = EXPIRED` ou être masqués de l'UI;
4. garder un log minimal/audit si requis.

Ne jamais supprimer les vieux backups avant que le nouveau run soit `SUCCEEDED`.

## Storage fichiers

Storage recommandé : R2/S3 ou bucket privé équivalent.

Structure recommandée :

```text
bot-backups/{organizationId}/{botSlug}/{tier}/{runId}/main.zip
bot-backups/{organizationId}/{botSlug}/{tier}/{runId}/env.zip.enc
```

Ne pas stocker ces fichiers dans Git. Ne pas stocker en public.

## Auth et sécurité

Pour endpoints d'upload bot :

```text
x-aikub-botops-token: <token bot>
```

À faire :

- résoudre le bot depuis Vault/token;
- refuser si token invalide;
- refuser si bot archivé/deleted;
- ne jamais accepter `botId` comme source de vérité;
- limiter taille max;
- rate-limit par bot;
- journaliser seulement metadata safe.

Pour downloads ERP :

- require BotOps/admin auth existante;
- vérifier accès compagnie/client du bot;
- audit log obligatoire;
- `env` download avec warning.

## Status mapping

Mapping côté API :

```text
/start accepté -> RUNNING
/chunk partiel -> RUNNING
/complete ok -> SUCCEEDED
/complete checksum fail -> FAILED
/fail appelé -> FAILED
rétention expirée -> EXPIRED
```

UI labels :

```text
RUNNING -> En cours
SUCCEEDED -> Prêt
FAILED -> Échec
EXPIRED -> Expiré
REQUESTED -> Demandé
SKIPPED -> Ignoré
```

## Tests minimum côté ERP

### Test API start

- Appeler `/uploads/start` avec token bot valide.
- Vérifier création `BotBackupRun` status `RUNNING`.
- Vérifier `metadata.tier`.

### Test chunks + complete

- Uploader un petit zip en 2 chunks.
- Appeler `/complete`.
- Vérifier status `SUCCEEDED`.
- Vérifier checksum serveur.
- Vérifier `storageUri`.

### Test env chiffré

- Uploader `env.zip.enc` fake.
- Vérifier download possible comme fichier opaque.
- Vérifier que le ERP n'affiche jamais le contenu.

### Test UI onglet Backups

Dans `BackupLivePanel` :

- vérifier Daily affiche maximum 6;
- Weekly maximum 4;
- Monthly maximum 3;
- bouton archive marche;
- bouton env chiffré marche si présent;
- failed affiche erreur courte.

### Test rétention

- Créer 7 daily SUCCEEDED;
- vérifier que seulement 6 restent visibles;
- créer 5 weekly;
- vérifier 4;
- créer 4 monthly;
- vérifier 3.

### Test permissions

- User sans accès compagnie/client ne peut pas lister/download;
- token bot invalide ne peut pas upload;
- `botId` spoofé dans body est ignoré.

## Important pour ne pas casser l'existant

- Ne pas supprimer `BackupsSection` global.
- Ne pas supprimer `BackupLivePanel`.
- Ne pas remplacer les targets/policies existants sans migration.
- Ajouter le support des archives bot dans les runs existants.
- Garder `GET /api/botops/backups/runs/:id/download` compatible sans query string.
- Si une migration DB est nécessaire, vérifier prod avant de query les nouveaux champs.

## Livrable attendu

1. Endpoints upload bot fonctionnels.
2. Stockage archive principale + env chiffré.
3. `BotBackupRun` rempli proprement.
4. Rétention ERP appliquée : 6/4/3.
5. Onglet Backups du bot affiche les groupes Daily/Weekly/Monthly.
6. Downloads fonctionnels.
7. Tests API + UI minimaux.

## Phrase courte du besoin

Brancher dans l'onglet Backups existant de BotOps les backups zip envoyés automatiquement par les bots Hermes. Chaque bot doit avoir 6 daily, 4 weekly, 3 monthly téléchargeables. Le zip principal contient l'identité du bot sans sessions/state.db. Le `.env` est un fichier séparé chiffré et opaque. Upload chunké, checksum serveur, retry côté bot, rétention serveur, downloads sécurisés et audités.
