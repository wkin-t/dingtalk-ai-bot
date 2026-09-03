# Deployment Evidence: 2026-07-31

## Scope

Deployed commit `cc047d2` to the three parallel DingTalk containers on the
Tencent Cloud server. This deployment changed application code and task/spec
documentation; it did not change production environment values.

## Environment backup

Before pulling code, the server copied `.env`, `.env.openai`, and
`.env.openrouter` to:

```text
.deploy-backup-20260731-143833/
```

SHA-256 checks before and after deployment matched for every original/backup
pair. No secret values are recorded here.

## Deployment commands

The server fast-forwarded from `6929e56` to `cc047d2`, then ran:

```text
docker compose up -d --build
docker compose -f docker-compose.openai.yml up -d --build
docker compose -f docker-compose.openrouter.yml up -d --build
```

## Verification

- `dingtalk-ai-bot-gemini`: running, restart count 0, port 35000.
- `dingtalk-ai-bot-openai`: running, restart count 0, port 35001.
- `dingtalk-ai-bot-openrouter`: running, restart count 0, port 35002.
- Root endpoint probes for all three ports returned HTTP 200.
- Gunicorn reported listening on each expected port.
- The workspace and all three containers contained the same
  `app/openai_client.py` SHA-256:
  `27ff6848ee76e2fd38de435ed96fabcbd2a718a6dba03e88fec0e5cf07ecb14d`.

## Boundary

No live model request, search canary, reasoning canary, or DingTalk UI witness
was performed in this deployment turn. Those remain separate production-only
acceptance gates.
