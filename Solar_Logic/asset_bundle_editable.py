from __future__ import annotations
import json
from pathlib import Path
from typing import Any
from .class_ids import class_name
from .editable_object_export import EDITABLE_ASSET_SCHEMAS
from .unity_typetree_codec import load_schema, write_typetree

EDITABLE_FORMAT_MARKER = "sunflowyr-editable-asset"

SUB_ASSET_CLASSES = {
    243,
    245,
}

class NewEditableAssetError(ValueError):
    pass

def try_read_editable_document(json_path: Path) -> dict[str, Any] | None:
    forced = json_path.name.lower().endswith(".editable.json")
    try:
        document = json.loads(Path(json_path).read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        if forced:
            raise NewEditableAssetError(f"couldnt read editable asset JSON: {exc}") from exc
        return None
    if not isinstance(document, dict):
        if forced:
            raise NewEditableAssetError("editable asset JSON root must be an object")
        return None
    if document.get("format") != EDITABLE_FORMAT_MARKER:
        if forced:
            raise NewEditableAssetError(
                f'editable asset JSON must set "format": "{EDITABLE_FORMAT_MARKER}"'
            )
        return None
    return document

def resolve_schema(class_id: int, override: str = "") -> str:
    if override:
        return override
    definition = EDITABLE_ASSET_SCHEMAS.get(int(class_id))
    if definition is None:
        raise NewEditableAssetError(
            f"class {class_id} ({class_name(int(class_id))}) has no editable schema"
        )
    return definition[1]

def build_editable_asset(
    document: dict[str, Any],
    fallback_name: str,
    sentinels: Any,
    bundle_plans: dict[str, Any] | None = None,
) -> Any:
    from .asset_bundle_prefab import BuiltObject, PrefabBuild, prepare_reference_values

    source = document.get("source")
    raw_class = document.get("classId")
    if raw_class is None and isinstance(source, dict):
        raw_class = source.get("classId")
    try:
        class_id = int(raw_class)
    except (TypeError, ValueError) as exc:
        raise NewEditableAssetError('editable asset JSON needs an integer "classId"') from exc

    fields = document.get("fields")
    if not isinstance(fields, dict):
        raise NewEditableAssetError('editable asset JSON needs a "fields" object')

    schema_name = resolve_schema(class_id, str(document.get("schema") or ""))
    external_refs: dict[int, str] = {}
    prepared = prepare_reference_values(fields, sentinels, external_refs, None, bundle_plans)

    try:
        payload = write_typetree(prepared, load_schema(schema_name), "<")
    except Exception as exc:
        raise NewEditableAssetError(
            f"could not write {class_name(class_id)} with {schema_name}: {exc}"
        ) from exc

    name = str(document.get("name") or prepared.get("m_Name") or fallback_name)
    built = BuiltObject(
        class_id=class_id,
        class_name=class_name(class_id),
        name=name,
        payload=payload,
        local_id=sentinels.take(),
        container_listed=class_id not in SUB_ASSET_CLASSES,
    )
    details = {
        "sourceFormat": EDITABLE_FORMAT_MARKER,
        "classId": class_id,
        "schema": schema_name,
        "payloadBytes": len(payload),
        "references": len(external_refs),
    }
    return PrefabBuild(
        objects=[built],
        root_name=name,
        details=details,
        external_refs=external_refs,
    )
