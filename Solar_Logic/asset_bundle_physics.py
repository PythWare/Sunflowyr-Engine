from __future__ import annotations
import json, math, struct
from pathlib import Path
from typing import Any
from .asset_bundle_writer import pack_aligned_string

PHYSIC_MATERIAL_CLASS_ID = 134
COMBINE_NAMES = {
    0: "Average",
    1: "Minimum",
    2: "Multiply",
    3: "Maximum",
}

class NewPhysicMaterialAssetError(ValueError):
    pass

def try_build_physic_material_asset(
    json_path: Path,
    fallback_name: str,
) -> tuple[bytes, str, dict[str, object]] | None:
    try:
        document = json.loads(Path(json_path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        if json_path.name.lower().endswith(".physicmaterial.json"):
            raise NewPhysicMaterialAssetError(f"couldnt read PhysicMaterial JSON: {exc}") from exc
        return None
    if not isinstance(document, dict):
        if json_path.name.lower().endswith(".physicmaterial.json"):
            raise NewPhysicMaterialAssetError("PhysicMaterial JSON root must be an object")
        return None
    if not is_physic_material_document(json_path, document):
        return None
    fields = document.get("fields") if isinstance(document.get("fields"), dict) else document
    name = clean_name(fields.get("m_Name", fields.get("name", fallback_name)), fallback_name)
    dynamic_friction = unit_float(fields.get("dynamicFriction", 0.6), "dynamicFriction")
    static_friction = unit_float(fields.get("staticFriction", 0.6), "staticFriction")
    bounciness = unit_float(fields.get("bounciness", 0.0), "bounciness")
    friction_combine = combine_value(fields.get("frictionCombine", 0), "frictionCombine")
    bounce_combine = combine_value(fields.get("bounceCombine", 0), "bounceCombine")
    payload = build_physic_material_object(
        name,
        dynamic_friction,
        static_friction,
        bounciness,
        friction_combine,
        bounce_combine,
    )
    return payload, name, {
        "sourceFormat": "physicmaterial-json",
        "dynamicFriction": dynamic_friction,
        "staticFriction": static_friction,
        "bounciness": bounciness,
        "frictionCombine": friction_combine,
        "frictionCombineName": COMBINE_NAMES[friction_combine],
        "bounceCombine": bounce_combine,
        "bounceCombineName": COMBINE_NAMES[bounce_combine],
    }

def is_physic_material_document(json_path: Path, document: dict[str, Any]) -> bool:
    if json_path.name.lower().endswith(".physicmaterial.json"):
        return True
    format_name = str(document.get("format", "")).casefold()
    if format_name in {"sunflowyr-physic-material", "sunflowyr-new-physic-material"}:
        return True
    if format_name != "sunflowyr-map-component":
        return False
    source = document.get("source")
    if not isinstance(source, dict):
        return False
    try:
        return int(source.get("classId", 0) or 0) == PHYSIC_MATERIAL_CLASS_ID
    except (TypeError, ValueError):
        return False

def build_physic_material_object(
    name: str,
    dynamic_friction: float,
    static_friction: float,
    bounciness: float,
    friction_combine: int,
    bounce_combine: int,
) -> bytes:
    output = bytearray(pack_aligned_string(name))
    output.extend(
        struct.pack(
            "<fffii",
            dynamic_friction,
            static_friction,
            bounciness,
            friction_combine,
            bounce_combine,
        )
    )
    return bytes(output)

def unit_float(value: Any, label: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise NewPhysicMaterialAssetError(f"{label} must be a number from 0 to 1") from exc
    if not math.isfinite(number) or number < 0.0 or number > 1.0:
        raise NewPhysicMaterialAssetError(f"{label} must be a finite number from 0 to 1")
    return number

def combine_value(value: Any, label: str) -> int:
    if isinstance(value, str):
        lookup = {name.casefold(): index for index, name in COMBINE_NAMES.items()}
        key = value.strip().casefold()
        if key in lookup:
            return lookup[key]
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise NewPhysicMaterialAssetError(f"{label} must be Average, Minimum, Multiply, Maximum, or 0-3") from exc
    if number not in COMBINE_NAMES:
        raise NewPhysicMaterialAssetError(f"{label} must be Average, Minimum, Multiply, Maximum, or 0-3")
    return number

def clean_name(value: Any, fallback: str) -> str:
    name = str(value or fallback).strip()
    if not name:
        raise NewPhysicMaterialAssetError("PhysicMaterial name cant be empty")
    if len(name.encode("utf-8")) > 4096:
        raise NewPhysicMaterialAssetError("PhysicMaterial name is too long")
    return name
