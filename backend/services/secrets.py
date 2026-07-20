"""Helpers for handling secret values (model API keys, tokens).

Secrets are passed to agent runtimes as **files** (mounted read-only) rather
than as plaintext environment variables, so they do not show up in
``docker inspect`` / ``docker ps`` output or in the generated compose file.
The agent runtime reads them via ``*_API_KEY_FILE`` (see agent_app/main.py).
"""
import os

# Suffixes that identify a value as a secret worth keeping out of the env dump.
SECRET_SUFFIXES = ("_API_KEY", "_SECRET", "_TOKEN", "APIKEY")


def is_secret_key(key: str) -> bool:
    return any(key.endswith(s) for s in SECRET_SUFFIXES)


def split_secrets(env_vars: dict) -> tuple[dict, dict]:
    """Split env vars into (plain_env, secrets).

    ``secrets`` maps the original env name -> secret value. The caller is
    responsible for writing each value to a file and exposing it to the
    runtime via a ``<NAME>_FILE`` env var.
    """
    plain: dict = {}
    secrets: dict = {}
    for k, v in (env_vars or {}).items():
        if v is None:
            continue
        if is_secret_key(k):
            secrets[k] = str(v)
        else:
            plain[k] = str(v)
    return plain, secrets
