from __future__ import annotations
import json, struct
from pathlib import Path
from typing import Any
from .asset_bundle_writer import align_buffer, pack_aligned_string

PLAN_FORMAT = "sunflowyr-bundle-plan"
DATABASE_TARGETS = {
    "PlayableCharacterDefinition": ("PlayableCharacterDatabase", "m_data"),
    "PlayableTeamDefinition": ("PlayableTeamDatabase", "m_data"),
}

def build_bundle_plan(
    bundle_name: str,
    manifest_assets: list[dict[str, Any]],
) -> dict[str, Any]:
    assets: list[dict[str, Any]] = []
    registrations: list[dict[str, Any]] = []
    for item in manifest_assets:
        details = item.get("import") or {}
        entry = {
            "assetPath": item.get("assetPath"),
            "name": item.get("name"),
            "className": item.get("className"),
            "bundlePathId": item.get("bundlePathId"),
        }
        script = details.get("script")
        if isinstance(script, dict) and script.get("class"):
            entry["script"] = script
            target = DATABASE_TARGETS.get(str(script.get("class")))
            if target is not None:
                database, member = target
                registrations.append(
                    {
                        "action": "appendToDatabase",
                        "assetPath": item.get("assetPath"),
                        "loadAs": "ScriptableObject",
                        "database": database,
                        "member": member,
                    }
                )
        if details.get("rootGameObject"):
            entry["rootGameObject"] = details.get("rootGameObject")
            entry["objectCount"] = details.get("objectCount")
        references = details.get("references")
        if isinstance(references, list) and references:
            entry["references"] = references
        assets.append(entry)

    return {
        "format": PLAN_FORMAT,
        "version": 1,
        "game": "rwby-ge",
        "tool": "Sunflowyr Engine",
        "mode": "additive",
        "bundleName": bundle_name,
        "pluginNote": (
            "Additive bundle: nothing here replaces an existing game asset. "
            "Load the bundle, LoadAsset each assetPath, then apply 'registrations'. "
            "appendToDatabase: locate the named database instance already loaded by the game "
            "(i.e., via Resources.FindObjectsOfTypeAll) and append the loaded asset to its list "
            "member. Prefabs are LoadAsset<GameObject> and Instantiate, MonoBehaviour "
            "definitions are ScriptableObject-style assets whose managed fields Sunflowyr "
            "already laid out with the game's own assemblies."
        ),
        "assets": assets,
        "registrations": registrations,
        "unresolvedNote": (
            "Fields that must point at existing game objects (i.e., a definition whose Prefab "
            "should be the game's own Ruby prefab) cant be referenced from an additive "
            "bundle, leave them null here and assign them from the plugin at runtime."
        ),
    }
