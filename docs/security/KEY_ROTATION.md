# Key Rotation Runbook

Operational runbook for rotating every production secret used by Hive.
Rotation itself is an ops task — this document gives the exact commands.

**Targets referenced below**

| Where | Path / identifier |
|---|---|
| Local env file | `.env` in the repo checkout (**never committed**) |
| Server env file | `root@187.127.140.125:/opt/hive/.env` (mode `600`) |
| GitHub secrets | repo `rShetty/hive` (`OPENROUTER_API_KEY`, `ENCRYPTION_KEY`, `SECRET_KEY`, `HIVE_SIGNING_SECRET`, `VPS_SSH_KEY`) |
| Running stack | `docker compose -f docker-compose.prod.yml` on the VPS |

---

## Golden rules

1. **Historical exposure requires rotation.** If a secret ever appeared in git
   history, CI logs, or terminal output, treat it as compromised and rotate it
   — even if the file was later deleted. Deleting a file from `HEAD` does not
   remove it from clones, forks, or GitHub's cached commit views. Commit
   allowlists in `.gitleaks.toml` are **not** a substitute for rotation and are
   no longer permitted.
2. **Order: create new → deploy new → verify → revoke old.** Never revoke
   first.
3. **Never echo secret values to stdout.** Generate with `openssl rand`, edit
   files over an interactive SSH session (or pipe values via stdin), and keep
   every env file at mode `600`. `deploy.sh` generates keys silently for this
   reason.
4. **After changing any GitHub secret, redeploy** so the running containers
   pick up the new value:

   ```bash
   ssh root@187.127.140.125 "cd /opt/hive && docker compose -f docker-compose.prod.yml up -d --force-recreate marketplace"
   ```

5. Verify health after every rotation:

   ```bash
   curl -sf https://hive.rajeev.me/api/health
   ```

---

## 1. OpenRouter API key (`OPENROUTER_API_KEY`)

Used for LLM calls (BYOA fallback). Compromise impact: billable API usage by a
third party.

```bash
# 1. Create a replacement key in the OpenRouter dashboard:
#    https://openrouter.ai/settings/keys -> "Create Key"
#    (copy it to your clipboard; do not paste it into a shell)

# 2. Update the GitHub secret (prompts securely; never pass as a CLI argument):
gh secret set OPENROUTER_API_KEY -R rShetty/hive

# 3. Update the server env file interactively (value never hits argv/logs):
ssh root@187.127.140.125
umask 077
vi /opt/hive/.env          # replace the OPENROUTER_API_KEY= line
chmod 600 /opt/hive/.env
exit

# 4. Redeploy + verify (see Golden rule 4/5), then trigger one agent run.

# 5. Revoke the old key in the OpenRouter dashboard ("Revoke").
```

## 2. Encryption key (`ENCRYPTION_KEY`)

Fernet key for secrets at rest (stored BYOA provider keys,
`backend/services/crypto.py`). **Rotating makes previously encrypted rows
undecryptable** — users must re-enter stored provider keys afterwards.

```bash
# 1. Back up data before touching the key:
ssh root@187.127.140.125 "cp -a /opt/hive/data /opt/hive/data.bak.$(date +%Y%m%d)"

# 2. Generate the new value locally (do NOT display it):
NEW_ENCRYPTION_KEY="$(openssl rand -hex 32)"

# 3. Update the GitHub secret via stdin:
printf '%s' "$NEW_ENCRYPTION_KEY" | gh secret set ENCRYPTION_KEY -R rShetty/hive --body -

# 4. Update the server env file via stdin (never argv/stdout):
printf '%s' "$NEW_ENCRYPTION_KEY" \
  | ssh root@187.127.140.125 "umask 077 && read -r K \
      && sed -i \"s|^ENCRYPTION_KEY=.*|ENCRYPTION_KEY=\$K|\" /opt/hive/.env \
      && chmod 600 /opt/hive/.env"
unset NEW_ENCRYPTION_KEY

# 5. Redeploy + verify. Announce that users must re-enter stored BYOA keys.
```

## 3. App secret key (`SECRET_KEY`)

JWT signing key (`backend/auth.py`). Rotating invalidates all outstanding JWTs:
every user session is logged out and any long-lived agent/delegation tokens are
rejected until re-issued.

```bash
NEW_SECRET_KEY="$(openssl rand -hex 32)"

printf '%s' "$NEW_SECRET_KEY" | gh secret set SECRET_KEY -R rShetty/hive --body -

printf '%s' "$NEW_SECRET_KEY" \
  | ssh root@187.127.140.125 "umask 077 && read -r K \
      && sed -i \"s|^SECRET_KEY=.*|SECRET_KEY=\$K|\" /opt/hive/.env \
      && chmod 600 /opt/hive/.env"
unset NEW_SECRET_KEY

# Redeploy + verify, then confirm login works end-to-end.
```

## 4. Inter-service signing secret (`HIVE_SIGNING_SECRET`)

HMAC signing between the marketplace and agents/OpenClaw
(`backend/services/agent_client.py`, `openclaw_deployer.py`). Both sides must
switch together — rotate during a redeploy window.

```bash
NEW_SIGNING_SECRET="$(openssl rand -hex 32)"

printf '%s' "$NEW_SIGNING_SECRET" | gh secret set HIVE_SIGNING_SECRET -R rShetty/hive --body -

printf '%s' "$NEW_SIGNING_SECRET" \
  | ssh root@187.127.140.125 "umask 077 && read -r K \
      && sed -i \"s|^HIVE_SIGNING_SECRET=.*|HIVE_SIGNING_SECRET=\$K|\" /opt/hive/.env \
      && chmod 600 /opt/hive/.env"
unset NEW_SIGNING_SECRET

# Redeploy + verify, then run one agent deploy to exercise the signature path.
```

## 5. VPS SSH key (`VPS_SSH_KEY`, root@187.127.140.125)

Used by CI (`appleboy/ssh-action`) and `deploy.sh`. Also refreshes the on-box
OpenClaw alias key `/root/.ssh/openclaw_deploy_key`.

```bash
# 1. Generate a NEW dedicated deploy key locally (no passphrase for CI use):
ssh-keygen -t ed25519 -f ~/.ssh/hive_deploy_new \
  -C "hive-deploy-$(date +%Y%m%d)" -N ""

# 2. Install the public half WITHOUT removing the old one yet:
ssh-copy-id -i ~/.ssh/hive_deploy_new.pub root@187.127.140.125

# 3. Prove the new key works before revoking anything:
ssh -i ~/.ssh/hive_deploy_new root@187.127.140.125 'echo new-key-ok'

# 4. Point GitHub Actions at the new private key (stdin, never argv):
gh secret set VPS_SSH_KEY -R rShetty/hive < ~/.ssh/hive_deploy_new

# 5. Refresh the on-server OpenClaw alias key and lock it down:
ssh -i ~/.ssh/hive_deploy_new root@187.127.140.125 \
  "cp ~/.ssh/id_ed25519 /root/.ssh/openclaw_deploy_key \
   && chmod 600 /root/.ssh/openclaw_deploy_key"

# 6. Trigger a workflow_dispatch deploy and confirm it succeeds with the new key.

# 7. Only now revoke the old key: remove its line from authorized_keys,
#    delete old private copies, and check auth logs for unexpected usage:
ssh -i ~/.ssh/hive_deploy_new root@187.127.140.125 \
  "grep -v '<old-key-comment-or-line>' ~/.ssh/authorized_keys > ~/.ssh/authorized_keys.tmp \
   && mv ~/.ssh/authorized_keys.tmp ~/.ssh/authorized_keys \
   && chmod 600 ~/.ssh/authorized_keys \
   && lastb | head -20"
```

---

## Historical exposure checklist

- The following commits were previously exempted via allowlists in
  `.gitleaks.toml` instead of being rotated. Any secret present in them must be
  rotated per the sections above, regardless of whether the files still exist:
  - `4c2d4c48f8b11dcad4692ba3f1042b0462c27a8b`
  - `87dc5276f1603499168baedfab495743195c5338`
  - `479ef294764bc94d0132c113bc4e48448b5e3f64`
- Confirm the scan is clean over full history after rotating:

  ```bash
  gitleaks detect --source . --redact
  ```

- CI enforces this: gitleaks runs with `fetch-depth: 0` (full history) and a
  dedicated step fails the build if `.env` is ever tracked.
