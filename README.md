# AIKUB Hermes Telemetry Agent

Reusable script that lets each Hermes bot document how its local machine/agent works into **Aikub Telemetry**.

## V1 scope

This first step sends one `bot_inventory_snapshot` event with:

| Section | How it is discovered |
|---|---|
| Identity | from `AIKUB_TELEMETRY_BOT_ID` plus local runtime detection |
| Model | from `~/.hermes/config.yaml` |
| Skills | from Hermes' own skill inventory helper, fallback to local `SKILL.md` files |

Later steps can add machine specs, crons, plugins/tools, sessions, logs, files, etc.

## Bot `.env` contract

Each bot receives only these four values:

```env
AIKUB_TELEMETRY_BASE_URL=https://aikubtelemetry-production.up.railway.app
AIKUB_TELEMETRY_BOT_ID=your-bot-slug
AIKUB_TELEMETRY_SOURCE=your-bot-source
AIKUB_TELEMETRY_API_KEY=replace-with-bot-write-key
```

No model, display name, skills, plugin list, or machine inventory is manually passed through env. The bot discovers those locally.

## Usage

Dry-run, no API write:

```bash
python3 aikub_telemetry_logger.py --dry-run
```

Send to Aikub Telemetry:

```bash
python3 aikub_telemetry_logger.py
```

## Security rules

- Never commit real `.env` files.
- Never print the API key. Dry-run output redacts it.
- Bots write only through the Aikub Telemetry API.
- Bots never connect directly to the database.
