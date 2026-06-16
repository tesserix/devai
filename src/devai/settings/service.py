"""SettingsService — persists connectors and provisions their secrets.

Storage: PostgreSQL ``user_settings`` table when a pool is available, else an
in-memory dict (dev/degraded). Secret *values* are written to the secrets
backend (GCP SM) and only the returned ``SecretRef`` name is persisted.

The table schema lives in the ``tesserix-k8s`` infra repo (per the project's
SQL-in-infra rule); this service only issues idempotent DML and tolerates the
table being absent (degrades to the in-memory store).
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

from devai.settings.models import (
    CONNECTOR_BY_KEY,
    Connector,
    Scope,
)

if TYPE_CHECKING:
    from devai.adapters.secrets.base import SecretsAdapter

logger = logging.getLogger(__name__)

_TABLE = "user_settings"

# Process-global instance for call sites with no constructor injection
# (tool handlers, adapters). Last constructed wins — each process builds
# exactly one real service at startup.
_GLOBAL: SettingsService | None = None


def get_settings_service() -> SettingsService | None:
    return _GLOBAL


class SettingsService:
    """CRUD for connectors + secret provisioning, scoped user/team/tenant/global."""

    def __init__(self, *, pool: Any = None, secrets: SecretsAdapter | None = None) -> None:
        self._pool = pool
        self._secrets = secrets
        self._mem: dict[str, Connector] = {}  # fallback store, keyed by storage_key
        global _GLOBAL  # noqa: PLW0603 — deliberate process-global registration
        _GLOBAL = self

    @property
    def has_db(self) -> bool:
        return self._pool is not None

    async def secrets_writable(self) -> bool:
        if self._secrets is None:
            return False
        try:
            return await self._secrets.can_write()
        except Exception:  # noqa: BLE001
            return False

    # ── persistence ──────────────────────────────────────────────────────

    async def _save_row(self, c: Connector) -> None:
        if self._pool is None:
            self._mem[c.storage_key()] = c
            return
        try:
            await self._pool.execute(
                f"""INSERT INTO {_TABLE}
                    (scope, scope_id, connector_key, instance_id, provider,
                     prefs, secret_refs, enabled, updated_by, updated_at)
                    VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9, now())
                    ON CONFLICT (scope, scope_id, connector_key, instance_id)
                    DO UPDATE SET provider=$5, prefs=$6, secret_refs=$7,
                                  enabled=$8, updated_by=$9, updated_at=now()""",
                c.scope.value,
                c.scope_id,
                c.connector_key,
                c.instance_id,
                c.provider,
                json.dumps(c.prefs),
                json.dumps(c.secret_refs),
                c.enabled,
                c.updated_by,
            )
        except Exception:
            logger.exception("settings: DB save failed for %s — using memory", c.storage_key())
            self._mem[c.storage_key()] = c

    async def _load_rows(self, scope: Scope, scope_id: str) -> list[Connector]:
        if self._pool is None:
            return [c for c in self._mem.values() if c.scope == scope and c.scope_id == scope_id]
        try:
            rows = await self._pool.fetch(
                f"""SELECT scope, scope_id, connector_key, instance_id, provider,
                           prefs, secret_refs, enabled, updated_by,
                           to_char(updated_at,'YYYY-MM-DD\"T\"HH24:MI:SSZ') AS updated_at
                    FROM {_TABLE} WHERE scope=$1 AND scope_id=$2""",
                scope.value,
                scope_id,
            )
            return [_row_to_connector(r) for r in rows]
        except Exception:
            logger.exception("settings: DB load failed (%s:%s) — using memory", scope.value, scope_id)
            return [c for c in self._mem.values() if c.scope == scope and c.scope_id == scope_id]

    # ── public API ───────────────────────────────────────────────────────

    async def list_connectors(self, scope: Scope, scope_id: str) -> list[Connector]:
        return await self._load_rows(scope, scope_id)

    async def list_all_by_key(self, connector_key: str) -> list[Connector]:
        """Every connector of one kind across ALL users/scopes. Used by the
        kagent active-variants reconcile (the union of which models users
        enabled) — never returns secret values, only prefs/provider/scope.
        """
        if self._pool is None:
            return [c for c in self._mem.values() if c.connector_key == connector_key]
        try:
            rows = await self._pool.fetch(
                f"""SELECT scope, scope_id, connector_key, instance_id, provider,
                           prefs, secret_refs, enabled, updated_by,
                           to_char(updated_at,'YYYY-MM-DD\"T\"HH24:MI:SSZ') AS updated_at
                    FROM {_TABLE} WHERE connector_key=$1""",
                connector_key,
            )
            return [_row_to_connector(r) for r in rows]
        except Exception:
            logger.exception("settings: list_all_by_key failed (%s)", connector_key)
            return [c for c in self._mem.values() if c.connector_key == connector_key]

    async def list_user_connectors_by_email(self, email: str) -> list[Connector]:
        """User-scope connectors for an email, matched by scope_id OR
        updated_by. Connectors are saved under the GIP uid (principal.uid)
        but runs only carry the email (triggered_by) — this bridges the two
        so per-user LLM resolves at run time regardless of uid/email keying.
        """
        if not email:
            return []
        if self._pool is None:
            return [
                c
                for c in self._mem.values()
                if c.scope == Scope.USER and (c.scope_id == email or c.updated_by == email)
            ]
        try:
            rows = await self._pool.fetch(
                f"""SELECT scope, scope_id, connector_key, instance_id, provider,
                           prefs, secret_refs, enabled, updated_by,
                           to_char(updated_at,'YYYY-MM-DD\"T\"HH24:MI:SSZ') AS updated_at
                    FROM {_TABLE}
                    WHERE scope='user' AND (scope_id=$1 OR updated_by=$1)""",
                email,
            )
            return [_row_to_connector(r) for r in rows]
        except Exception:
            logger.exception("settings: by-email connector load failed (%s)", email)
            return [
                c
                for c in self._mem.values()
                if c.scope == Scope.USER and (c.scope_id == email or c.updated_by == email)
            ]

    async def upsert_connector(
        self,
        *,
        scope: Scope,
        scope_id: str,
        connector_key: str,
        provider: str,
        instance_id: str = "default",
        prefs: dict[str, Any] | None = None,
        secret_values: dict[str, str] | None = None,
        updated_by: str = "",
    ) -> Connector:
        """Create/update a connector. Secret values are written to the backend;
        only their refs are persisted. Non-secret fields go straight to prefs.
        """
        spec = CONNECTOR_BY_KEY.get(connector_key)
        if spec is None:
            raise ValueError(f"unknown connector: {connector_key}")

        secret_field_keys = {f.key for f in spec.fields if f.secret}

        # Load any existing instance so we keep prior secret refs not re-supplied.
        existing = await self._get(scope, scope_id, connector_key, instance_id)
        secret_refs = dict(existing.secret_refs) if existing else {}

        # Provision secrets that were supplied with a non-empty value.
        for fkey, value in (secret_values or {}).items():
            if fkey not in secret_field_keys or not value:
                continue
            if self._secrets is None:
                raise RuntimeError("no secrets backend configured — cannot store secret")
            logical = f"devai-{scope.value}-{scope_id or 'global'}-{connector_key}-{instance_id}-{fkey}"
            ref = await self._secrets.set_secret(
                logical,
                value,
                labels={"scope": scope.value, "connector": connector_key, "field": fkey},
            )
            secret_refs[fkey] = ref.name

        # Non-secret prefs: MERGE over the existing instance, don't replace.
        # A connector like LLM holds per-provider config (claude_model,
        # openai_model, …); saving one provider must not drop the others'
        # settings. New non-empty values win; unset fields keep their prior
        # value. (Secret-keyed values are never persisted to prefs.)
        clean_prefs = dict(existing.prefs) if existing else {}
        for k, v in (prefs or {}).items():
            if k in secret_field_keys:
                continue
            if v in ("", None):
                continue
            clean_prefs[k] = v

        connector = Connector(
            scope=scope,
            scope_id=scope_id,
            connector_key=connector_key,
            provider=provider,
            instance_id=instance_id,
            prefs=clean_prefs,
            secret_refs=secret_refs,
            updated_by=updated_by,
        )
        await self._save_row(connector)
        # Audit trail — WHO changed WHICH connector and which secret fields
        # were (re)provisioned. Never records secret values, only field names.
        await self._audit(
            action="settings.connector.upsert",
            actor=updated_by or scope_id,
            connector=connector,
            details={
                "provider": provider,
                "secrets_set": sorted(secret_refs.keys()),
                "secret_refs": sorted(secret_refs.values()),  # SM names, not values
                "prefs_keys": sorted(clean_prefs.keys()),
            },
        )
        return connector

    async def delete_connector(
        self, scope: Scope, scope_id: str, connector_key: str, instance_id: str = "default", *, actor: str = ""
    ) -> bool:
        existing = await self._get(scope, scope_id, connector_key, instance_id)
        deleted_refs: list[str] = []
        if existing and self._secrets is not None:
            for ref_name in existing.secret_refs.values():
                try:
                    await self._secrets.delete_secret(ref_name)
                    deleted_refs.append(ref_name)
                except Exception:  # noqa: BLE001
                    logger.warning("settings: secret delete failed for %s", ref_name)
        key = Connector(
            scope=scope, scope_id=scope_id, connector_key=connector_key, instance_id=instance_id
        ).storage_key()
        await self._audit(
            action="settings.connector.delete",
            actor=actor or scope_id,
            connector=Connector(scope=scope, scope_id=scope_id, connector_key=connector_key, instance_id=instance_id),
            details={"deleted_secret_refs": sorted(deleted_refs)},
        )
        if self._pool is None:
            return self._mem.pop(key, None) is not None
        try:
            await self._pool.execute(
                f"DELETE FROM {_TABLE} WHERE scope=$1 AND scope_id=$2 AND connector_key=$3 AND instance_id=$4",
                scope.value,
                scope_id,
                connector_key,
                instance_id,
            )
            return True
        except Exception:
            logger.exception("settings: DB delete failed")
            return self._mem.pop(key, None) is not None

    async def clear_secret_field(
        self,
        scope: Scope,
        scope_id: str,
        connector_key: str,
        field_key: str,
        instance_id: str = "default",
        *,
        actor: str = "",
    ) -> bool:
        """Remove ONE secret field from a connector (e.g. drop just the
        Anthropic key) without touching the rest of the connector.

        Deletes the backend secret and drops its ref; the connector and its
        other secrets/prefs stay. Returns False when the field isn't set.
        """
        existing = await self._get(scope, scope_id, connector_key, instance_id)
        if not existing or field_key not in existing.secret_refs:
            return False
        ref_name = existing.secret_refs.pop(field_key)
        if self._secrets is not None:
            try:
                await self._secrets.delete_secret(ref_name)
            except Exception:  # noqa: BLE001 — drop the ref regardless; orphan SM version is harmless
                logger.warning("settings: secret delete failed for %s", ref_name)
        await self._save_row(existing)
        await self._audit(
            action="settings.connector.secret.clear",
            actor=actor or scope_id,
            connector=existing,
            details={"cleared_field": field_key, "deleted_secret_ref": ref_name},
        )
        return True

    async def _audit(self, *, action: str, actor: str, connector: Connector, details: dict[str, Any]) -> None:
        """Write a queryable audit_log entry for a settings change.

        Records WHO (actor) did WHAT (action) to WHICH connector (entity_ref =
        the scope:scope_id:connector:instance key) plus non-secret detail.
        Never raises — auditing must not break the save — and is a no-op when
        no DB pool is wired (in-memory/test mode).
        """
        if self._pool is None:
            return
        try:
            await self._pool.execute(
                """INSERT INTO audit_log
                   (run_id, agent_name, action, entity_type, entity_ref, details, actor, actor_type)
                   VALUES (NULL, NULL, $1, 'settings_connector', $2, $3, $4, 'user')""",
                action,
                connector.storage_key(),
                json.dumps(details),
                actor,
            )
        except Exception:  # noqa: BLE001
            logger.warning("settings: audit write failed (%s)", action, exc_info=True)

    async def resolve_secret(self, ref_name: str) -> str | None:
        """Resolve a stored secret ref name back to its value (for the overlay)."""
        if self._secrets is None or not ref_name:
            return None
        return await self._secrets.get_secret(ref_name)

    async def _get(self, scope: Scope, scope_id: str, connector_key: str, instance_id: str) -> Connector | None:
        for c in await self._load_rows(scope, scope_id):
            if c.connector_key == connector_key and c.instance_id == instance_id:
                return c
        return None


def _row_to_connector(r: Any) -> Connector:
    prefs = r["prefs"]
    refs = r["secret_refs"]
    return Connector(
        scope=Scope(r["scope"]),
        scope_id=r["scope_id"],
        connector_key=r["connector_key"],
        instance_id=r["instance_id"],
        provider=r["provider"] or "",
        prefs=json.loads(prefs) if isinstance(prefs, str) else (prefs or {}),
        secret_refs=json.loads(refs) if isinstance(refs, str) else (refs or {}),
        enabled=r["enabled"],
        updated_by=r["updated_by"] or "",
        updated_at=r["updated_at"] or "",
    )


__all__ = ["SettingsService", "get_settings_service"]
