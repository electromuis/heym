"""Plugin management API (install/uninstall/list/toggle) and Plugin API 2's options route."""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile, status
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.config import settings
from app.db.models import CredentialType, Plugin, User
from app.db.session import get_db
from app.models.plugin_schemas import (
    PluginCredentialTypeDef,
    PluginDoc,
    PluginManifest,
    PluginNodeDef,
    PluginNodeSummary,
    PluginSummary,
    walk_fields,
)
from app.services import plugin_credentials, plugin_loader, plugin_store
from app.services.encryption import decrypt_config
from app.services.plugin_store import PluginInstallError

router = APIRouter()


def require_plugins_enabled() -> None:
    if not settings.plugins_enabled:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Plugins are disabled. Set HEYM_PLUGINS_ENABLED=true to enable them.",
        )


def _admin_emails() -> set[str]:
    return {e.strip().lower() for e in settings.plugin_admin_emails.split(",") if e.strip()}


def require_plugin_admin(current_user: User) -> None:
    require_plugins_enabled()
    allowed = _admin_emails()
    if not allowed or current_user.email.strip().lower() not in allowed:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You are not allowed to manage plugins.",
        )


def _safe_icon_path(plugin_id: str, filename: str) -> Path | None:
    """Resolve an icon file inside the plugin package dir, guarding traversal."""
    if not filename:
        return None
    package_root = (plugin_store.plugins_root() / plugin_id).resolve()
    candidate = (package_root / filename).resolve()
    try:
        candidate.relative_to(package_root)
    except ValueError:
        return None
    return candidate if candidate.is_file() else None


def _resolve_icon(plugin_id: str, manifest: PluginManifest, node_key: str | None) -> Path | None:
    """Find the icon for a node (its own icon) or fall back to the package icon."""
    if node_key:
        node = manifest.get_node(node_key)
        if node is not None and node.icon:
            node_icon = _safe_icon_path(plugin_id, node.icon)
            if node_icon is not None:
                return node_icon
    return _safe_icon_path(plugin_id, "icon.svg")


def _to_summary(plugin: Plugin) -> PluginSummary:
    manifest = PluginManifest.model_validate(plugin.manifest)
    api2 = manifest.plugin_api >= 2
    nodes = [
        PluginNodeSummary(
            key=node.key,
            name=node.name,
            kind=node.kind,
            description=node.description,
            fields=node.fields,
            dsl_hint=node.dsl_hint,
            doc_slug=node.doc_slug,
            has_icon=_resolve_icon(plugin.plugin_id, manifest, node.key) is not None,
            **(_api2_node_keys(manifest, node) if api2 else {}),
        )
        for node in manifest.resolved_nodes()
    ]
    return PluginSummary(
        id=plugin.plugin_id,
        name=plugin.name,
        version=plugin.version,
        kind=plugin.kind,
        description=plugin.description,
        enabled=plugin.enabled,
        nodes=nodes,
        has_icon=_safe_icon_path(plugin.plugin_id, "icon.svg") is not None,
        plugin_api=manifest.plugin_api,
        palette=manifest.palette if api2 else None,
        credentials=[_public_credential_type(c) for c in manifest.credentials] if api2 else None,
    )


def _api2_node_keys(manifest: PluginManifest, node: PluginNodeDef) -> dict:
    """The Plugin API 2 keys of a node's listing; the colour already falls back to group and package."""
    return {
        "group": node.group,
        "color": manifest.node_color(node),
        "subtitle": node.subtitle,
        "exits": node.exits,
        "exits_from": node.exits_from,
        "inputs": node.inputs,
        "sample": node.sample,
        "source": node.source,
    }


def _public_credential_type(definition: PluginCredentialTypeDef) -> PluginCredentialTypeDef:
    """A credential type as the browser sees it: a secret field never carries a default."""
    fields = [f.model_copy(update={"default": None}) if f.secret else f for f in definition.fields]
    return definition.model_copy(update={"fields": fields})


@router.get("", response_model=list[PluginSummary])
async def list_plugins(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[PluginSummary]:
    require_plugins_enabled()
    rows = (await db.execute(select(Plugin))).scalars().all()
    return [_to_summary(p) for p in rows]


@router.get("/{plugin_id}/doc", response_model=PluginDoc)
async def get_plugin_doc(
    plugin_id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> PluginDoc:
    require_plugins_enabled()
    plugin = (
        await db.execute(select(Plugin).where(Plugin.plugin_id == plugin_id))
    ).scalar_one_or_none()
    if plugin is None:
        raise HTTPException(status_code=404, detail="Plugin not found")
    readme = plugin_store.plugins_root() / plugin_id / "README.md"
    markdown = readme.read_text() if readme.exists() else ""
    manifest = PluginManifest.model_validate(plugin.manifest)
    return PluginDoc(
        id=plugin_id,
        name=plugin.name,
        doc_slug=manifest.resolved_doc_slug(),
        markdown=markdown,
    )


@router.get("/{plugin_id}/icon")
async def get_plugin_icon(
    plugin_id: str,
    node: str | None = Query(None, description="Resolve the icon for a specific node key"),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> FileResponse:
    require_plugins_enabled()
    plugin = (
        await db.execute(select(Plugin).where(Plugin.plugin_id == plugin_id))
    ).scalar_one_or_none()
    if plugin is None:
        raise HTTPException(status_code=404, detail="Plugin not found")
    manifest = PluginManifest.model_validate(plugin.manifest)
    icon = _resolve_icon(plugin_id, manifest, node)
    if icon is None:
        raise HTTPException(status_code=404, detail="Icon not found")
    return FileResponse(icon, media_type="image/svg+xml")


@router.post("/install", response_model=PluginSummary)
async def install_plugin(
    file: UploadFile = File(...),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> PluginSummary:
    require_plugin_admin(current_user)
    data = await file.read()
    try:
        manifest = plugin_store.extract_and_validate(data, plugin_store.plugins_root())
    except PluginInstallError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    plugin_loader.clear_cache()
    existing = (
        await db.execute(select(Plugin).where(Plugin.plugin_id == manifest.id))
    ).scalar_one_or_none()
    if existing is None:
        existing = Plugin(plugin_id=manifest.id)
        db.add(existing)
    existing.name = manifest.name
    existing.version = manifest.version
    existing.kind = manifest.package_kind()
    existing.description = manifest.description
    existing.manifest = manifest.model_dump(by_alias=True)
    existing.enabled = True
    existing.installed_by = current_user.email
    await db.commit()
    await db.refresh(existing)
    return _to_summary(existing)


@router.patch("/{plugin_id}", response_model=PluginSummary)
async def set_plugin_enabled(
    plugin_id: str,
    enabled: bool,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> PluginSummary:
    require_plugin_admin(current_user)
    plugin = (
        await db.execute(select(Plugin).where(Plugin.plugin_id == plugin_id))
    ).scalar_one_or_none()
    if plugin is None:
        raise HTTPException(status_code=404, detail="Plugin not found")
    plugin.enabled = enabled
    await db.commit()
    await db.refresh(plugin)
    return _to_summary(plugin)


@router.delete("/{plugin_id}")
async def uninstall_plugin(
    plugin_id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    require_plugin_admin(current_user)
    plugin = (
        await db.execute(select(Plugin).where(Plugin.plugin_id == plugin_id))
    ).scalar_one_or_none()
    if plugin is not None:
        await db.delete(plugin)
        await db.commit()
    plugin_store.remove_plugin_dir(plugin_id, plugin_store.plugins_root())
    plugin_loader.clear_cache()
    return {"status": "uninstalled", "plugin_id": plugin_id}


#: How long a form waits for a plugin's options before showing its static ones.
OPTIONS_TIMEOUT_SECONDS = 10.0


class PluginOptionsRequest(BaseModel):
    node: str
    field: str
    config: dict[str, Any] = Field(default_factory=dict)


class PluginOption(BaseModel):
    label: str
    value: str


async def _enabled_manifest(db: AsyncSession, plugin_id: str) -> PluginManifest:
    plugin = (
        await db.execute(select(Plugin).where(Plugin.plugin_id == plugin_id))
    ).scalar_one_or_none()
    if plugin is None or not plugin.enabled:
        raise HTTPException(status_code=404, detail="Plugin not found")
    return PluginManifest.model_validate(plugin.manifest)


async def _with_credentials(
    db: AsyncSession,
    manifest: PluginManifest,
    node: PluginNodeDef,
    config: dict,
    user: User,
    secrets: list[str],
) -> dict:
    """The form's values with each top-level credential field replaced by the user's values."""
    from app.api.credentials import _get_accessible_credential

    out = dict(config)
    for field in node.fields:
        if field.type != "credential" or not out.get(field.key):
            continue
        try:
            credential_id = uuid.UUID(str(out[field.key]))
        except ValueError:
            out[field.key] = None
            continue
        credential = await _get_accessible_credential(db, credential_id, user)
        if credential is None or credential.type != CredentialType.plugin:
            out[field.key] = None
            continue
        stored = decrypt_config(credential.encrypted_config)
        out[field.key] = plugin_credentials.values_for_run(
            stored, manifest.id, field.credential_type or "", field.label
        )
        secrets.extend(plugin_credentials.secret_values(stored))
    return out


def _as_options(result: object) -> list[PluginOption]:
    if not isinstance(result, list):
        raise ValueError("an options function must return a list")
    options: list[PluginOption] = []
    for item in result:
        if isinstance(item, dict) and "value" in item:
            value = str(item["value"])
            options.append(PluginOption(label=str(item.get("label", value)), value=value))
        elif isinstance(item, (str, int, float)) and not isinstance(item, bool):
            options.append(PluginOption(label=str(item), value=str(item)))
        else:
            raise ValueError("an option is a text or an object with label and value")
    return options


@router.post("/{plugin_id}/options", response_model=list[PluginOption])
async def load_plugin_options(
    plugin_id: str,
    body: PluginOptionsRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[PluginOption]:
    """A pick-list's options from the plugin (`loadOptions`), for the form as it is filled in."""
    require_plugins_enabled()
    manifest = await _enabled_manifest(db, plugin_id)
    node = manifest.get_node(body.node)
    if node is None:
        raise HTTPException(status_code=404, detail="Plugin node not found")
    field = next((f for f in walk_fields(node.fields) if f.key == body.field), None)
    function = field.load_options_function() if field is not None else None
    if function is None:
        raise HTTPException(status_code=404, detail="This field has no loaded options")
    secrets: list[str] = []
    config = await _with_credentials(db, manifest, node, body.config, current_user, secrets)
    ctx = {"plugin_id": plugin_id, "node_key": node.key, "user_id": str(current_user.id)}
    try:
        result = await asyncio.wait_for(
            asyncio.to_thread(
                plugin_loader.call_handler,
                plugin_id,
                plugin_store.plugins_root(),
                function,
                {"config": config, "ctx": ctx},
            ),
            timeout=OPTIONS_TIMEOUT_SECONDS,
        )
        return _as_options(result)
    except TimeoutError as exc:
        raise HTTPException(status_code=504, detail="The plugin took too long") from exc
    except (plugin_loader.PluginRuntimeError, ValueError) as exc:
        detail = plugin_credentials.mask_text(str(exc), secrets)
        raise HTTPException(status_code=422, detail=detail) from exc
