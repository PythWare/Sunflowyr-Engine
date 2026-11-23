from __future__ import annotations
import base64, json, math, struct
from pathlib import Path
from typing import Any, Callable
from .mono_repack import EndianWriter, taildata_value, write_pptr
from .serialized import SerializedAsset, load_serialized_asset

class AnimationClipRepackError(ValueError):
    pass

asset_cache: dict[tuple[str, str], SerializedAsset] = {}

def repack_animation_clip_json(
    json_path: Path,
    taildata: dict[str, Any],
    data_root: Path,
) -> bytes:
    document = json.loads(Path(json_path).read_text(encoding="utf-8-sig"))
    if not isinstance(document, dict):
        raise AnimationClipRepackError("AnimationClip JSON root must be an object")
    class_id = int(taildata_value(taildata, "classId", 0) or 0)
    if class_id != 74:
        raise AnimationClipRepackError(
            f"AnimationClip taildata expected class 74, found {class_id}"
        )
    container = str(taildata_value(taildata, "container") or "")
    if not container:
        raise AnimationClipRepackError("taildata record doesnt name a Unity container")
    asset = load_animation_asset(Path(data_root), container)
    return AnimationClipWriter(asset).write(document)

def load_animation_asset(data_root: Path, container: str) -> SerializedAsset:
    root = data_root.resolve()
    key = (str(root).lower(), normalize_container(container))
    asset = asset_cache.get(key)
    if asset is None:
        asset_path = (root / container).resolve()
        try:
            asset_path.relative_to(root)
        except ValueError as exc:
            raise AnimationClipRepackError(
                f"AnimationClip container escapes the game data folder: {container}"
            ) from exc
        if not asset_path.is_file():
            raise AnimationClipRepackError(
                f"AnimationClip container wasnt found: {asset_path}"
            )
        asset = load_serialized_asset(asset_path, root)
        asset_cache[key] = asset
    return asset

def normalize_container(value: str) -> str:
    return value.replace("\\", "/").lower()

class AnimationClipWriter:
    def __init__(self, asset: SerializedAsset) -> None:
        self.asset = asset
        self.writer = EndianWriter(asset.endian)
        self.version = unity_version_tuple(asset.unity_version)

    def write(self, document: dict[str, Any]) -> bytes:
        self.write_string(document.get("m_Name", ""))
        if self.version[0] >= 5:
            self.writer.bool(document.get("m_Legacy", False))
        elif self.version[0] >= 4:
            self.writer.i32(int(document.get("m_AnimationType", 0)))
        self.writer.bool(document.get("m_Compressed", False))
        if self.version_at_least(4, 3):
            self.writer.bool(document.get("m_UseHighQualityCurve", False))
        self.writer.align()

        self.write_array(
            document.get("m_RotationCurves"),
            self.write_quaternion_curve,
            "m_RotationCurves",
        )
        self.write_array(
            document.get("m_CompressedRotationCurves"),
            self.write_compressed_animation_curve,
            "m_CompressedRotationCurves",
        )
        if self.version_at_least(5, 3):
            self.write_array(
                document.get("m_EulerCurves"),
                self.write_vector3_curve,
                "m_EulerCurves",
            )
        self.write_array(
            document.get("m_PositionCurves"),
            self.write_vector3_curve,
            "m_PositionCurves",
        )
        self.write_array(
            document.get("m_ScaleCurves"),
            self.write_vector3_curve,
            "m_ScaleCurves",
        )
        self.write_array(
            document.get("m_FloatCurves"),
            self.write_float_curve,
            "m_FloatCurves",
        )
        if self.version_at_least(4, 3):
            self.write_array(
                document.get("m_PPtrCurves"),
                self.write_pptr_curve,
                "m_PPtrCurves",
            )

        self.write_float(document.get("m_SampleRate", 0.0))
        self.writer.i32(int(document.get("m_WrapMode", 0)))
        if self.version_at_least(3, 4):
            self.write_aabb(require_object(document.get("m_Bounds"), "m_Bounds"))
        if self.version[0] >= 4:
            self.writer.u32(int(document.get("m_MuscleClipSize", 0)))
            self.write_clip_muscle_constant(
                require_object(document.get("m_MuscleClip"), "m_MuscleClip")
            )
        if self.version_at_least(4, 3):
            self.write_animation_clip_binding_constant(
                require_object(
                    document.get("m_ClipBindingConstant"),
                    "m_ClipBindingConstant",
                )
            )
        if self.version_at_least(2018, 3):
            self.writer.bool(document.get("m_HasGenericRootTransform", False))
            self.writer.bool(document.get("m_HasMotionFloatCurves", False))
            self.writer.align()
        self.write_array(
            document.get("m_Events"),
            self.write_animation_event,
            "m_Events",
        )
        if self.version[0] >= 2017:
            self.writer.align()

        tail = document.get("solarUnparsedTail")
        if isinstance(tail, dict):
            self.writer.write(decode_base64_blob(tail, "solarUnparsedTail"))
        return self.writer.bytes()

    def write_quaternion_curve(self, value: Any) -> None:
        curve = require_object(value, "quaternion curve")
        self.write_animation_curve(
            require_object(curve.get("curve"), "quaternion curve.curve"),
            "quaternion",
            self.write_quaternion,
        )
        self.write_string(curve.get("path", ""))

    def write_vector3_curve(self, value: Any) -> None:
        curve = require_object(value, "vector3 curve")
        self.write_animation_curve(
            require_object(curve.get("curve"), "vector3 curve.curve"),
            "vector3",
            self.write_vector3,
        )
        self.write_string(curve.get("path", ""))

    def write_float_curve(self, value: Any) -> None:
        curve = require_object(value, "float curve")
        self.write_animation_curve(
            require_object(curve.get("curve"), "float curve.curve"),
            "float",
            self.write_float,
        )
        self.write_string(curve.get("attribute", ""))
        self.write_string(curve.get("path", ""))
        self.writer.i32(int(curve.get("classID", 0)))
        self.write_pointer(curve.get("script"))

    def write_pptr_curve(self, value: Any) -> None:
        curve = require_object(value, "PPtr curve")
        self.write_array(
            curve.get("curve"),
            self.write_pptr_keyframe,
            "PPtrCurve.curve",
        )
        self.write_string(curve.get("attribute", ""))
        self.write_string(curve.get("path", ""))
        self.writer.i32(int(curve.get("classID", 0)))
        self.write_pointer(curve.get("script"))

    def write_pptr_keyframe(self, value: Any) -> None:
        key = require_object(value, "PPtr keyframe")
        self.write_float(key.get("time", 0.0))
        self.write_pointer(key.get("value"))

    def write_animation_curve(
        self,
        curve: dict[str, Any],
        value_kind: str,
        value_writer: Callable[[Any], None],
    ) -> None:
        keys = curve.get("m_Curve")
        if isinstance(keys, dict):
            count, data = decode_array_blob(
                keys,
                f"AnimationCurve<{value_kind}>.m_Curve",
            )
            expected_stride = keyframe_stride(value_kind, self.version[0] >= 2018)
            stride = int(keys.get("stride", expected_stride))
            if stride != expected_stride or len(data) != count * expected_stride:
                raise AnimationClipRepackError(
                    f"AnimationCurve<{value_kind}> blob expected {count * expected_stride} "
                    f"bytes at stride {expected_stride}, found {len(data)} bytes at stride {stride}"
                )
            self.writer.i32(count)
            self.writer.write(data)
        else:
            key_list = require_list(keys, f"AnimationCurve<{value_kind}>.m_Curve")
            self.writer.i32(len(key_list))
            for key in key_list:
                self.write_keyframe(key, value_writer)
        self.writer.i32(int(curve.get("m_PreInfinity", 0)))
        self.writer.i32(int(curve.get("m_PostInfinity", 0)))
        if self.version_at_least(5, 3):
            self.writer.i32(int(curve.get("m_RotationOrder", 0)))

    def write_keyframe(
        self,
        value: Any,
        value_writer: Callable[[Any], None],
    ) -> None:
        key = require_object(value, "animation keyframe")
        self.write_float(key.get("time", 0.0))
        value_writer(key.get("value"))
        value_writer(key.get("inSlope"))
        value_writer(key.get("outSlope"))
        if self.version[0] >= 2018:
            self.writer.i32(int(key.get("weightedMode", 0)))
            value_writer(key.get("inWeight"))
            value_writer(key.get("outWeight"))

    def write_compressed_animation_curve(self, value: Any) -> None:
        curve = require_object(value, "compressed animation curve")
        self.write_string(curve.get("m_Path", ""))
        self.write_packed_int_vector(
            require_object(curve.get("m_Times"), "compressed curve m_Times")
        )
        self.write_packed_quat_vector(
            require_object(curve.get("m_Values"), "compressed curve m_Values")
        )
        self.write_packed_float_vector(
            require_object(curve.get("m_Slopes"), "compressed curve m_Slopes")
        )
        self.writer.i32(int(curve.get("m_PreInfinity", 0)))
        self.writer.i32(int(curve.get("m_PostInfinity", 0)))

    def write_packed_int_vector(self, value: dict[str, Any]) -> None:
        self.writer.u32(int(value.get("m_NumItems", 0)))
        self.write_byte_array(value.get("m_Data"), "PackedIntVector.m_Data")
        self.writer.u8(int(value.get("m_BitSize", 0)))
        self.writer.align()

    def write_packed_quat_vector(self, value: dict[str, Any]) -> None:
        self.writer.u32(int(value.get("m_NumItems", 0)))
        self.write_byte_array(value.get("m_Data"), "PackedQuatVector.m_Data")
        self.writer.align()

    def write_packed_float_vector(self, value: dict[str, Any]) -> None:
        self.writer.u32(int(value.get("m_NumItems", 0)))
        self.write_float(value.get("m_Range", 0.0))
        self.write_float(value.get("m_Start", 0.0))
        self.write_byte_array(value.get("m_Data"), "PackedFloatVector.m_Data")
        self.writer.align()
        self.writer.u8(int(value.get("m_BitSize", 0)))
        self.writer.align()

    def write_aabb(self, value: dict[str, Any]) -> None:
        self.write_vector3(value.get("m_Center"))
        self.write_vector3(value.get("m_Extent"))

    def write_xform(self, value: Any) -> None:
        transform = require_object(value, "xform")
        if self.version_at_least(5, 4):
            self.write_vector3(transform.get("t"))
        else:
            self.write_vector4(transform.get("t"))
        self.write_quaternion(transform.get("q"))
        if self.version_at_least(5, 4):
            self.write_vector3(transform.get("s"))
        else:
            self.write_vector4(transform.get("s"))

    def write_hand_pose(self, value: Any) -> None:
        pose = require_object(value, "hand pose")
        self.write_xform(pose.get("m_GrabX"))
        self.write_float_array(pose.get("m_DoFArray"), "HandPose.m_DoFArray")
        self.write_float(pose.get("m_Override", 0.0))
        self.write_float(pose.get("m_CloseOpen", 0.0))
        self.write_float(pose.get("m_InOut", 0.0))
        self.write_float(pose.get("m_Grab", 0.0))

    def write_human_goal(self, value: Any) -> None:
        goal = require_object(value, "human goal")
        self.write_xform(goal.get("m_X"))
        self.write_float(goal.get("m_WeightT", 0.0))
        self.write_float(goal.get("m_WeightR", 0.0))
        if self.version[0] >= 5:
            if self.version_at_least(5, 4):
                self.write_vector3(goal.get("m_HintT"))
            else:
                self.write_vector4(goal.get("m_HintT"))
            self.write_float(goal.get("m_HintWeightT", 0.0))

    def write_human_pose(self, value: Any) -> None:
        pose = require_object(value, "human pose")
        self.write_xform(pose.get("m_RootX"))
        if self.version_at_least(5, 4):
            self.write_vector3(pose.get("m_LookAtPosition"))
        else:
            self.write_vector4(pose.get("m_LookAtPosition"))
        self.write_vector4(pose.get("m_LookAtWeight"))
        self.write_array(
            pose.get("m_GoalArray"),
            self.write_human_goal,
            "HumanPose.m_GoalArray",
        )
        self.write_hand_pose(pose.get("m_LeftHandPose"))
        self.write_hand_pose(pose.get("m_RightHandPose"))
        self.write_float_array(pose.get("m_DoFArray"), "HumanPose.m_DoFArray")
        if self.version_at_least(5, 2):
            self.write_vector3_array(
                pose.get("m_TDoFArray"),
                "HumanPose.m_TDoFArray",
            )

    def write_streamed_clip(self, value: Any) -> None:
        clip = require_object(value, "streamed clip")
        self.write_u32_array(clip.get("data"), "StreamedClip.data")
        self.writer.u32(int(clip.get("curveCount", 0)))

    def write_dense_clip(self, value: Any) -> None:
        clip = require_object(value, "dense clip")
        self.writer.i32(int(clip.get("m_FrameCount", 0)))
        self.writer.u32(int(clip.get("m_CurveCount", 0)))
        self.write_float(clip.get("m_SampleRate", 0.0))
        self.write_float(clip.get("m_BeginTime", 0.0))
        self.write_float_array(clip.get("m_SampleArray"), "DenseClip.m_SampleArray")

    def write_constant_clip(self, value: Any) -> None:
        clip = require_object(value, "constant clip")
        self.write_float_array(clip.get("data"), "ConstantClip.data")

    def write_value_constant(self, value: Any) -> None:
        item = require_object(value, "value constant")
        self.writer.u32(int(item.get("m_ID", 0)))
        if self.version[0] < 5 or (self.version[0] == 5 and self.version[1] < 5):
            self.writer.u32(int(item.get("m_TypeID", 0)))
        self.writer.u32(int(item.get("m_Type", 0)))
        self.writer.u32(int(item.get("m_Index", 0)))

    def write_value_array_constant(self, value: Any) -> None:
        constant = require_object(value, "value array constant")
        self.write_value_constant_array(
            constant.get("m_ValueArray"),
            "ValueArrayConstant.m_ValueArray",
        )

    def write_clip(self, value: Any) -> None:
        clip = require_object(value, "clip")
        self.write_streamed_clip(clip.get("m_StreamedClip"))
        self.write_dense_clip(clip.get("m_DenseClip"))
        if self.version_at_least(4, 3):
            self.write_constant_clip(clip.get("m_ConstantClip"))
        if self.version[0] < 2018 or (
            self.version[0] == 2018 and self.version[1] < 3
        ):
            self.write_value_array_constant(clip.get("m_Binding"))

    def write_clip_muscle_constant(self, value: Any) -> None:
        muscle = require_object(value, "clip muscle constant")
        self.write_human_pose(muscle.get("m_DeltaPose"))
        self.write_xform(muscle.get("m_StartX"))
        if self.version_at_least(5, 5):
            self.write_xform(muscle.get("m_StopX"))
        self.write_xform(muscle.get("m_LeftFootStartX"))
        self.write_xform(muscle.get("m_RightFootStartX"))
        if self.version[0] < 5:
            self.write_xform(muscle.get("m_MotionStartX"))
            self.write_xform(muscle.get("m_MotionStopX"))
        if self.version_at_least(5, 4):
            self.write_vector3(muscle.get("m_AverageSpeed"))
        else:
            self.write_vector4(muscle.get("m_AverageSpeed"))
        self.write_clip(muscle.get("m_Clip"))
        for name in (
            "m_StartTime",
            "m_StopTime",
            "m_OrientationOffsetY",
            "m_Level",
            "m_CycleOffset",
            "m_AverageAngularSpeed",
        ):
            self.write_float(muscle.get(name, 0.0))
        self.write_i32_array(
            muscle.get("m_IndexArray"),
            "ClipMuscleConstant.m_IndexArray",
        )
        if self.version[0] < 4 or (
            self.version[0] == 4 and self.version[1] < 3
        ):
            self.write_i32_array(
                muscle.get("m_AdditionalCurveIndexArray"),
                "ClipMuscleConstant.m_AdditionalCurveIndexArray",
            )
        self.write_value_delta_array(
            muscle.get("m_ValueArrayDelta"),
            "ClipMuscleConstant.m_ValueArrayDelta",
        )
        if self.version_at_least(5, 3):
            self.write_float_array(
                muscle.get("m_ValueArrayReferencePose"),
                "ClipMuscleConstant.m_ValueArrayReferencePose",
            )
        self.writer.bool(muscle.get("m_Mirror", False))
        if self.version_at_least(4, 3):
            self.writer.bool(muscle.get("m_LoopTime", False))
        self.writer.bool(muscle.get("m_LoopBlend", False))
        self.writer.bool(muscle.get("m_LoopBlendOrientation", False))
        self.writer.bool(muscle.get("m_LoopBlendPositionY", False))
        self.writer.bool(muscle.get("m_LoopBlendPositionXZ", False))
        if self.version_at_least(5, 5):
            self.writer.bool(muscle.get("m_StartAtOrigin", False))
        self.writer.bool(muscle.get("m_KeepOriginalOrientation", False))
        self.writer.bool(muscle.get("m_KeepOriginalPositionY", False))
        self.writer.bool(muscle.get("m_KeepOriginalPositionXZ", False))
        self.writer.bool(muscle.get("m_HeightFromFeet", False))
        self.writer.align()

    def write_generic_binding(self, value: Any) -> None:
        binding = require_object(value, "generic binding")
        self.writer.u32(int(binding.get("path", 0)))
        self.writer.u32(int(binding.get("attribute", 0)))
        self.write_pointer(binding.get("script"))
        if self.version_at_least(5, 6):
            self.writer.i32(int(binding.get("typeID", 0)))
        else:
            self.writer.u16(int(binding.get("typeID", 0)))
        self.writer.u8(int(binding.get("customType", 0)))
        self.writer.u8(int(binding.get("isPPtrCurve", 0)))
        if self.version_at_least(2022, 1):
            self.writer.u8(int(binding.get("isIntCurve", 0)))
        self.writer.align()

    def write_animation_clip_binding_constant(self, value: Any) -> None:
        binding = require_object(value, "animation clip binding constant")
        self.write_array(
            binding.get("genericBindings"),
            self.write_generic_binding,
            "AnimationClipBindingConstant.genericBindings",
        )
        self.write_array(
            binding.get("pptrCurveMapping"),
            self.write_pointer,
            "AnimationClipBindingConstant.pptrCurveMapping",
        )

    def write_animation_event(self, value: Any) -> None:
        event = require_object(value, "animation event")
        self.write_float(event.get("time", 0.0))
        self.write_string(event.get("functionName", ""))
        self.write_string(event.get("data", ""))
        self.write_pointer(event.get("objectReferenceParameter"))
        self.write_float(event.get("floatParameter", 0.0))
        if self.version[0] >= 3:
            self.writer.i32(int(event.get("intParameter", 0)))
        self.writer.i32(int(event.get("messageOptions", 0)))

    def write_pointer(self, value: Any) -> None:
        write_pptr(
            self.writer,
            self.asset,
            value if isinstance(value, dict) else {},
        )

    def write_array(
        self,
        value: Any,
        item_writer: Callable[[Any], None],
        label: str,
    ) -> None:
        items = require_list(value, label)
        self.writer.i32(len(items))
        for item in items:
            item_writer(item)

    def write_float_array(self, value: Any, label: str) -> None:
        if isinstance(value, dict):
            count, data = decode_typed_blob(value, label, "float32", 4)
            self.writer.i32(count)
            self.writer.write(data)
            return
        items = require_list(value, label)
        self.writer.i32(len(items))
        for item in items:
            self.write_float(item)

    def write_i32_array(self, value: Any, label: str) -> None:
        if isinstance(value, dict):
            count, data = decode_typed_blob(value, label, "int32", 4)
            self.writer.i32(count)
            self.writer.write(data)
            return
        items = require_list(value, label)
        self.writer.i32(len(items))
        for item in items:
            self.writer.i32(int(item))

    def write_u32_array(self, value: Any, label: str) -> None:
        if isinstance(value, dict):
            count, data = decode_typed_blob(value, label, "uint32", 4)
            self.writer.i32(count)
            self.writer.write(data)
            return
        items = require_list(value, label)
        self.writer.i32(len(items))
        for item in items:
            self.writer.u32(int(item))

    def write_vector3_array(self, value: Any, label: str) -> None:
        if isinstance(value, dict):
            count, data = decode_typed_blob(
                value,
                label,
                "vector3_float32",
                12,
            )
            self.writer.i32(count)
            self.writer.write(data)
            return
        items = require_list(value, label)
        self.writer.i32(len(items))
        for item in items:
            self.write_vector3(item)

    def write_value_delta_array(self, value: Any, label: str) -> None:
        if isinstance(value, dict):
            count, data = decode_typed_blob(value, label, "value_delta", 8)
            self.writer.i32(count)
            self.writer.write(data)
            return
        items = require_list(value, label)
        self.writer.i32(len(items))
        for item in items:
            delta = require_object(item, "value delta")
            self.write_float(delta.get("m_Start", 0.0))
            self.write_float(delta.get("m_Stop", 0.0))

    def write_value_constant_array(self, value: Any, label: str) -> None:
        stride = (
            16
            if self.version[0] < 5
            or (self.version[0] == 5 and self.version[1] < 5)
            else 12
        )
        if isinstance(value, dict):
            count, data = decode_typed_blob(
                value,
                label,
                "value_constant",
                stride,
            )
            self.writer.i32(count)
            self.writer.write(data)
            return
        items = require_list(value, label)
        self.writer.i32(len(items))
        for item in items:
            self.write_value_constant(item)

    def write_byte_array(self, value: Any, label: str) -> None:
        data = decode_base64_blob(require_object(value, label), label)
        self.writer.i32(len(data))
        self.writer.write(data)
        self.writer.align()

    def write_string(self, value: Any) -> None:
        self.writer.aligned_string(value)

    def write_float(self, value: Any) -> None:
        self.writer.f32(float_value(value))

    def write_vector3(self, value: Any) -> None:
        vector = require_object(value, "Vector3")
        self.write_float(vector.get("x", 0.0))
        self.write_float(vector.get("y", 0.0))
        self.write_float(vector.get("z", 0.0))

    def write_vector4(self, value: Any) -> None:
        vector = require_object(value, "Vector4")
        self.write_float(vector.get("x", 0.0))
        self.write_float(vector.get("y", 0.0))
        self.write_float(vector.get("z", 0.0))
        self.write_float(vector.get("w", 0.0))

    def write_quaternion(self, value: Any) -> None:
        self.write_vector4(value)

    def version_at_least(self, major: int, minor: int = 0) -> bool:
        return self.version[0] > major or (
            self.version[0] == major and self.version[1] >= minor
        )

def decode_array_blob(value: dict[str, Any], label: str) -> tuple[int, bytes]:
    count = int(value.get("count", -1))
    if count < 0:
        raise AnimationClipRepackError(f"{label} blob is missing a valid count")
    data = decode_base64_blob(value, label)
    return count, data

def decode_typed_blob(
    value: dict[str, Any],
    label: str,
    expected_type: str,
    expected_stride: int,
) -> tuple[int, bytes]:
    count, data = decode_array_blob(value, label)
    item_type = str(value.get("item_type") or "")
    stride = int(value.get("stride", expected_stride))
    if item_type and item_type != expected_type:
        raise AnimationClipRepackError(
            f"{label} expected item_type {expected_type}, found {item_type}"
        )
    if stride != expected_stride or len(data) != count * expected_stride:
        raise AnimationClipRepackError(
            f"{label} expected {count * expected_stride} bytes at stride "
            f"{expected_stride}, found {len(data)} bytes at stride {stride}"
        )
    return count, data

def decode_base64_blob(value: dict[str, Any], label: str) -> bytes:
    if value.get("encoding") != "base64":
        raise AnimationClipRepackError(f"{label} must use base64 encoding")
    text = value.get("data")
    if not isinstance(text, str):
        raise AnimationClipRepackError(f"{label}.data must be base64 text")
    try:
        data = base64.b64decode(text, validate=True)
    except Exception as exc:
        raise AnimationClipRepackError(f"{label}.data is invalid base64") from exc
    declared_size = value.get("size")
    if declared_size is not None and int(declared_size) != len(data):
        raise AnimationClipRepackError(
            f"{label} declares {declared_size} bytes but contains {len(data)}"
        )
    return data

def require_object(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise AnimationClipRepackError(f"{label} must be an object")
    return value

def require_list(value: Any, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise AnimationClipRepackError(f"{label} must be a list")
    return value

def float_value(value: Any) -> float:
    if isinstance(value, str):
        if value == "NaN":
            return math.nan
        if value == "Infinity":
            return math.inf
        if value == "-Infinity":
            return -math.inf
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise AnimationClipRepackError(f"invalid float value {value!r}") from exc

def keyframe_stride(value_kind: str, weighted: bool) -> int:
    value_size = {
        "float": 4,
        "vector3": 12,
        "quaternion": 16,
    }.get(value_kind)
    if value_size is None:
        raise AnimationClipRepackError(
            f"unsupported AnimationClip keyframe value kind {value_kind}"
        )
    stride = 4 + value_size * 3
    if weighted:
        stride += 4 + value_size * 2
    return stride

def unity_version_tuple(version: str) -> tuple[int, int, int, int]:
    numbers: list[int] = []
    current = ""
    for char in version:
        if char.isdigit():
            current += char
        elif current:
            numbers.append(int(current))
            current = ""
    if current:
        numbers.append(int(current))
    numbers.extend([0] * (4 - len(numbers)))
    return tuple(numbers[:4])
