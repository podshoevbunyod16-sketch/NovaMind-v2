# SECURITY.md — politics of safety NovaMind-v2

## Supported versions

Only the current working branch and `main` receive fixes. This project is an MVP, not a foundation model and not a hosted service with an SLA.

## Reporting a vulnerability

Please use GitHub's private vulnerability reporting (Security → Report a vulnerability) or open a minimal issue **without** including secrets, tokens or user data. Include: affected endpoint/file, reproduction steps, impact, suggested fix. We aim to acknowledge within 72 hours; there is no paid bug-bounty programme.

**Never** attach real API keys, cookies, session tokens, private chats or user uploads to an issue.

## Security model in one page

- Terminal/agent shell is **disabled by default** (`TERMINAL_ENABLED=0`). It is a command allow-list inside a per-user folder, **not** an OS sandbox. For untrusted users run the container with a non-root UID, read-only root filesystem, cgroup limits and no access to host secrets.
- Authentication is currently browser-session based (`nova_sid` in a signed cookie). It isolates browsers; it is not a full account system. `auth.py` contains the User/Session/Workspace/Role model and revocation primitives for the migration.
- File uploads are validated before being written: size, normalized name, extension, magic bytes, ZIP-bomb checks and an optional antivirus hook.
- Web pages, uploaded documents and MCP output are untrusted data, never instructions. `guardrails.py` and `agent/policy.py` mark them as `<untrusted>` and redact secrets.
- Production checks: `SESSION_SECRET`, strong `ADMIN_CODE`, HTTPS cookies, `APP_ENV=production`. The generated admin code is **never** printed to logs.
- Rate limits: in-process fallback; Redis is required for multi-node deployments.

## Secrets

- Keep secrets only in environment variables / `.env` (ignored by git). `.env.example` contains placeholders only.
- `scripts/scan_secrets.py --history` runs in CI and prints rule names and paths, never values.
- Logs are redacted (`observability.redact`, `guardrails.check_output`); enable `LOG_JSON=1` for structured logs.
- Rotate any credential that may have been exposed; do not rewrite Git history in this repository without a separate, approved incident process.

## Known limitations

- A committed user upload remains in Git history; it was removed from the index in this branch, but history rewriting is deliberately out of scope.
- There is no server-side account database, MFA enforcement, S3 quarantine, Redis cluster or distributed rate limiting yet.
- The offline demo provider (`local_demo`) is for graceful degradation only: it is not a strong model and must not be treated as one.
