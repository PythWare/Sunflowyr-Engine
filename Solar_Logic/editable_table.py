from __future__ import annotations
import json, sqlite3
from pathlib import Path
from typing import Any
from .class_ids import class_name
from .editable_object_export import EDITABLE_ASSET_SCHEMAS
from .editable_object_repack import load_editable_asset
from .model_context import ModelContextIndex
from .mono_repack import taildata_value
from .particle_system_export import decorate_pointers
from .serialized import SerializedAsset, SerializedObject
from .unity_typetree_codec import load_schema, read_typetree, write_typetree
from .paths import ensure_parent

TABLE_ASSET_CLASS_IDS = {147, 150}
TABLE_FORMAT = "sunflowyr-editable-asset-sqlite"

class EditableTableError(ValueError):
    pass

def export_editable_asset_table(
    asset: SerializedAsset,
    obj: SerializedObject,
    context: ModelContextIndex,
    out_path: Path,
    metadata: dict[str, Any],
    export_name: str,
) -> dict[str, Any]:
    definition = EDITABLE_ASSET_SCHEMAS.get(obj.class_id)
    if definition is None or obj.class_id not in TABLE_ASSET_CLASS_IDS:
        raise EditableTableError(f"unsupported editable table class {obj.class_id}")
    with asset.path.open("rb") as handle:
        handle.seek(obj.byte_start)
        data = handle.read(obj.byte_size)
    if len(data) != obj.byte_size:
        raise EditableTableError("editable table object data is truncated")
    fields = read_typetree(data, load_schema(definition[1]), asset.endian)
    decorate_pointers(fields, asset, context)

    ensure_parent(out_path)
    if out_path.exists():
        out_path.unlink()
    connection = sqlite3.connect(out_path)
    try:
        connection.execute("CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        source = {
            "container": asset.relative_path,
            "pathId": obj.path_id,
            "classId": obj.class_id,
            "className": class_name(obj.class_id),
            "byteOffset": obj.byte_start,
            "byteSize": obj.byte_size,
        }
        metadata_rows = {
            "format": TABLE_FORMAT,
            "version": "1",
            "game": "rwby-ge",
            "unityVersion": asset.unity_version,
            "classId": str(obj.class_id),
            "className": class_name(obj.class_id),
            "name": str(fields.get("m_Name") or export_name),
            "unityName": str(fields.get("m_Name") or ""),
            "source": json.dumps(source, ensure_ascii=False, separators=(",", ":")),
        }
        connection.executemany(
            "INSERT INTO metadata(key, value) VALUES (?, ?)",
            metadata_rows.items(),
        )
        if obj.class_id == 147:
            write_resource_manager_tables(connection, fields)
        else:
            write_preload_tables(connection, fields)
        connection.commit()
    finally:
        connection.close()

    row_count = table_row_count(fields, obj.class_id)
    conversion = {
        "format": "editable_asset_sqlite_unity54",
        "kind": definition[0],
        "classId": obj.class_id,
        "className": class_name(obj.class_id),
        "name": str(fields.get("m_Name") or export_name),
        "rowCount": row_count,
        "sourceByteLength": len(data),
    }
    metadata["conversion"] = conversion
    metadata["writeback_note"] = (
        f"{class_name(obj.class_id)} uses SQLite because its ordered pointer tables are substantially "
        "more compact and easier to edit than deeply repeated JSON objects."
    )
    return conversion

def repack_editable_asset_table(
    database_path: Path,
    taildata: dict[str, Any],
    data_root: Path,
) -> bytes:
    class_id = int(taildata_value(taildata, "classId", 0) or 0)
    if class_id not in TABLE_ASSET_CLASS_IDS:
        raise EditableTableError(f"unsupported editable table class {class_id}")
    container = str(taildata_value(taildata, "container") or "")
    if not container:
        raise EditableTableError("taildata record doesnt name a Unity container")
    connection = sqlite3.connect(f"file:{Path(database_path).resolve().as_posix()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        metadata_rows = dict(connection.execute("SELECT key, value FROM metadata"))
        if metadata_rows.get("format") != TABLE_FORMAT:
            raise EditableTableError("SQLite file isnt a Sunflowyr editable asset table")
        stored_class = int(metadata_rows.get("classId", 0))
        if stored_class != class_id:
            raise EditableTableError(
                f"editable table class mismatch: SQLite has {stored_class}, taildata has {class_id}"
            )
        if class_id == 147:
            fields = read_resource_manager_tables(connection)
        else:
            fields = read_preload_tables(connection, metadata_rows.get("unityName", ""))
    except sqlite3.Error as exc:
        raise EditableTableError(f"could not read editable SQLite asset: {exc}") from exc
    finally:
        connection.close()
    asset = load_editable_asset(Path(data_root), container)
    schema_name = EDITABLE_ASSET_SCHEMAS[class_id][1]
    return write_typetree(fields, load_schema(schema_name), asset.endian)

def create_pointer_table(connection: sqlite3.Connection, table_name: str, owner_column: bool) -> None:
    owner = "owner_index INTEGER NOT NULL, " if owner_column else ""
    connection.execute(
        f"CREATE TABLE {table_name} ("
        f"{owner}row_index INTEGER NOT NULL, file_id INTEGER NOT NULL, path_id INTEGER NOT NULL, "
        "resolved_container TEXT, resolved_path_id INTEGER, class_id INTEGER, class_name TEXT, resolved_name TEXT)"
    )

def write_resource_manager_tables(connection: sqlite3.Connection, fields: dict[str, Any]) -> None:
    connection.execute(
        "CREATE TABLE container (row_index INTEGER PRIMARY KEY, asset_key TEXT NOT NULL, "
        "file_id INTEGER NOT NULL, path_id INTEGER NOT NULL, resolved_container TEXT, "
        "resolved_path_id INTEGER, class_id INTEGER, class_name TEXT, resolved_name TEXT)"
    )
    create_pointer_table(connection, "dependent_assets", False)
    create_pointer_table(connection, "dependencies", True)
    for row_index, pair in enumerate(fields.get("m_Container") or []):
        key = str(pair[0]) if isinstance(pair, list) and pair else ""
        pointer = pair[1] if isinstance(pair, list) and len(pair) > 1 else {}
        connection.execute(
            "INSERT INTO container VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (row_index, key, *pointer_values(pointer)),
        )
    for owner_index, item in enumerate(fields.get("m_DependentAssets") or []):
        record = item if isinstance(item, dict) else {}
        connection.execute(
            "INSERT INTO dependent_assets VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (owner_index, *pointer_values(record.get("m_Object"))),
        )
        for row_index, pointer in enumerate(record.get("m_Dependencies") or []):
            connection.execute(
                "INSERT INTO dependencies VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (owner_index, row_index, *pointer_values(pointer)),
            )

def write_preload_tables(connection: sqlite3.Connection, fields: dict[str, Any]) -> None:
    create_pointer_table(connection, "assets", False)
    connection.execute("CREATE TABLE dependencies (row_index INTEGER PRIMARY KEY, dependency TEXT NOT NULL)")
    for row_index, pointer in enumerate(fields.get("m_Assets") or []):
        connection.execute(
            "INSERT INTO assets VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (row_index, *pointer_values(pointer)),
        )
    for row_index, value in enumerate(fields.get("m_Dependencies") or []):
        connection.execute("INSERT INTO dependencies VALUES (?, ?)", (row_index, str(value)))

def read_resource_manager_tables(connection: sqlite3.Connection) -> dict[str, Any]:
    containers = [
        [row["asset_key"], pointer_from_row(row)]
        for row in connection.execute("SELECT * FROM container ORDER BY row_index")
    ]
    dependencies_by_owner: dict[int, list[dict[str, int]]] = {}
    for row in connection.execute("SELECT * FROM dependencies ORDER BY owner_index, row_index"):
        dependencies_by_owner.setdefault(int(row["owner_index"]), []).append(pointer_from_row(row))
    dependent_assets = []
    for row in connection.execute("SELECT * FROM dependent_assets ORDER BY row_index"):
        owner_index = int(row["row_index"])
        dependent_assets.append(
            {
                "m_Object": pointer_from_row(row),
                "m_Dependencies": dependencies_by_owner.get(owner_index, []),
            }
        )
    return {"m_Container": containers, "m_DependentAssets": dependent_assets}

def read_preload_tables(connection: sqlite3.Connection, name: str) -> dict[str, Any]:
    assets = [pointer_from_row(row) for row in connection.execute("SELECT * FROM assets ORDER BY row_index")]
    dependencies = [
        str(row["dependency"])
        for row in connection.execute("SELECT * FROM dependencies ORDER BY row_index")
    ]
    return {"m_Name": name, "m_Assets": assets, "m_Dependencies": dependencies}

def pointer_values(value: Any) -> tuple[Any, ...]:
    pointer = value if isinstance(value, dict) else {}
    return (
        int(pointer.get("m_FileID", pointer.get("fileId", 0)) or 0),
        int(pointer.get("m_PathID", pointer.get("pathId", 0)) or 0),
        pointer.get("container"),
        pointer.get("resolvedPathId"),
        pointer.get("classId"),
        pointer.get("className"),
        pointer.get("name"),
    )

def pointer_from_row(row: sqlite3.Row) -> dict[str, int]:
    return {"m_FileID": int(row["file_id"]), "m_PathID": int(row["path_id"])}

def table_row_count(fields: dict[str, Any], class_id: int) -> int:
    if class_id == 147:
        dependencies = sum(
            len(item.get("m_Dependencies") or [])
            for item in fields.get("m_DependentAssets") or []
            if isinstance(item, dict)
        )
        return len(fields.get("m_Container") or []) + len(fields.get("m_DependentAssets") or []) + dependencies
    return len(fields.get("m_Assets") or []) + len(fields.get("m_Dependencies") or [])
