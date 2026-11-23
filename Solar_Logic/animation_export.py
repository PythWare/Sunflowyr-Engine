from __future__ import annotations
import base64, json, math, struct
from pathlib import Path
from typing import Any, Callable
from .binary import EndianReader
from .class_ids import class_name
from .serialized import SerializedAsset, SerializedObject

class AnimationClipExportError(ValueError):
    pass

def export_animation_clip_json(
    asset: SerializedAsset,
    obj: SerializedObject,
    out_path: Path,
    metadata: dict,
) -> dict:
    document, conversion = read_animation_clip_document(asset, obj)
    metadata["conversion"] = conversion
    document["conversion"] = conversion
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(document, handle, ensure_ascii=False, indent=2, allow_nan=False)
        handle.write("\n")

    return conversion

def read_animation_clip_document(
    asset: SerializedAsset,
    obj: SerializedObject,
) -> tuple[dict[str, Any], dict[str, Any]]:
    with asset.path.open("rb") as handle:
        parser = AnimationClipParser(asset, EndianReader(handle, asset.endian), obj)
        document = parser.read()
    return document, parser.conversion(document)

class AnimationClipParser:
    def __init__(self, asset: SerializedAsset, reader: EndianReader, obj: SerializedObject) -> None:
        self.asset = asset
        self.reader = reader
        self.obj = obj
        self.version = unity_version_tuple(asset.unity_version)
        self.object_end = obj.byte_start + obj.byte_size
        self.unparsed_tail_size = 0

    def read(self) -> dict:
        r = self.reader
        r.seek(self.obj.byte_start)
        name = self.read_string()

        if self.version[0] >= 5:
            legacy = r.bool()
            animation_type = None
        elif self.version[0] >= 4:
            animation_type = r.i32()
            legacy = animation_type == 1
        else:
            animation_type = 1
            legacy = True
        compressed = r.bool()
        use_high_quality_curve = False
        if self.version_at_least(4, 3):
            use_high_quality_curve = r.bool()
        r.align()

        document: dict[str, Any] = {
            "m_Name": name,
            "m_AnimationType": animation_type,
            "m_Legacy": legacy,
            "m_Compressed": compressed,
            "m_UseHighQualityCurve": use_high_quality_curve,
            "m_RotationCurves": self.read_array("m_RotationCurves", self.read_quaternion_curve, 100000, 4),
            "m_CompressedRotationCurves": self.read_array(
                "m_CompressedRotationCurves", self.read_compressed_animation_curve, 100000, 4
            ),
        }
        if self.version_at_least(5, 3):
            document["m_EulerCurves"] = self.read_array("m_EulerCurves", self.read_vector3_curve, 100000, 4)
        else:
            document["m_EulerCurves"] = []

        document.update(
            {
                "m_PositionCurves": self.read_array("m_PositionCurves", self.read_vector3_curve, 100000, 4),
                "m_ScaleCurves": self.read_array("m_ScaleCurves", self.read_vector3_curve, 100000, 4),
                "m_FloatCurves": self.read_array("m_FloatCurves", self.read_float_curve, 100000, 4),
            }
        )
        if self.version_at_least(4, 3):
            document["m_PPtrCurves"] = self.read_array("m_PPtrCurves", self.read_pptr_curve, 100000, 4)
        else:
            document["m_PPtrCurves"] = []

        document["m_SampleRate"] = self.f32()
        document["m_WrapMode"] = r.i32()
        document["m_Bounds"] = self.read_aabb() if self.version_at_least(3, 4) else None
        if self.version[0] >= 4:
            document["m_MuscleClipSize"] = r.u32()
            document["m_MuscleClip"] = self.read_clip_muscle_constant()
        else:
            document["m_MuscleClipSize"] = 0
            document["m_MuscleClip"] = None
        if self.version_at_least(4, 3):
            document["m_ClipBindingConstant"] = self.read_animation_clip_binding_constant()
        else:
            document["m_ClipBindingConstant"] = None
        if self.version_at_least(2018, 3):
            document["m_HasGenericRootTransform"] = r.bool()
            document["m_HasMotionFloatCurves"] = r.bool()
            r.align()
        document["m_Events"] = self.read_array("m_Events", self.read_animation_event, 100000, 4)
        if self.version[0] >= 2017:
            r.align()

        if r.tell() > self.object_end:
            raise AnimationClipExportError(
                f"AnimationClip reader passed object end at 0x{r.tell():x}, expected 0x{self.object_end:x}"
            )
        if r.tell() < self.object_end:
            tail = r.read(self.object_end - r.tell())
            self.unparsed_tail_size = len(tail)
            document["solarUnparsedTail"] = {
                "size": len(tail),
                "encoding": "base64",
                "data": base64.b64encode(tail).decode("ascii"),
            }
        return document

    def conversion(self, document: dict) -> dict:
        muscle = document.get("m_MuscleClip") or {}
        clip = muscle.get("m_Clip") or {}
        dense = clip.get("m_DenseClip") or {}
        streamed = clip.get("m_StreamedClip") or {}
        start = muscle.get("m_StartTime")
        stop = muscle.get("m_StopTime")
        duration = None
        if isinstance(start, (int, float)) and isinstance(stop, (int, float)):
            duration = stop - start
        return {
            "format": "animation_clip_json_unity54",
            "name": document.get("m_Name", ""),
            "legacy": document.get("m_Legacy"),
            "compressed": document.get("m_Compressed"),
            "sample_rate": document.get("m_SampleRate"),
            "duration": duration,
            "rotation_curve_count": len(document.get("m_RotationCurves") or []),
            "compressed_rotation_curve_count": len(document.get("m_CompressedRotationCurves") or []),
            "euler_curve_count": len(document.get("m_EulerCurves") or []),
            "position_curve_count": len(document.get("m_PositionCurves") or []),
            "scale_curve_count": len(document.get("m_ScaleCurves") or []),
            "float_curve_count": len(document.get("m_FloatCurves") or []),
            "pptr_curve_count": len(document.get("m_PPtrCurves") or []),
            "event_count": len(document.get("m_Events") or []),
            "streamed_curve_count": streamed.get("curveCount"),
            "dense_frame_count": dense.get("m_FrameCount"),
            "dense_curve_count": dense.get("m_CurveCount"),
            "unparsed_tail_size": self.unparsed_tail_size,
        }

    def read_quaternion_curve(self) -> dict:
        return {
            "curve": self.read_animation_curve("quaternion", self.read_quaternion),
            "path": self.read_string(),
        }

    def read_vector3_curve(self) -> dict:
        return {
            "curve": self.read_animation_curve("vector3", self.read_vector3),
            "path": self.read_string(),
        }

    def read_float_curve(self) -> dict:
        return {
            "curve": self.read_animation_curve("float", self.f32),
            "attribute": self.read_string(),
            "path": self.read_string(),
            "classID": self.reader.i32(),
            "script": self.read_pptr(),
        }

    def read_pptr_curve(self) -> dict:
        keyframes = self.read_array("PPtrCurve.curve", self.read_pptr_keyframe, 100000, 4)
        return {
            "curve": keyframes,
            "attribute": self.read_string(),
            "path": self.read_string(),
            "classID": self.reader.i32(),
            "script": self.read_pptr(),
        }

    def read_pptr_keyframe(self) -> dict:
        return {
            "time": self.f32(),
            "value": self.read_pptr(),
        }

    def read_animation_curve(self, value_kind: str, value_reader: Callable[[], Any]) -> dict:
        key_count = self.read_count(f"AnimationCurve<{value_kind}>.m_Curve", 500000, 16)
        inline_limit = 32 if value_kind == "float" else 24
        if key_count > inline_limit:
            stride = self.keyframe_stride(value_kind)
            raw = self.reader.read(key_count * stride)
            keys: list | dict = self.keyframe_blob(value_kind, key_count, raw, stride)
        else:
            keys = [self.read_keyframe(value_reader) for _ in range(key_count)]
        curve = {
            "m_Curve": keys,
            "m_PreInfinity": self.reader.i32(),
            "m_PostInfinity": self.reader.i32(),
        }
        if self.version_at_least(5, 3):
            curve["m_RotationOrder"] = self.reader.i32()
        return curve

    def read_keyframe(self, value_reader: Callable[[], Any]) -> dict:
        keyframe = {
            "time": self.f32(),
            "value": value_reader(),
            "inSlope": value_reader(),
            "outSlope": value_reader(),
        }
        if self.version[0] >= 2018:
            keyframe["weightedMode"] = self.reader.i32()
            keyframe["inWeight"] = value_reader()
            keyframe["outWeight"] = value_reader()
        return keyframe

    def read_compressed_animation_curve(self) -> dict:
        return {
            "m_Path": self.read_string(),
            "m_Times": self.read_packed_int_vector(),
            "m_Values": self.read_packed_quat_vector(),
            "m_Slopes": self.read_packed_float_vector(),
            "m_PreInfinity": self.reader.i32(),
            "m_PostInfinity": self.reader.i32(),
        }

    def read_packed_int_vector(self) -> dict:
        num_items = self.reader.u32()
        data = self.read_byte_array("PackedIntVector.m_Data")
        bit_size = self.reader.u8()
        self.reader.align()
        return {
            "m_NumItems": num_items,
            "m_Data": self.bytes_blob(data),
            "m_BitSize": bit_size,
        }

    def read_packed_quat_vector(self) -> dict:
        num_items = self.reader.u32()
        data = self.read_byte_array("PackedQuatVector.m_Data")
        self.reader.align()
        return {
            "m_NumItems": num_items,
            "m_Data": self.bytes_blob(data),
        }

    def read_packed_float_vector(self) -> dict:
        num_items = self.reader.u32()
        value_range = self.f32()
        start = self.f32()
        data = self.read_byte_array("PackedFloatVector.m_Data")
        self.reader.align()
        bit_size = self.reader.u8()
        self.reader.align()
        return {
            "m_NumItems": num_items,
            "m_Range": value_range,
            "m_Start": start,
            "m_Data": self.bytes_blob(data),
            "m_BitSize": bit_size,
        }

    def read_aabb(self) -> dict:
        return {
            "m_Center": self.read_vector3(),
            "m_Extent": self.read_vector3(),
        }

    def read_xform(self) -> dict:
        return {
            "t": self.read_vector3() if self.version_at_least(5, 4) else self.read_vector4(),
            "q": self.read_quaternion(),
            "s": self.read_vector3() if self.version_at_least(5, 4) else self.read_vector4(),
        }

    def read_hand_pose(self) -> dict:
        return {
            "m_GrabX": self.read_xform(),
            "m_DoFArray": self.read_float_array("HandPose.m_DoFArray", inline_limit=512),
            "m_Override": self.f32(),
            "m_CloseOpen": self.f32(),
            "m_InOut": self.f32(),
            "m_Grab": self.f32(),
        }

    def read_human_goal(self) -> dict:
        goal = {
            "m_X": self.read_xform(),
            "m_WeightT": self.f32(),
            "m_WeightR": self.f32(),
        }
        if self.version[0] >= 5:
            goal["m_HintT"] = self.read_vector3() if self.version_at_least(5, 4) else self.read_vector4()
            goal["m_HintWeightT"] = self.f32()
        return goal

    def read_human_pose(self) -> dict:
        pose = {
            "m_RootX": self.read_xform(),
            "m_LookAtPosition": self.read_vector3() if self.version_at_least(5, 4) else self.read_vector4(),
            "m_LookAtWeight": self.read_vector4(),
            "m_GoalArray": self.read_array("HumanPose.m_GoalArray", self.read_human_goal, 256, 16),
            "m_LeftHandPose": self.read_hand_pose(),
            "m_RightHandPose": self.read_hand_pose(),
            "m_DoFArray": self.read_float_array("HumanPose.m_DoFArray", inline_limit=512),
        }
        if self.version_at_least(5, 2):
            pose["m_TDoFArray"] = self.read_vector3_array("HumanPose.m_TDoFArray", inline_limit=512)
        return pose

    def read_streamed_clip(self) -> dict:
        return {
            "data": self.read_u32_array("StreamedClip.data", inline_limit=2048),
            "curveCount": self.reader.u32(),
        }

    def read_dense_clip(self) -> dict:
        return {
            "m_FrameCount": self.reader.i32(),
            "m_CurveCount": self.reader.u32(),
            "m_SampleRate": self.f32(),
            "m_BeginTime": self.f32(),
            "m_SampleArray": self.read_float_array("DenseClip.m_SampleArray", inline_limit=2048),
        }

    def read_constant_clip(self) -> dict:
        return {
            "data": self.read_float_array("ConstantClip.data", inline_limit=2048),
        }

    def read_value_constant(self) -> dict:
        value = {
            "m_ID": self.reader.u32(),
        }
        if self.version[0] < 5 or (self.version[0] == 5 and self.version[1] < 5):
            value["m_TypeID"] = self.reader.u32()
        value["m_Type"] = self.reader.u32()
        value["m_Index"] = self.reader.u32()
        return value

    def read_value_array_constant(self) -> dict:
        values = self.read_value_constant_array("ValueArrayConstant.m_ValueArray")
        return {
            "m_ValueArray": values,
        }

    def read_clip(self) -> dict:
        clip = {
            "m_StreamedClip": self.read_streamed_clip(),
            "m_DenseClip": self.read_dense_clip(),
        }
        if self.version_at_least(4, 3):
            clip["m_ConstantClip"] = self.read_constant_clip()
        if self.version[0] < 2018 or (self.version[0] == 2018 and self.version[1] < 3):
            clip["m_Binding"] = self.read_value_array_constant()
        return clip

    def read_value_delta(self) -> dict:
        return {
            "m_Start": self.f32(),
            "m_Stop": self.f32(),
        }

    def read_clip_muscle_constant(self) -> dict:
        muscle = {
            "m_DeltaPose": self.read_human_pose(),
            "m_StartX": self.read_xform(),
        }
        if self.version_at_least(5, 5):
            muscle["m_StopX"] = self.read_xform()
        muscle["m_LeftFootStartX"] = self.read_xform()
        muscle["m_RightFootStartX"] = self.read_xform()
        if self.version[0] < 5:
            muscle["m_MotionStartX"] = self.read_xform()
            muscle["m_MotionStopX"] = self.read_xform()
        muscle["m_AverageSpeed"] = self.read_vector3() if self.version_at_least(5, 4) else self.read_vector4()
        muscle["m_Clip"] = self.read_clip()
        muscle.update(
            {
                "m_StartTime": self.f32(),
                "m_StopTime": self.f32(),
                "m_OrientationOffsetY": self.f32(),
                "m_Level": self.f32(),
                "m_CycleOffset": self.f32(),
                "m_AverageAngularSpeed": self.f32(),
                "m_IndexArray": self.read_i32_array("ClipMuscleConstant.m_IndexArray", inline_limit=2048),
            }
        )
        if self.version[0] < 4 or (self.version[0] == 4 and self.version[1] < 3):
            muscle["m_AdditionalCurveIndexArray"] = self.read_i32_array(
                "ClipMuscleConstant.m_AdditionalCurveIndexArray", inline_limit=2048
            )
        muscle["m_ValueArrayDelta"] = self.read_value_delta_array("ClipMuscleConstant.m_ValueArrayDelta")
        if self.version_at_least(5, 3):
            muscle["m_ValueArrayReferencePose"] = self.read_float_array(
                "ClipMuscleConstant.m_ValueArrayReferencePose", inline_limit=2048
            )
        muscle["m_Mirror"] = self.reader.bool()
        if self.version_at_least(4, 3):
            muscle["m_LoopTime"] = self.reader.bool()
        muscle["m_LoopBlend"] = self.reader.bool()
        muscle["m_LoopBlendOrientation"] = self.reader.bool()
        muscle["m_LoopBlendPositionY"] = self.reader.bool()
        muscle["m_LoopBlendPositionXZ"] = self.reader.bool()
        if self.version_at_least(5, 5):
            muscle["m_StartAtOrigin"] = self.reader.bool()
        muscle["m_KeepOriginalOrientation"] = self.reader.bool()
        muscle["m_KeepOriginalPositionY"] = self.reader.bool()
        muscle["m_KeepOriginalPositionXZ"] = self.reader.bool()
        muscle["m_HeightFromFeet"] = self.reader.bool()
        self.reader.align()
        return muscle

    def read_generic_binding(self) -> dict:
        path = self.reader.u32()
        attribute = self.reader.u32()
        script = self.read_pptr()
        if self.version_at_least(5, 6):
            type_id = self.reader.i32()
        else:
            type_id = self.reader.u16()
        binding = {
            "path": path,
            "attribute": attribute,
            "script": script,
            "typeID": type_id,
            "typeName": class_name(type_id),
            "customType": self.reader.u8(),
            "isPPtrCurve": self.reader.u8(),
        }
        if self.version_at_least(2022, 1):
            binding["isIntCurve"] = self.reader.u8()
        self.reader.align()
        return binding

    def read_animation_clip_binding_constant(self) -> dict:
        return {
            "genericBindings": self.read_array(
                "AnimationClipBindingConstant.genericBindings", self.read_generic_binding, 1000000, 16
            ),
            "pptrCurveMapping": self.read_array(
                "AnimationClipBindingConstant.pptrCurveMapping", self.read_pptr, 1000000, 12
            ),
        }

    def read_animation_event(self) -> dict:
        event = {
            "time": self.f32(),
            "functionName": self.read_string(),
            "data": self.read_string(),
            "objectReferenceParameter": self.read_pptr(),
            "floatParameter": self.f32(),
        }
        if self.version[0] >= 3:
            event["intParameter"] = self.reader.i32()
        event["messageOptions"] = self.reader.i32()
        return event

    def read_vector3(self) -> dict:
        return {
            "x": self.f32(),
            "y": self.f32(),
            "z": self.f32(),
        }

    def read_vector4(self) -> dict:
        return {
            "x": self.f32(),
            "y": self.f32(),
            "z": self.f32(),
            "w": self.f32(),
        }

    def read_quaternion(self) -> dict:
        return self.read_vector4()

    def read_pptr(self) -> dict:
        file_id_offset = self.reader.tell()
        file_id = self.reader.i32()
        path_id_offset = self.reader.tell()
        path_id = self.reader.i32() if self.asset.format_version < 14 else self.reader.i64()
        return {
            "m_FileID": file_id,
            "m_PathID": path_id,
            "file_id_offset": file_id_offset,
            "path_id_offset": path_id_offset,
        }

    def read_array(
        self,
        label: str,
        item_reader: Callable[[], Any],
        max_count: int,
        min_item_size: int = 0,
    ) -> list:
        count = self.read_count(label, max_count, min_item_size)
        return [item_reader() for _ in range(count)]

    def read_float_array(self, label: str, inline_limit: int = 2048) -> list | dict:
        count = self.read_count(label, 20_000_000, 4)
        raw = self.reader.read(count * 4)
        if count <= inline_limit:
            return [self.json_float(value) for value in self.unpack_f32_values(raw, count)]
        preview_count = min(count, 16)
        preview = [self.json_float(value) for value in self.unpack_f32_values(raw[: preview_count * 4], preview_count)]
        return self.array_blob("float32", count, raw, preview)

    def read_i32_array(self, label: str, inline_limit: int = 2048) -> list | dict:
        count = self.read_count(label, 20_000_000, 4)
        raw = self.reader.read(count * 4)
        if count <= inline_limit:
            return list(struct.unpack(self.reader.endian + f"{count}i", raw)) if count else []
        preview_count = min(count, 16)
        preview = list(struct.unpack(self.reader.endian + f"{preview_count}i", raw[: preview_count * 4]))
        return self.array_blob("int32", count, raw, preview)

    def read_u32_array(self, label: str, inline_limit: int = 2048) -> list | dict:
        count = self.read_count(label, 20_000_000, 4)
        raw = self.reader.read(count * 4)
        if count <= inline_limit:
            return list(struct.unpack(self.reader.endian + f"{count}I", raw)) if count else []
        preview_count = min(count, 16)
        preview = list(struct.unpack(self.reader.endian + f"{preview_count}I", raw[: preview_count * 4]))
        return self.array_blob("uint32", count, raw, preview)

    def read_vector3_array(self, label: str, inline_limit: int = 512) -> list | dict:
        count = self.read_count(label, 5_000_000, 12)
        if count <= inline_limit:
            return [self.read_vector3() for _ in range(count)]
        raw = self.reader.read(count * 12)
        preview: list[dict] = []
        for index in range(min(count, 8)):
            x, y, z = struct.unpack(self.reader.endian + "fff", raw[index * 12 : index * 12 + 12])
            preview.append({"x": self.json_float(x), "y": self.json_float(y), "z": self.json_float(z)})
        return self.array_blob("vector3_float32", count, raw, preview, stride=12)

    def read_value_delta_array(self, label: str, inline_limit: int = 2048) -> list | dict:
        count = self.read_count(label, 5_000_000, 8)
        if count <= inline_limit:
            return [self.read_value_delta() for _ in range(count)]
        raw = self.reader.read(count * 8)
        preview = []
        for index in range(min(count, 8)):
            start, stop = struct.unpack(self.reader.endian + "ff", raw[index * 8 : index * 8 + 8])
            preview.append({"m_Start": self.json_float(start), "m_Stop": self.json_float(stop)})
        return self.array_blob("value_delta", count, raw, preview, stride=8)

    def read_value_constant_array(self, label: str, inline_limit: int = 4096) -> list | dict:
        has_type_id = self.version[0] < 5 or (self.version[0] == 5 and self.version[1] < 5)
        stride = 16 if has_type_id else 12
        count = self.read_count(label, 5_000_000, stride)
        if count <= inline_limit:
            return [self.read_value_constant() for _ in range(count)]
        raw = self.reader.read(count * stride)
        preview = []
        for index in range(min(count, 8)):
            chunk = raw[index * stride : index * stride + stride]
            if has_type_id:
                value_id, type_id, value_type, value_index = struct.unpack(self.reader.endian + "IIII", chunk)
                preview.append(
                    {
                        "m_ID": value_id,
                        "m_TypeID": type_id,
                        "m_Type": value_type,
                        "m_Index": value_index,
                    }
                )
            else:
                value_id, value_type, value_index = struct.unpack(self.reader.endian + "III", chunk)
                preview.append({"m_ID": value_id, "m_Type": value_type, "m_Index": value_index})
        return self.array_blob("value_constant", count, raw, preview, stride=stride)

    def read_byte_array(self, label: str) -> bytes:
        count = self.read_count(label, 200_000_000, 1)
        data = self.reader.read(count)
        self.reader.align()
        return data

    def read_count(self, label: str, max_count: int, min_item_size: int = 0) -> int:
        count = self.reader.i32()
        if count < 0 or count > max_count:
            raise AnimationClipExportError(f"{label} has invalid count {count}")
        if min_item_size and count * min_item_size > self.object_end - self.reader.tell():
            raise AnimationClipExportError(
                f"{label} count {count} cant fit in object at 0x{self.reader.tell():x}"
            )
        return count

    def read_string(self, max_length: int = 65536) -> str:
        length = self.reader.i32()
        remaining = self.object_end - self.reader.tell()
        if length < 0 or length > remaining or length > max_length:
            raise AnimationClipExportError(f"invalid aligned string length {length} at 0x{self.reader.tell() - 4:x}")
        if length == 0:
            return ""
        data = self.reader.read(length)
        self.reader.align()
        return data.decode("utf-8", "replace")

    def f32(self) -> float | str:
        return self.json_float(self.reader.f32())

    def json_float(self, value: float) -> float | str:
        if math.isfinite(value):
            return value
        if math.isnan(value):
            return "NaN"
        return "Infinity" if value > 0 else "-Infinity"

    def unpack_f32_values(self, raw: bytes, count: int) -> tuple[float, ...]:
        if count <= 0:
            return ()
        return struct.unpack(self.reader.endian + f"{count}f", raw)

    def bytes_blob(self, data: bytes) -> dict:
        return {
            "size": len(data),
            "encoding": "base64",
            "data": base64.b64encode(data).decode("ascii"),
        }

    def array_blob(
        self,
        item_type: str,
        count: int,
        data: bytes,
        preview: list,
        *,
        stride: int = 4,
    ) -> dict:
        byte_order = "little" if self.reader.endian == "<" else "big"
        return {
            "count": count,
            "item_type": item_type,
            "byte_order": byte_order,
            "stride": stride,
            "encoding": "base64",
            "data": base64.b64encode(data).decode("ascii"),
            "sampleValues": preview,
        }

    def keyframe_stride(self, value_kind: str) -> int:
        value_size = {
            "float": 4,
            "vector3": 12,
            "quaternion": 16,
        }[value_kind]
        stride = 4 + value_size * 3
        if self.version[0] >= 2018:
            stride += 4 + value_size * 2
        return stride

    def keyframe_blob(self, value_kind: str, count: int, data: bytes, stride: int) -> dict:
        preview = []
        offset = 0
        for _ in range(min(count, 4)):
            item, offset = self.decode_keyframe_preview(value_kind, data, offset)
            preview.append(item)
        blob = self.array_blob(f"keyframe_{value_kind}", count, data, preview, stride=stride)
        blob["layout"] = {
            "fields": ["time", "value", "inSlope", "outSlope"],
            "value_kind": value_kind,
            "weighted": self.version[0] >= 2018,
        }
        return blob

    def decode_keyframe_preview(self, value_kind: str, data: bytes, offset: int) -> tuple[dict, int]:
        time, offset = self.decode_float(data, offset)
        value, offset = self.decode_value_preview(value_kind, data, offset)
        in_slope, offset = self.decode_value_preview(value_kind, data, offset)
        out_slope, offset = self.decode_value_preview(value_kind, data, offset)
        item = {
            "time": time,
            "value": value,
            "inSlope": in_slope,
            "outSlope": out_slope,
        }
        if self.version[0] >= 2018:
            weighted_mode = struct.unpack(self.reader.endian + "i", data[offset : offset + 4])[0]
            offset += 4
            in_weight, offset = self.decode_value_preview(value_kind, data, offset)
            out_weight, offset = self.decode_value_preview(value_kind, data, offset)
            item.update({"weightedMode": weighted_mode, "inWeight": in_weight, "outWeight": out_weight})
        return item, offset

    def decode_value_preview(self, value_kind: str, data: bytes, offset: int) -> tuple[Any, int]:
        if value_kind == "float":
            return self.decode_float(data, offset)
        if value_kind == "vector3":
            x, y, z = struct.unpack(self.reader.endian + "fff", data[offset : offset + 12])
            return {
                "x": self.json_float(x),
                "y": self.json_float(y),
                "z": self.json_float(z),
            }, offset + 12
        if value_kind == "quaternion":
            x, y, z, w = struct.unpack(self.reader.endian + "ffff", data[offset : offset + 16])
            return {
                "x": self.json_float(x),
                "y": self.json_float(y),
                "z": self.json_float(z),
                "w": self.json_float(w),
            }, offset + 16
        raise AnimationClipExportError(f"unsupported keyframe preview value kind {value_kind!r}")

    def decode_float(self, data: bytes, offset: int) -> tuple[float | str, int]:
        value = struct.unpack(self.reader.endian + "f", data[offset : offset + 4])[0]
        return self.json_float(value), offset + 4

    def version_at_least(self, major: int, minor: int = 0) -> bool:
        return self.version[0] > major or (self.version[0] == major and self.version[1] >= minor)

def unity_version_tuple(version: str) -> tuple[int, int, int, int]:
    nums: list[int] = []
    current = ""
    for char in version:
        if char.isdigit():
            current += char
        elif current:
            nums.append(int(current))
            current = ""
    if current:
        nums.append(int(current))
    nums.extend([0] * (4 - len(nums)))
    return tuple(nums[:4])
