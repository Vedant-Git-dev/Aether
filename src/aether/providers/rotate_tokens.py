"""Offline provider-token key rotation.

Usage:
    python -m aether.providers.rotate_tokens --check
    python -m aether.providers.rotate_tokens --apply

The command reads the token keyring from the environment (or .env), then
re-encrypts provider access tokens, refresh tokens, and OAuth state
verifiers under the active key. It never touches the content key or any
other table. Running it twice is safe; rows already on the active key are
skipped.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections import Counter

import asyncpg

from ..config import Settings
from ..memory.db import create_pool
from .crypto import TokenKeyError, TokenKeyring
from .store import _access_aad, _refresh_aad, _state_aad


async def _connection_rows(pool: asyncpg.Pool) -> list[asyncpg.Record]:
    return await pool.fetch(
        "SELECT provider, access_enc, refresh_enc, key_id FROM provider_connections"
    )


async def _state_rows(pool: asyncpg.Pool) -> list[asyncpg.Record]:
    return await pool.fetch(
        "SELECT state_hash, code_verifier_enc, key_id FROM provider_oauth_states"
    )


def _counts(rows: list[asyncpg.Record], active_id: str, known: set[str]) -> Counter[str]:
    counts: Counter[str] = Counter()
    for row in rows:
        key_id = str(row["key_id"])
        if key_id == active_id:
            counts["active"] += 1
        elif key_id in known:
            counts["previous"] += 1
        else:
            counts["unknown"] += 1
    return counts


def _report(label: str, rows: list[asyncpg.Record], active_id: str, known: set[str]) -> bool:
    counts = _counts(rows, active_id, known)
    print(
        f"{label}: {len(rows)} row(s) — {counts['active']} active, "
        f"{counts['previous']} previous, {counts['unknown']} unknown"
    )
    return counts["unknown"] == 0


async def _rotate_connections(pool: asyncpg.Pool, keyring: TokenKeyring) -> int:
    rotated = 0
    rows = await _connection_rows(pool)
    async with pool.acquire() as conn, conn.transaction():
        for row in rows:
            provider = str(row["provider"])
            key_id = str(row["key_id"])
            if key_id == keyring.active_id:
                continue
            access = keyring.decrypt_text(bytes(row["access_enc"]), _access_aad(provider), key_id)
            refresh = None
            if row["refresh_enc"] is not None:
                refresh = keyring.decrypt_text(
                    bytes(row["refresh_enc"]), _refresh_aad(provider), key_id
                )
            access_enc, new_key_id = keyring.encrypt_text(access, _access_aad(provider))
            refresh_enc = None
            if refresh is not None:
                refresh_enc, _ = keyring.encrypt_text(refresh, _refresh_aad(provider))
            await conn.execute(
                "UPDATE provider_connections SET access_enc = $2, refresh_enc = $3,"
                " key_id = $4, updated_at = now() WHERE provider = $1",
                provider,
                access_enc,
                refresh_enc,
                new_key_id,
            )
            rotated += 1
    return rotated


async def _rotate_states(pool: asyncpg.Pool, keyring: TokenKeyring) -> int:
    rotated = 0
    rows = await _state_rows(pool)
    async with pool.acquire() as conn, conn.transaction():
        for row in rows:
            state_hash = str(row["state_hash"])
            key_id = str(row["key_id"])
            if key_id == keyring.active_id:
                continue
            verifier = keyring.decrypt_text(
                bytes(row["code_verifier_enc"]), _state_aad(state_hash), key_id
            )
            blob, new_key_id = keyring.encrypt_text(verifier, _state_aad(state_hash))
            await conn.execute(
                "UPDATE provider_oauth_states SET code_verifier_enc = $2, key_id = $3"
                " WHERE state_hash = $1",
                state_hash,
                blob,
                new_key_id,
            )
            rotated += 1
    return rotated


async def _run(apply: bool) -> int:
    settings = Settings()
    try:
        keyring = TokenKeyring.from_values(
            active_key_b64=settings.token_encryption_key,
            active_id=settings.token_encryption_key_id,
            previous_key_b64=settings.token_encryption_key_previous,
            previous_id=settings.token_encryption_key_previous_id,
        )
    except TokenKeyError as exc:
        print(f"provider token keyring: {exc}", file=sys.stderr)
        return 2
    if keyring is None:
        print("provider token keyring: AETHER_TOKEN_ENCRYPTION_KEY is not set", file=sys.stderr)
        return 2
    if not settings.database_url:
        print("DATABASE_URL is not set", file=sys.stderr)
        return 2

    pool = await create_pool(settings.database_url)
    try:
        connections = await _connection_rows(pool)
        states = await _state_rows(pool)
        known = set(keyring.key_ids)
        ok = _report("provider connections", connections, keyring.active_id, known)
        ok = _report("OAuth states", states, keyring.active_id, known) and ok
        if not ok:
            print(
                "some rows use a key id the keyring doesn't know — set the matching "
                "AETHER_TOKEN_ENCRYPTION_KEY_PREVIOUS and ..._PREVIOUS_ID before rotating",
                file=sys.stderr,
            )
            return 1
        if not apply:
            return 0
        rotated_connections = await _rotate_connections(pool, keyring)
        rotated_states = await _rotate_states(pool, keyring)
        print(
            f"rotated {rotated_connections} connection(s) and "
            f"{rotated_states} OAuth state(s) to key {keyring.active_id}"
        )
        return 0
    finally:
        await pool.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Rotate the provider-token keyring offline.")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--check", action="store_true", help="report key ids; write nothing")
    group.add_argument("--apply", action="store_true", help="re-encrypt rows under the active key")
    args = parser.parse_args()
    raise SystemExit(asyncio.run(_run(apply=bool(args.apply))))


if __name__ == "__main__":
    main()
