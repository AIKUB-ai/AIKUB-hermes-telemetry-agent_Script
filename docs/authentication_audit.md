# Audit authentification — dépôt uniquement

Base auditée : `33a4006` (branche main). Aucun accès aux credentials, installations ou logs de production; aucune commande Hermes, réauthentification, installation, commit ou push exécuté. Les tests utilisent des HOME temporaires et des commandes de collecte/Git/cron simulées (sauf dépôt Git de fixture local).

## Conclusion

Le message « Provider authentication failed. Check the configured credentials; raw provider details are in the gateway logs. » ne prouve pas une suppression de credentials. La causalité entre installation et réauthentification des cinq bots reste **non établie**. Il manque les versions réellement déployées, la commande d'installation effective et la classe d'erreur provider dans les logs gateway, à collecter sans divulguer de tokens.

## Constats prouvés lors de l'audit initial (avant durcissement ci-dessous)

- **Bug corrigé : sélection du home.** Avant correction, `run_telemetry.sh:13` fixait HERMES_HOME à `$HOME/.hermes` avant la lecture du `.env`. Cela neutralisait le HERMES_HOME du fichier et le fallback HERMES_REAL_HOME. Suppression de cette initialisation; résolution conservée après lecture des variables (`run_telemetry.sh:52-56` après correction). Conséquence certaine : collecte/curseurs potentiellement sur le mauvais profil. Cela ne change pas l'environnement du gateway parent et ne prouve pas un échec OAuth.
- **Pas de sourcing shell du .env.** `run_telemetry.sh:22-43` extrait une liste de clés avec awk; pas de `source`, `eval` ni export global des credentials providers. Aucun changement HOME, chown ou chmod des credentials trouvé dans les scripts actuels. Le chmod de l'updater porte sur ses scripts (`scripts/aikub_telemetry_self_update.sh:59`).
- **Pas de refresh OAuth explicite dans les scripts actuels.** Le runner appelle Python, pas `hermes auth`, `hermes model` ou une commande de réauthentification. `scripts/aikub_telemetry_logger.py:73-102` lit config.yaml; il ne l'écrit pas. Les écritures des collecteurs concernent le curseur et la quarantaine (`scripts/aikub_telemetry_logs_incremental.py:181,286`). La connexion SQLite sessions n'est pas ouverte avec `mode=ro` (`scripts/aikub_telemetry_sessions_snapshot.py:113`), même si les requêtes de collecte sont des lectures : durcissement possible, pas une preuve de mutation d'auth.
- **Limite de la garantie passive : import Hermes.** `scripts/aikub_telemetry_logger.py:137-143` importe `tools.skills_tool` depuis l'installation et exécute `_find_all_skills`. Les effets transitifs dépendent de la version Hermes réellement installée. Aucune preuve de refresh par ce chemin n'a été établie. Le test d'inventaire couvre la fixture/fallback, pas toutes les versions de cet import.
- **Historique comptes : lecture, pas rotation.** À `5e92873`, `scripts/aikub_telemetry_logger.py:309-315` lisait auth.json; les JWT étaient décodés localement. `ccccbd5:...:280-298` ajoutait un hash des claims de sujet, sans appel OAuth. Ces fonctions ont été supprimées par `4c56e14`. La lecture seule ne démontre pas une invalidation de refresh token.
- **Ancienne planification agent.** `5e92873:telemetry_cron_script.md:72-87` recommandait `hermes cron create` avec un prompt sessions, sans mode script/no-agent. Cela pouvait déclencher un run agent et donc utiliser le provider lors de son exécution, mais ne démontre pas un refresh au moment de la commande create/list. Depuis `4c56e14`, la documentation prescrit cron Linux; les anciens crons Hermes ne sont pas désactivés automatiquement (`README.md:51-53`). Des doubles exécutions restent possibles si la migration n'a pas été faite.

## Risques identifiés (statut après durcissement local)

1. **Cible updater non bornée — corrigé localement :** avant durcissement, AIKUB_TELEMETRY_AGENT_DIR pouvait désigner un dossier Hermes non Git; le helper pouvait le déplacer entier et ne recopier que `.env`, logs et quelques états, pas auth.json/config.yaml. C'était un chemin destructif conditionnel, **pas une preuve que les cinq installations l'ont emprunté**. Les gardes détaillées ci-dessous refusent maintenant ces cibles avant modification. Le runner force normalement la cible à son INSTALL_DIR.
2. **Concurrence :** locks Linux séparés light/sessions (`run_telemetry.sh:81-82`), absence de verrou interne dans l'updater. Un chevauchement, run manuel ou ancien cron Hermes peut faire fetch/reset/réparation en concurrence (`scripts/aikub_telemetry_self_update.sh:35-55`). Aucun lien démontré avec un refresh OAuth; risque de checkout incohérent et doublons télémétrie.
3. **Mise à jour en place :** chaque collecte charge la version distante de main, sans pin, par reset --hard ou clone (`scripts/aikub_telemetry_self_update.sh:38-44`). La version actuelle du dépôt ne suffit donc pas à reconstruire le code exécuté au moment de l'incident. Les permissions/owners existants des credentials ne sont pas vérifiables par cet audit dépôt.

## Durcissement complémentaire effectué localement

- **Updater :** résolution canonique via `realpath -m`, puis refus de `/`, HOME, home Hermes par défaut, HERMES_HOME, HERMES_REAL_HOME, répertoires `profiles`, racines des profils et leurs ancêtres. Les profils liés par symlink et les profils frères d'un home personnalisé sont protégés; une nouvelle racine de profil ne peut pas servir de cible. Les sous-dossiers dédiés de télémétrie restent autorisés.
- **Identité de l'installation :** tout dossier existant doit contenir le runner et les trois collecteurs attendus, sous forme de fichiers ordinaires non symlink. Les dossiers mixtes contenant à leur racine auth.json, credentials.json, config.yaml, state.db, .codex ou profiles sont refusés même s'ils contiennent les scripts. Les métadonnées `.git` externes (symlink/worktree) sont refusées. Validation avant toute action, puis de nouveau avant reset/remplacement; un clone destiné à remplacer l'installation doit lui aussi contenir les scripts attendus.
- **Symlinks valides :** les opérations portent sur la destination canonique; un alias vers une véritable installation demeure un symlink après réparation. `.env` et les états runtime continuent d'être préservés par le chemin de réparation existant.
- **Inventaire skills :** suppression de l'import `tools.skills_tool` et de la modification de `sys.path`. Lecture seule des SKILL.md des deux racines déjà prises en charge, avec priorité aux skills locaux et exclusion de `optional-skills`. C'est désormais explicitement un inventaire de fichiers installés, pas une garantie d'activation exacte au runtime : les filtres disabled et les registrations dynamiques de plugins ne sont pas exécutés.
- **Sessions SQLite :** URI échappée `Path.resolve().as_uri()` avec `?mode=ro` et `uri=True`. Écriture SQL refusée, création accidentelle de base interdite; chemins contenant espaces, `?` et `#` testés. Pas de `immutable=1`, pour conserver la lecture correcte des bases actives/WAL. Ce mode ne constitue pas une garantie d'absence de toute activité SQLite sur ses fichiers auxiliaires.
- **Limites :** garde de configuration erronée, pas frontière de sécurité contre un acteur local changeant les symlinks en concurrence. Les risques de concurrence, de reset en place et de version distante non épinglée demeurent. Un dossier incomplet/non identifiable est maintenant refusé plutôt que déplacé automatiquement. GNU `realpath -m` est requis (cible Linux).

## Validation initiale

- Nouveau test `tests/test_runner.py:106-125` : cinq cas de résolution home/profil, dont priorité de l'environnement et fallback par défaut.
- Avant correction : trois sous-cas échouaient (home du .env et HERMES_REAL_HOME).
- Après correction : `PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -v` → **37 tests OK**.
- `bash -n run_telemetry.sh scripts/aikub_telemetry_self_update.sh` et `git diff --check` → OK.

## Validation du durcissement

- Nouveau fichier `tests/test_passive_hardening.py` : HOME et bases SQLite temporaires, sentinelles synthétiques auth.json/config.yaml et dépôts Git locaux uniquement. Aucun credential réel ni accès production.
- Les tests vérifient refus des cibles dangereuses/étrangères, conservation du contenu et des inodes des sentinelles, absence de déplacement `.old_*` ou staging résiduel lors des refus, clone neuf valide, réparation via alias symlink et mise à jour Git suivante.
- Tests de lectures passives : aucune insertion Hermes dans sys.path, priorité du SKILL.md local, écriture SQLite refusée et absence de création de base manquante.
- Avant correction des lecteurs : deux tests échouaient (mutation sys.path et écriture SQLite autorisée). Les anciens chemins destructifs de l'updater n'ont pas été exécutés contre HOME ou `/` pour obtenir artificiellement un test rouge.
- Suite finale : `PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -v` → **48 tests OK**; `bash -n run_telemetry.sh scripts/aikub_telemetry_self_update.sh` et `git diff --check` → OK. Le test WAL confirme la lecture des données validées alors qu'une connexion d'écriture reste ouverte.
- Les modifications préexistantes `run_telemetry.sh` et `tests/test_runner.py` sont conservées sans retouche. Aucun commit/push ni déploiement effectué.

Pour trancher l'incident : comparer les SHA effectivement exécutés et heures d'installation/erreur; relever uniquement la catégorie provider (ex. jeton expiré, refresh réutilisé, permission, mauvais profil), les chemins effectifs HOME/HERMES_HOME du gateway et les métadonnées owner/mode/mtime des fichiers auth. Ne pas copier les logs bruts ou valeurs de credentials dans le rapport. Aucune de ces opérations production n'a été effectuée ici.
