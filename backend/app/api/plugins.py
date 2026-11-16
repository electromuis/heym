"""Plugin management API (install/uninstall/list/toggle)."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile, status
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.config import settings
from app.db.models import Plugin, User
from app.db.session import get_db
from app.models.plugin_schemas import (
    PluginCredentialTypeDef,
    PluginDoc,
    PluginManifest,
    PluginNodeDef,
    PluginNodeSummary,
    PluginSummary,
)
from app.services import plugin_loader, plugin_store
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
