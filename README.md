# Datacom Kudos prototype

A small Python standard-library HTTP application demonstrating authenticated peer kudos, a recent feed, and moderator hide/restore/delete workflows backed by SQLite.

This is a local prototype, not a production portal integration. It uses environment-configured bearer tokens and a seeded local employee directory. The local UI is not protected by TLS and the token map is a development adapter only. Bind to localhost; replace these adapters with the organization's SSO and directory services and deploy behind the approved HTTPS portal before production use.

## Requirements

- Python 3.10 or newer
- No third-party packages

## Run locally (PowerShell)

From this folder:

```powershell
$env:KUDOS_TOKENS_JSON='{"replace-with-a-random-user-token":"E002","replace-with-a-random-moderator-token":"E001"}'
python .\app.py
```

Open `http://127.0.0.1:8000`. Enter the matching bearer token in the demo UI. The seeded directory includes moderator `E001` (Alex Admin) and employees `E002` (Sam Employee) and `E003` (Taylor Employee).

The token values above are placeholders; choose local random values. Do not commit real credentials. Tokens are read from `KUDOS_TOKENS_JSON` and are never persisted to the database.

Optional environment variables:

- `KUDOS_DB_PATH`: SQLite database path (default: `kudos.sqlite3` in this folder)
- `KUDOS_HOST`: bind address (default: `127.0.0.1`; do not expose this demo to an untrusted network)
- `KUDOS_PORT`: HTTP port (default: `8000`)
- `KUDOS_TOKENS_JSON`: JSON object mapping bearer tokens to employee IDs
- `KUDOS_SUBMISSIONS_PER_HOUR`: per-user creation limit (default: `10`)
- `KUDOS_DUPLICATE_WINDOW_SECONDS`: duplicate message rejection window (default: `60`)

The seeded employees are inserted if missing; existing employee rows are not overwritten.

## Test

```powershell
python -m unittest discover -s tests -v
```

## API overview

All API endpoints require `Authorization: Bearer <token>`.

- `GET /api/employees?query=...`
- `POST /api/kudos` with `{"recipient_id":"E003","message":"Thanks for your help!"}`
- `GET /api/kudos?limit=20&cursor=...`
- `GET /api/moderation/kudos?limit=50` (moderators)
- `POST /api/kudos/{id}/hide` with optional `{"reason":"..."}`
- `POST /api/kudos/{id}/restore`
- `DELETE /api/kudos/{id}`

`E001` is a moderator in the local demo directory. Employees cannot award themselves kudos. Submissions are limited to 10 per hour, and identical sender/recipient/message submissions within 60 seconds are rejected.

See [SPECIFICATION.md](SPECIFICATION.md) and [PROCESS_FLOW.md](PROCESS_FLOW.md) for approved requirements, design, and flows.
