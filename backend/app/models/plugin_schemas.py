"""Pydantic models for plugin manifests and the plugins API.

A plugin *package* (one zip / one installed entry) may expose multiple *nodes*,
each of which becomes a draggable node on the canvas. A node is either an
``action`` or a ``trigger`` and maps to a function in the package's handler
module.

Legacy single-node manifests (top-level ``kind`` + ``fields``) are still accepted
and synthesized into a single node.

Plugin API 2 (``"pluginApi": 2``, see ``docs/plugin-api-2.md``) adds optional keys:
a palette section, credential types, node colours, exits, inputs, richer field
types and trigger sources. A manifest without ``pluginApi`` is API 1: unknown and
API 2 keys are ignored, as before. An API 2 manifest is strict about unknown keys.
"""

from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

_PLUGIN_ID_RE = re.compile(r"^[a-z0-9-]+$")
_NODE_KEY_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_GROUP_KEY_RE = re.compile(r"^[a-z][a-z0-9_-]*$")
_EXIT_KEY_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")
_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")

#: The highest plugin API version this Heym understands.
SUPPORTED_PLUGIN_API = 2

PluginKind = Literal["action", "trigger"]
PluginFieldType = Literal[
    "string",
    "number",
    "boolean",
    "select",
    # Plugin API 2
    "text",
    "code",
    "json",
    "expression",
    "list",
    "keyValue",
    "multiselect",
    "credential",
    "condition",
]
_API1_FIELD_TYPES = {"string", "number", "boolean", "select"}
_LIST_COLUMN_TYPES = {"string", "number", "boolean", "select", "text", "expression", "condition"}
_CREDENTIAL_FIELD_TYPES = {"string", "number", "boolean", "select", "text"}
_OPTION_FIELD_TYPES = {"select", "multiselect"}
CodeLanguage = Literal["python", "javascript", "json", "sql", "markdown", "text"]

# Keys an API 2 manifest adds. An API 1 manifest drops them before validation, so a
# plugin written for stock Heym cannot change meaning by carrying one of them.
_API2_PACKAGE_KEYS = ("palette", "credentials")
_API2_NODE_KEYS = (
    "group",
    "color",
    "subtitle",
    "exits",
    "exitsFrom",
    "inputs",
    "sample",
    "source",
)
_API2_FIELD_KEYS = (
    "help",
    "placeholder",
    "advanced",
    "section",
    "showIf",
    "allowExpression",
    "searchable",
    "loadOptions",
    "rows",
    "language",
    "credentialType",
    "fields",
)


def _check_color(value: str | None) -> str | None:
    if value is not None and not _COLOR_RE.match(value):
        raise ValueError("colour must be #rrggbb")
    return value


def _check_pattern(value: str, pattern: re.Pattern[str], what: str) -> str:
    if not pattern.match(value):
        raise ValueError(f"{what} must match {pattern.pattern}")
    return value


class PluginFieldOption(BaseModel):
    label: str
    value: str


class PluginField(BaseModel):
    key: str
    label: str
    type: PluginFieldType = "string"
    required: bool = False
    secret: bool = False
    default: Any = None
    options: list[PluginFieldOption] = Field(default_factory=list)
    dynamic: bool = False
    expression: bool = False
    # Plugin API 2
    help: str = ""
    placeholder: str = ""
    advanced: bool = False
    section: str = ""
    show_if: dict[str, Any] | None = Field(default=None, alias="showIf")
    allow_expression: bool = Field(default=False, alias="allowExpression")
    searchable: bool = False
    load_options: bool | str = Field(default=False, alias="loadOptions")
    rows: int | None = None
    language: CodeLanguage | None = None
    credential_type: str | None = Field(default=None, alias="credentialType")
    fields: list[PluginField] = Field(default_factory=list)

    model_config = {"populate_by_name": True}

    @model_validator(mode="after")
    def _check_type_keys(self) -> PluginField:
        where = f"field {self.key}"
        if self.type == "list":
            if not self.fields:
                raise ValueError(f"{where}: a list needs `fields`")
            for column in self.fields:
                if column.type not in _LIST_COLUMN_TYPES:
                    raise ValueError(f"{where}: a list column can't be of type {column.type}")
            check_show_if(self.fields, where)
        elif self.fields:
            raise ValueError(f"{where}: only a list has `fields`")
        if self.type == "credential" and not self.credential_type:
            raise ValueError(f"{where}: a credential field needs `credentialType`")
        if self.type != "credential" and self.credential_type:
            raise ValueError(f"{where}: only a credential field has `credentialType`")
        if self.load_options and self.type not in _OPTION_FIELD_TYPES:
            raise ValueError(f"{where}: `loadOptions` is for select and multiselect")
        if self.allow_expression and self.type not in _OPTION_FIELD_TYPES:
            raise ValueError(f"{where}: `allowExpression` is for select and multiselect")
        if self.language is not None and self.type != "code":
            raise ValueError(f"{where}: `language` is for code fields")
        if self.rows is not None and (self.type != "text" or self.rows < 1):
            raise ValueError(f"{where}: `rows` is a positive number for text fields")
        return self

    def load_options_function(self) -> str | None:
        """The handler function that loads this field's options, if any."""
        if isinstance(self.load_options, str) and self.load_options:
            return self.load_options
        return f"options_{self.key}" if self.load_options else None


def check_show_if(fields: list[PluginField], where: str) -> None:
    """Field keys are unique and every `showIf` names another field of the same form."""
    keys = {f.key for f in fields}
    if len(keys) != len(fields):
        raise ValueError(f"{where}: field keys must be unique")
    for f in fields:
        for other in (f.show_if or {}).keys():
            if other not in keys or other == f.key:
                raise ValueError(f"{where}: field {f.key}: `showIf` names an unknown field")


class PluginExit(BaseModel):
    key: str
    label: str = ""
    color: str | None = None

    @field_validator("key")
    @classmethod
    def _validate_key(cls, v: str) -> str:
        return _check_pattern(v, _EXIT_KEY_RE, "exit key")

    @field_validator("color")
    @classmethod
    def _validate_color(cls, v: str | None) -> str | None:
        return _check_color(v)


class PluginInput(BaseModel):
    key: str
    label: str = ""

    @field_validator("key")
    @classmethod
    def _validate_key(cls, v: str) -> str:
        return _check_pattern(v, _EXIT_KEY_RE, "input key")


class PluginExitsFrom(BaseModel):
    field: str
    column: str


class PluginPaletteGroup(BaseModel):
    key: str
    label: str
    color: str | None = None

    @field_validator("key")
    @classmethod
    def _validate_key(cls, v: str) -> str:
        return _check_pattern(v, _GROUP_KEY_RE, "group key")

    @field_validator("color")
    @classmethod
    def _validate_color(cls, v: str | None) -> str | None:
        return _check_color(v)


class PluginPalette(BaseModel):
    section: str = ""
    position: Literal["top", "bottom"] = "bottom"
    color: str | None = None
    icon: str = ""
    groups: list[PluginPaletteGroup] = Field(default_factory=list)

    @field_validator("color")
    @classmethod
    def _validate_color(cls, v: str | None) -> str | None:
        return _check_color(v)

    @model_validator(mode="after")
    def _unique_groups(self) -> PluginPalette:
        keys = [g.key for g in self.groups]
        if len(keys) != len(set(keys)):
            raise ValueError("palette group keys must be unique")
        return self


class PluginCredentialTypeDef(BaseModel):
    """A credential type a package declares; stored as Heym credential type ``plugin``."""

    key: str
    name: str
    description: str = ""
    fields: list[PluginField] = Field(default_factory=list)
    test: str = ""

    @field_validator("key")
    @classmethod
    def _validate_key(cls, v: str) -> str:
        return _check_pattern(v, _NODE_KEY_RE, "credential type key")

    @model_validator(mode="after")
    def _check_fields(self) -> PluginCredentialTypeDef:
        where = f"credential type {self.key}"
        for f in self.fields:
            if f.type not in _CREDENTIAL_FIELD_TYPES:
                raise ValueError(f"{where}: field {f.key} can't be of type {f.type}")
        check_show_if(self.fields, where)
        return self

    def secret_keys(self) -> set[str]:
        return {f.key for f in self.fields if f.secret}


class PluginNodeDef(BaseModel):
    """A single node exposed by a plugin package."""

    key: str
    name: str
    kind: PluginKind
    function: str = ""
    description: str = ""
    icon: str = ""
    fields: list[PluginField] = Field(default_factory=list)
    dsl_hint: str = Field(default="", alias="dslHint")
    doc_slug: str = Field(default="", alias="docSlug")
    # Plugin API 2
    group: str | None = None
    color: str | None = None
    subtitle: str = ""
    exits: list[PluginExit] = Field(default_factory=list)
    exits_from: PluginExitsFrom | None = Field(default=None, alias="exitsFrom")
    inputs: list[PluginInput] = Field(default_factory=list)
    sample: dict[str, Any] | None = None
    source: Literal["push", "loop"] | None = None

    model_config = {"populate_by_name": True}

    @field_validator("key")
    @classmethod
    def _validate_key(cls, v: str) -> str:
        if not _NODE_KEY_RE.match(v):
            raise ValueError("Node key must match ^[A-Za-z0-9_-]+$")
        return v

    @field_validator("color")
    @classmethod
    def _validate_color(cls, v: str | None) -> str | None:
        return _check_color(v)

    @model_validator(mode="after")
    def _default_function(self) -> PluginNodeDef:
        # A trigger with a `source` calls a function only when the manifest names one; its
        # event is the output otherwise (docs/plugin-api-2.md, "Triggers").
        if not self.function and self.source is None:
            self.function = "run" if self.kind == "action" else "trigger"
        return self

    @model_validator(mode="after")
    def _check_api2_keys(self) -> PluginNodeDef:
        where = f"node {self.key}"
        exit_keys = [e.key for e in self.exits]
        if len(exit_keys) != len(set(exit_keys)):
            raise ValueError(f"{where}: exit keys must be unique")
        input_keys = [i.key for i in self.inputs]
        if len(input_keys) != len(set(input_keys)):
            raise ValueError(f"{where}: input keys must be unique")
        if self.source is not None and self.kind != "trigger":
            raise ValueError(f"{where}: only a trigger has a `source`")
        if self.inputs and self.kind == "trigger":
            raise ValueError(f"{where}: a trigger has no inputs")
        check_show_if(self.fields, where)
        if self.exits_from is not None:
            list_field = next((f for f in self.fields if f.key == self.exits_from.field), None)
            if list_field is None or list_field.type != "list":
                raise ValueError(f"{where}: `exitsFrom` must name a list field")
            if not any(c.key == self.exits_from.column for c in list_field.fields):
                raise ValueError(f"{where}: `exitsFrom` names an unknown column")
        return self

    def has_exits(self) -> bool:
        return bool(self.exits) or self.exits_from is not None

    def exit_keys(self, config: dict | None = None) -> list[str]:
        """The node's exits in order: the fixed ones, then those of the `exitsFrom` column."""
        keys = [e.key for e in self.exits]
        if self.exits_from is not None:
            rows = (config or {}).get(self.exits_from.field)
            for row in rows if isinstance(rows, list) else []:
                value = row.get(self.exits_from.column) if isinstance(row, dict) else None
                if is_exit_value(value) and value not in keys:
                    keys.append(value)
        return keys


def is_exit_value(value: object) -> bool:
    """Whether a value of an `exitsFrom` column names an exit: non-empty text on one line."""
    return isinstance(value, str) and bool(value.strip()) and "\n" not in value


class PluginManifest(BaseModel):
    id: str
    name: str
    version: str
    description: str = ""
    entry: str = "handler.py"
    dependencies: list[str] = Field(default_factory=list)
    nodes: list[PluginNodeDef] = Field(default_factory=list)
    # Legacy single-node fields (optional; synthesized into `nodes`).
    kind: PluginKind | None = None
    fields: list[PluginField] = Field(default_factory=list)
    dsl_hint: str = Field(default="", alias="dslHint")
    doc_slug: str = Field(default="", alias="docSlug")
    # Plugin API 2
    plugin_api: int = Field(default=1, alias="pluginApi")
    palette: PluginPalette | None = None
    credentials: list[PluginCredentialTypeDef] = Field(default_factory=list)

    model_config = {"populate_by_name": True}

    @model_validator(mode="before")
    @classmethod
    def _apply_api_version(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        api = data.get("pluginApi", data.get("plugin_api", 1))
        if not isinstance(api, int) or isinstance(api, bool) or api < 1:
            raise ValueError("plugin.json: pluginApi must be a whole number of 1 or more")
        if api > SUPPORTED_PLUGIN_API:
            raise ValueError(
                f"plugin.json: pluginApi {api} is newer than this Heym supports "
                f"({SUPPORTED_PLUGIN_API})"
            )
        if api == 1:
            return _strip_api2_keys(data)
        _check_known_keys(data, cls, "plugin.json")
        return data

    @field_validator("id")
    @classmethod
    def _validate_id(cls, v: str) -> str:
        if not _PLUGIN_ID_RE.match(v):
            raise ValueError("Plugin id must match ^[a-z0-9-]+$")
        return v

    @model_validator(mode="after")
    def _synthesize_and_validate_nodes(self) -> PluginManifest:
        if not self.nodes:
            if self.kind is None:
                raise ValueError("Plugin manifest must define `nodes` or a top-level `kind`")
            self.nodes = [
                PluginNodeDef(
                    key=self.id,
                    name=self.name,
                    kind=self.kind,
                    fields=self.fields,
                    dsl_hint=self.dsl_hint,
                    doc_slug=self.doc_slug or self.id,
                )
            ]
        keys = [n.key for n in self.nodes]
        if len(keys) != len(set(keys)):
            raise ValueError("Plugin node keys must be unique within a package")
        for node in self.nodes:
            if not node.doc_slug:
                node.doc_slug = self.id
        if self.plugin_api == 1:
            for node in self.nodes:
                for f in node.fields:
                    if f.type not in _API1_FIELD_TYPES:
                        raise ValueError(f"node {node.key}: field type {f.type} needs pluginApi 2")
        self._check_references()
        return self

    def _check_references(self) -> None:
        group_keys = {g.key for g in self.palette.groups} if self.palette else set()
        credential_keys = [c.key for c in self.credentials]
        if len(credential_keys) != len(set(credential_keys)):
            raise ValueError("credential type keys must be unique")
        for node in self.nodes:
            if node.group is not None and node.group not in group_keys:
                raise ValueError(f"node {node.key}: unknown group {node.group}")
            for f in walk_fields(node.fields):
                if f.credential_type is not None and f.credential_type not in credential_keys:
                    raise ValueError(f"node {node.key}: field {f.key}: unknown credential type")

    def resolved_nodes(self) -> list[PluginNodeDef]:
        return self.nodes

    def get_node(self, key: str) -> PluginNodeDef | None:
        return next((n for n in self.nodes if n.key == key), None)

    def get_credential_type(self, key: str) -> PluginCredentialTypeDef | None:
        return next((c for c in self.credentials if c.key == key), None)

    def package_kind(self) -> str:
        kinds = {n.kind for n in self.nodes}
        if len(kinds) == 1:
            return next(iter(kinds))
        return "mixed"

    def resolved_doc_slug(self) -> str:
        return self.doc_slug or self.id

    def node_color(self, node: PluginNodeDef) -> str | None:
        """A node's colour: its own, then its group's, then the package's."""
        if node.color:
            return node.color
        if self.palette is None:
            return None
        group = next((g for g in self.palette.groups if g.key == node.group), None)
        if group is not None and group.color:
            return group.color
        return self.palette.color


def walk_fields(fields: list[PluginField]) -> list[PluginField]:
    """Every field of a form, the columns of list fields included."""
    out: list[PluginField] = []
    for f in fields:
        out.append(f)
        out.extend(walk_fields(f.fields))
    return out


def _strip_api2_keys(data: dict) -> dict:
    """An API 1 manifest as stock Heym reads it: the API 2 keys are dropped."""

    def strip_field(field: Any) -> Any:
        if not isinstance(field, dict):
            return field
        return {k: v for k, v in field.items() if k not in _API2_FIELD_KEYS}

    def strip_node(node: Any) -> Any:
        if not isinstance(node, dict):
            return node
        out = {k: v for k, v in node.items() if k not in _API2_NODE_KEYS}
        if isinstance(out.get("fields"), list):
            out["fields"] = [strip_field(f) for f in out["fields"]]
        return out

    out = {k: v for k, v in data.items() if k not in _API2_PACKAGE_KEYS}
    if isinstance(out.get("nodes"), list):
        out["nodes"] = [strip_node(n) for n in out["nodes"]]
    if isinstance(out.get("fields"), list):
        out["fields"] = [strip_field(f) for f in out["fields"]]
    return out


def _known_keys(model: type[BaseModel]) -> set[str]:
    keys: set[str] = set()
    for name, info in model.model_fields.items():
        keys.add(name)
        if info.alias:
            keys.add(info.alias)
    return keys


_CHILD_LISTS: dict[str, str] = {
    "nodes": "PluginNodeDef",
    "fields": "PluginField",
    "credentials": "PluginCredentialTypeDef",
    "options": "PluginFieldOption",
    "exits": "PluginExit",
    "inputs": "PluginInput",
    "groups": "PluginPaletteGroup",
}
_CHILD_OBJECTS: dict[str, str] = {
    "palette": "PluginPalette",
    "exitsFrom": "PluginExitsFrom",
    "exits_from": "PluginExitsFrom",
}


def _check_known_keys(data: Any, model: type[BaseModel], where: str) -> None:
    """Refuse keys an API 2 manifest doesn't define, naming the place and the key, never a value."""
    if not isinstance(data, dict):
        return
    known = _known_keys(model)
    for key in data:
        if key not in known:
            raise ValueError(f'{where}: unknown key "{key}"')
    for key, value in data.items():
        if key in _CHILD_LISTS and isinstance(value, list):
            child = globals()[_CHILD_LISTS[key]]
            for index, item in enumerate(value):
                _check_known_keys(item, child, f"{where}: {key}[{index}]")
        elif key in _CHILD_OBJECTS:
            _check_known_keys(value, globals()[_CHILD_OBJECTS[key]], f"{where}: {key}")


class PluginNodeSummary(BaseModel):
    key: str
    name: str
    kind: PluginKind
    description: str
    fields: list[PluginField]
    dsl_hint: str = ""
    doc_slug: str = ""
    has_icon: bool = False
    # Plugin API 2 (absent for API 1 packages)
    group: str | None = None
    color: str | None = None
    subtitle: str | None = None
    exits: list[PluginExit] | None = None
    exits_from: PluginExitsFrom | None = Field(default=None, alias="exitsFrom")
    inputs: list[PluginInput] | None = None
    sample: dict[str, Any] | None = None
    source: str | None = None

    model_config = {"populate_by_name": True}


class PluginSummary(BaseModel):
    """Public listing shape returned by GET /api/plugins (one per package)."""

    id: str
    name: str
    version: str
    kind: str
    description: str
    enabled: bool
    nodes: list[PluginNodeSummary]
    has_icon: bool = False
    plugin_api: int = Field(default=1, alias="pluginApi")
    palette: PluginPalette | None = None
    credentials: list[PluginCredentialTypeDef] | None = None

    model_config = {"populate_by_name": True}


class PluginDoc(BaseModel):
    id: str
    name: str
    doc_slug: str
    markdown: str
