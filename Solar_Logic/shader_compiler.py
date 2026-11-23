from __future__ import annotations
import ctypes, datetime, os, re
from ctypes import (
    POINTER,
    WINFUNCTYPE,
    byref,
    c_char_p,
    c_size_t,
    c_uint,
    c_ulong,
    c_void_p,
    create_string_buffer,
)
from dataclasses import dataclass, field
from pathlib import Path

DLL_NAME = "d3dcompiler_47.dll"
LOG_NAME = "shader_compiler.log"

D3DCOMPILE_DEBUG = 1 << 0
D3DCOMPILE_SKIP_OPTIMIZATION = 1 << 2
D3DCOMPILE_PACK_MATRIX_ROW_MAJOR = 1 << 3
D3DCOMPILE_PACK_MATRIX_COLUMN_MAJOR = 1 << 4
D3DCOMPILE_OPTIMIZATION_LEVEL3 = 1 << 15

SHADER_TARGETS = (
    "ps_4_0",
    "vs_4_0",
    "ps_5_0",
    "vs_5_0",
    "ps_3_0",
    "vs_3_0",
    "cs_5_0",
    "gs_4_0",
)
DEFAULT_TARGET = "ps_4_0"
DEFAULT_ENTRY_POINT = "main"

class ShaderCompilerError(RuntimeError):
    pass

@dataclass
class CompileResult:
    ok: bool
    source: Path
    target: str
    entry_point: str
    bytecode: bytes = b""
    messages: str = ""
    log_path: Path | None = None
    reflection: dict = field(default_factory=dict)

    @property
    def byte_length(self) -> int:
        return len(self.bytecode)

def log_path_for(folder: Path) -> Path:
    return Path(folder) / LOG_NAME

def write_log(folder: Path, lines: list[str]) -> Path:
    path = log_path_for(folder)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with path.open("a", encoding="utf-8") as handle:
            handle.write(f"\n===== {stamp} =====\n")
            for line in lines:
                handle.write(line.rstrip() + "\n")
    except OSError:
        pass
    return path

def load_compiler() -> ctypes.WinDLL:
    if os.name != "nt":
        raise ShaderCompilerError(
            "the HLSL compiler is a Windows DLL, shader compilation needs Windows"
        )
    candidates = [DLL_NAME]
    system_root = os.environ.get("SystemRoot")
    if system_root:
        candidates.insert(0, str(Path(system_root) / "System32" / DLL_NAME))
    last_error = ""
    for candidate in candidates:
        try:
            return ctypes.WinDLL(candidate)
        except OSError as error:
            last_error = str(error)
    raise ShaderCompilerError(
        f"{DLL_NAME} couldnt be loaded ({last_error}), installing the DirectX runtime restores it"
    )

def blob_bytes(blob: c_void_p) -> bytes:
    if not blob:
        return b""
    vtable = ctypes.cast(blob, POINTER(c_void_p))[0]
    entries = ctypes.cast(vtable, POINTER(c_void_p))
    get_pointer = WINFUNCTYPE(c_void_p, c_void_p)(entries[3])
    get_size = WINFUNCTYPE(c_size_t, c_void_p)(entries[4])
    pointer = get_pointer(blob)
    size = get_size(blob)
    if not pointer or not size:
        return b""
    return ctypes.string_at(pointer, size)

def release_blob(blob: c_void_p) -> None:
    if not blob:
        return
    vtable = ctypes.cast(blob, POINTER(c_void_p))[0]
    entries = ctypes.cast(vtable, POINTER(c_void_p))
    release = WINFUNCTYPE(c_ulong, c_void_p)(entries[2])
    release(blob)

INCLUDE_PATTERN = re.compile(
    r'^[ \t]*#[ \t]*include[ \t]*[<"]([^>"]+)[>"][ \t]*(?://.*)?$'
)
MAX_INCLUDE_DEPTH = 32

def expand_includes(
    source_path: Path,
    include_roots: list[Path] | None = None,
    stack: tuple[Path, ...] = (),
) -> bytes:
    source_path = Path(source_path)
    if source_path in stack:
        chain = " -> ".join(item.name for item in stack + (source_path,))
        raise ShaderCompilerError(f"#include cycle: {chain}")
    if len(stack) >= MAX_INCLUDE_DEPTH:
        raise ShaderCompilerError(
            f"#include nesting is deeper than {MAX_INCLUDE_DEPTH} files at {source_path.name}"
        )
    roots = [Path(item) for item in (include_roots or [])]
    text = source_path.read_text(encoding="utf-8-sig", errors="replace")
    out: list[str] = []
    for number, line in enumerate(text.splitlines(), start=1):
        match = INCLUDE_PATTERN.match(line)
        if match is None:
            out.append(line)
            continue
        requested = match.group(1)
        if ".." in Path(requested.replace("\\", "/")).parts:
            raise ShaderCompilerError(
                f'{source_path.name} line {number}: #include "{requested}" walks out of the '
                "folder being compiled, which isnt allowed"
            )
        target = resolve_include(requested, source_path, roots)
        if target is None:
            raise ShaderCompilerError(
                f'{source_path.name} line {number}: #include "{requested}" wasnt found '
                f"beside it or in the folder being compiled"
            )
        body = expand_includes(target, roots, stack + (source_path,)).decode("utf-8", "replace")
        out.append(line_directive(1, target))
        out.append(body)
        out.append(line_directive(number + 1, source_path))
    return ("\n".join(out) + "\n").encode("utf-8")

def resolve_include(name: str, source_path: Path, roots: list[Path]) -> Path | None:
    candidate = name.replace("\\", "/").strip()
    if not candidate or ".." in Path(candidate).parts:
        return None
    for folder in [source_path.parent, *roots]:
        found = folder / candidate
        if found.is_file():
            return found.resolve()
    return None

def line_directive(number: int, path: Path) -> str:
    escaped = str(path).replace("\\", "/").replace('"', "")
    return '#line %d "%s"' % (number, escaped)

def compile_hlsl(
    source_path: Path,
    entry_point: str = DEFAULT_ENTRY_POINT,
    target: str = DEFAULT_TARGET,
    log_folder: Path | None = None,
    flags: int = D3DCOMPILE_OPTIMIZATION_LEVEL3,
    include_roots: list[Path] | None = None,
) -> CompileResult:
    source_path = Path(source_path)
    if target not in SHADER_TARGETS:
        raise ShaderCompilerError(
            f"unknown shader target {target!r}, expected one of {', '.join(SHADER_TARGETS)}"
        )
    try:
        source = expand_includes(source_path, include_roots)
    except OSError as error:
        raise ShaderCompilerError(f"{source_path.name} couldnt be read: {error}") from error

    library = load_compiler()
    prototype = library.D3DCompile
    prototype.restype = ctypes.c_long
    prototype.argtypes = [
        c_void_p,
        c_size_t,
        c_char_p,
        c_void_p,
        c_void_p,
        c_char_p,
        c_char_p,
        c_uint,
        c_uint,
        POINTER(c_void_p),
        POINTER(c_void_p),
    ]

    buffer = create_string_buffer(source, len(source))
    code_blob = c_void_p()
    error_blob = c_void_p()
    status = prototype(
        ctypes.cast(buffer, c_void_p),
        len(source),
        source_path.name.encode("utf-8"),
        None,
        None,
        entry_point.encode("ascii"),
        target.encode("ascii"),
        flags,
        0,
        byref(code_blob),
        byref(error_blob),
    )
    messages = blob_bytes(error_blob).decode("utf-8", "replace").strip()
    bytecode = blob_bytes(code_blob) if status >= 0 else b""
    release_blob(error_blob)
    release_blob(code_blob)

    ok = status >= 0 and bool(bytecode)
    result = CompileResult(
        ok=ok,
        source=source_path,
        target=target,
        entry_point=entry_point,
        bytecode=bytecode,
        messages=messages or ("" if ok else f"D3DCompile returned HRESULT 0x{status & 0xFFFFFFFF:08X}"),
    )
    if ok:
        try:
            result.reflection = reflect_bytecode(bytecode, library)
        except ShaderCompilerError as error:
            result.messages = (result.messages + "\n" + str(error)).strip()

    folder = Path(log_folder) if log_folder else source_path.parent
    lines = [
        f"source     : {source_path}",
        f"target     : {target}   entry point: {entry_point}",
        f"result     : {'compiled' if ok else 'FAILED'}",
    ]
    if ok:
        lines.append(f"bytecode   : {len(bytecode)} bytes")
        constants = result.reflection.get("constantBuffers") or []
        for buffer_info in constants:
            lines.append(f"cbuffer {buffer_info['name']} ({buffer_info['size']} bytes)")
            for variable in buffer_info["variables"]:
                lines.append(
                    "    %-28s offset %-5d size %d"
                    % (variable["name"], variable["offset"], variable["size"])
                )
        for binding in result.reflection.get("bindings") or []:
            lines.append(
                "bound %-28s type %-3d slot %d" % (binding["name"], binding["type"], binding["bindPoint"])
            )
    if result.messages:
        lines.append("compiler output:")
        lines.extend("    " + line for line in result.messages.splitlines())
    result.log_path = write_log(folder, lines)
    return result

def reflect_bytecode(bytecode: bytes, library: ctypes.WinDLL | None = None) -> dict:
    library = library or load_compiler()
    iid = (ctypes.c_ubyte * 16)(
        0xA1, 0x6C, 0x53, 0x8D, 0xCA, 0x0C, 0x56, 0x49,
        0xA8, 0x37, 0x78, 0x69, 0x63, 0x75, 0x55, 0x84,
    )
    reflect = library.D3DReflect
    reflect.restype = ctypes.c_long
    reflect.argtypes = [c_void_p, c_size_t, c_void_p, POINTER(c_void_p)]
    buffer = create_string_buffer(bytecode, len(bytecode))
    reflector = c_void_p()
    status = reflect(
        ctypes.cast(buffer, c_void_p), len(bytecode), ctypes.byref(iid), byref(reflector)
    )
    if status < 0 or not reflector:
        raise ShaderCompilerError(f"D3DReflect failed with HRESULT 0x{status & 0xFFFFFFFF:08X}")

    vtable = ctypes.cast(reflector, POINTER(c_void_p))[0]
    entries = ctypes.cast(vtable, POINTER(c_void_p))

    class ShaderDesc(ctypes.Structure):
        _fields_ = [
            ("Version", c_uint),
            ("Creator", c_char_p),
            ("Flags", c_uint),
            ("ConstantBuffers", c_uint),
            ("BoundResources", c_uint),
            ("InputParameters", c_uint),
            ("OutputParameters", c_uint),
            ("InstructionCount", c_uint),
            ("TempRegisterCount", c_uint),
            ("TempArrayCount", c_uint),
            ("DefCount", c_uint),
            ("DclCount", c_uint),
            ("TextureNormalInstructions", c_uint),
            ("TextureLoadInstructions", c_uint),
            ("TextureCompInstructions", c_uint),
            ("TextureBiasInstructions", c_uint),
            ("TextureGradientInstructions", c_uint),
            ("FloatInstructionCount", c_uint),
            ("IntInstructionCount", c_uint),
            ("UintInstructionCount", c_uint),
            ("StaticFlowControlCount", c_uint),
            ("DynamicFlowControlCount", c_uint),
            ("MacroInstructionCount", c_uint),
            ("ArrayInstructionCount", c_uint),
            ("CutInstructionCount", c_uint),
            ("EmitInstructionCount", c_uint),
            ("GSOutputTopology", c_uint),
            ("GSMaxOutputVertexCount", c_uint),
            ("InputPrimitive", c_uint),
            ("PatchConstantParameters", c_uint),
            ("cGSInstanceCount", c_uint),
            ("cControlPoints", c_uint),
            ("HSOutputPrimitive", c_uint),
            ("HSPartitioning", c_uint),
            ("TessellatorDomain", c_uint),
            ("cBarrierInstructions", c_uint),
            ("cInterlockedInstructions", c_uint),
            ("cTextureStoreInstructions", c_uint),
        ]

    class BindDesc(ctypes.Structure):
        _fields_ = [
            ("Name", c_char_p),
            ("Type", c_uint),
            ("BindPoint", c_uint),
            ("BindCount", c_uint),
            ("uFlags", c_uint),
            ("ReturnType", c_uint),
            ("Dimension", c_uint),
            ("NumSamples", c_uint),
        ]

    class BufferDesc(ctypes.Structure):
        _fields_ = [
            ("Name", c_char_p),
            ("Type", c_uint),
            ("Variables", c_uint),
            ("Size", c_uint),
            ("uFlags", c_uint),
        ]

    class VariableDesc(ctypes.Structure):
        _fields_ = [
            ("Name", c_char_p),
            ("StartOffset", c_uint),
            ("Size", c_uint),
            ("uFlags", c_uint),
            ("DefaultValue", c_void_p),
            ("StartTexture", c_uint),
            ("TextureSize", c_uint),
            ("StartSampler", c_uint),
            ("SamplerSize", c_uint),
        ]

    class TypeDesc(ctypes.Structure):
        _fields_ = [
            ("Class", c_uint),
            ("Type", c_uint),
            ("Rows", c_uint),
            ("Columns", c_uint),
            ("Elements", c_uint),
            ("Members", c_uint),
            ("Offset", c_uint),
            ("Name", c_char_p),
        ]

    get_desc = WINFUNCTYPE(ctypes.c_long, c_void_p, POINTER(ShaderDesc))(entries[3])
    get_constant_buffer = WINFUNCTYPE(c_void_p, c_void_p, c_uint)(entries[4])
    get_resource_binding = WINFUNCTYPE(ctypes.c_long, c_void_p, c_uint, POINTER(BindDesc))(entries[6])
    release = WINFUNCTYPE(c_ulong, c_void_p)(entries[2])

    description = ShaderDesc()
    get_desc(reflector, byref(description))

    constant_buffers = []
    for index in range(description.ConstantBuffers):
        buffer_pointer = get_constant_buffer(reflector, index)
        if not buffer_pointer:
            continue
        buffer_vtable = ctypes.cast(buffer_pointer, POINTER(c_void_p))[0]
        buffer_entries = ctypes.cast(buffer_vtable, POINTER(c_void_p))
        buffer_get_desc = WINFUNCTYPE(ctypes.c_long, c_void_p, POINTER(BufferDesc))(buffer_entries[0])
        buffer_get_variable = WINFUNCTYPE(c_void_p, c_void_p, c_uint)(buffer_entries[1])
        buffer_desc = BufferDesc()
        if buffer_get_desc(buffer_pointer, byref(buffer_desc)) < 0:
            continue
        variables = []
        for variable_index in range(buffer_desc.Variables):
            variable_pointer = buffer_get_variable(buffer_pointer, variable_index)
            if not variable_pointer:
                continue
            variable_vtable = ctypes.cast(variable_pointer, POINTER(c_void_p))[0]
            variable_entries = ctypes.cast(variable_vtable, POINTER(c_void_p))
            variable_get_desc = WINFUNCTYPE(
                ctypes.c_long, c_void_p, POINTER(VariableDesc)
            )(variable_entries[0])
            variable_desc = VariableDesc()
            if variable_get_desc(variable_pointer, byref(variable_desc)) < 0:
                continue
            record = {
                "name": (variable_desc.Name or b"").decode("utf-8", "replace"),
                "offset": int(variable_desc.StartOffset),
                "size": int(variable_desc.Size),
            }
            variable_get_type = WINFUNCTYPE(c_void_p, c_void_p)(variable_entries[1])
            type_pointer = variable_get_type(variable_pointer)
            if type_pointer:
                type_entries = ctypes.cast(
                    ctypes.cast(type_pointer, POINTER(c_void_p))[0], POINTER(c_void_p)
                )
                type_get_desc = WINFUNCTYPE(
                    ctypes.c_long, c_void_p, POINTER(TypeDesc)
                )(type_entries[0])
                type_desc = TypeDesc()
                if type_get_desc(type_pointer, byref(type_desc)) >= 0:
                    record.update(
                        {
                            "class": int(type_desc.Class),
                            "type": int(type_desc.Type),
                            "rows": int(type_desc.Rows),
                            "columns": int(type_desc.Columns),
                            "elements": int(type_desc.Elements),
                        }
                    )
            variables.append(record)
        constant_buffers.append(
            {
                "name": (buffer_desc.Name or b"").decode("utf-8", "replace"),
                "size": int(buffer_desc.Size),
                "variables": variables,
            }
        )

    bindings = []
    for index in range(description.BoundResources):
        bind_desc = BindDesc()
        if get_resource_binding(reflector, index, byref(bind_desc)) < 0:
            continue
        bindings.append(
            {
                "name": (bind_desc.Name or b"").decode("utf-8", "replace"),
                "type": int(bind_desc.Type),
                "bindPoint": int(bind_desc.BindPoint),
                "bindCount": int(bind_desc.BindCount),
            }
        )

    result = {
        "version": int(description.Version),
        "creator": (description.Creator or b"").decode("utf-8", "replace"),
        "instructionCount": int(description.InstructionCount),
        "constantBuffers": constant_buffers,
        "bindings": bindings,
    }
    release(reflector)
    return result

def disassemble(bytecode: bytes) -> str:
    library = load_compiler()
    prototype = library.D3DDisassemble
    prototype.restype = ctypes.c_long
    prototype.argtypes = [c_void_p, c_size_t, c_uint, c_char_p, POINTER(c_void_p)]
    buffer = create_string_buffer(bytecode, len(bytecode))
    blob = c_void_p()
    status = prototype(ctypes.cast(buffer, c_void_p), len(bytecode), 0, None, byref(blob))
    if status < 0:
        raise ShaderCompilerError(f"D3DDisassemble failed with HRESULT 0x{status & 0xFFFFFFFF:08X}")
    text = blob_bytes(blob).decode("utf-8", "replace")
    release_blob(blob)
    return text

HLSL_EXTENSIONS = {".hlsl", ".frag", ".vert", ".fx"}
INCLUDE_EXTENSIONS = {".hlsli", ".inc", ".cginc"}

ENTRY_POINT_CANDIDATES = (
    "main",
    "frag",
    "vert",
    "PSMain",
    "VSMain",
    "MainPS",
    "MainVS",
    "PS",
    "VS",
    "pixel",
    "vertex",
    "ps_main",
    "vs_main",
)

PIXEL_HINTS = (".ps.", "_ps", "-ps", "pixel", "frag", "fragment")
VERTEX_HINTS = (".vs.", "_vs", "-vs", "vertex", "vert")
COMPUTE_HINTS = (".cs.", "_cs", "-cs", "compute", "kernel")

def guess_target(path: Path, default_target: str = DEFAULT_TARGET) -> str:
    lowered = path.name.casefold()
    model = "5_0" if default_target.endswith("5_0") else "4_0"
    if any(hint in lowered for hint in COMPUTE_HINTS):
        return "cs_5_0"
    if any(hint in lowered for hint in PIXEL_HINTS):
        return f"ps_{model}"
    if any(hint in lowered for hint in VERTEX_HINTS):
        return f"vs_{model}"
    return default_target

def discover_hlsl_sources(folder: Path) -> list[Path]:
    folder = Path(folder)
    found: list[Path] = []
    for path in sorted(folder.rglob("*")):
        if not path.is_file():
            continue
        suffix = path.suffix.casefold()
        if suffix in INCLUDE_EXTENSIONS:
            continue
        if suffix in HLSL_EXTENSIONS:
            found.append(path)
    return found

def compile_source_detecting_entry(
    source_path: Path,
    target: str,
    log_folder: Path | None = None,
    entry_point: str | None = None,
    include_roots: list[Path] | None = None,
) -> CompileResult:
    if entry_point:
        return compile_hlsl(source_path, entry_point, target, log_folder, include_roots=include_roots)
    try:
        text = source_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        text = ""
    ordered = [name for name in ENTRY_POINT_CANDIDATES if name in text]
    ordered += [name for name in ENTRY_POINT_CANDIDATES if name not in ordered]
    first: CompileResult | None = None
    for candidate in ordered:
        result = compile_hlsl(source_path, candidate, target, log_folder, include_roots=include_roots)
        if result.ok:
            return result
        if first is None:
            first = result
    if first is not None:
        return first
    return compile_hlsl(source_path, DEFAULT_ENTRY_POINT, target, log_folder, include_roots=include_roots)

@dataclass
class FolderResult:
    folder: Path
    output_folder: Path
    log_path: Path | None = None
    compiled: list[CompileResult] = field(default_factory=list)
    failed: list[CompileResult] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.compiled) + len(self.failed)

def compile_folder(
    folder: Path,
    output_folder: Path | None = None,
    default_target: str = DEFAULT_TARGET,
    entry_point: str | None = None,
) -> FolderResult:
    import json

    folder = Path(folder)
    if not folder.is_dir():
        raise ShaderCompilerError(f"{folder} isnt a folder")
    output_folder = Path(output_folder) if output_folder else folder / "compiled"
    output_folder.mkdir(parents=True, exist_ok=True)

    sources = discover_hlsl_sources(folder)
    stems = output_stems(folder, sources)
    result = FolderResult(folder=folder, output_folder=output_folder)
    lines = [
        f"folder     : {folder}",
        f"output     : {output_folder}",
        f"found      : {len(sources)} HLSL file(s)",
    ]
    if not sources:
        lines.append("nothing to compile: no .hlsl files were found in that folder")

    for source_path in sources:
        target = guess_target(source_path, default_target)
        compiled = compile_source_detecting_entry(
            source_path, target, output_folder, entry_point, include_roots=[folder]
        )
        if compiled.ok:
            stem = stems[source_path]
            binary_path = output_folder / f"{stem}.{target}.dxbc"
            binary_path.write_bytes(compiled.bytecode)
            sidecar = output_folder / f"{stem}.{target}.reflection.json"
            sidecar.write_text(
                json.dumps(compiled.reflection, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            write_disassembly(output_folder / f"{stem}.{target}.asm", compiled.bytecode)
            result.compiled.append(compiled)
            lines.append(
                "OK    %-42s %-7s entry %-8s %6d bytes"
                % (source_path.name, target, compiled.entry_point, compiled.byte_length)
            )
        else:
            result.failed.append(compiled)
            lines.append("FAIL  %-42s %-7s" % (source_path.name, target))
            for message in compiled.messages.splitlines()[:6]:
                lines.append("          " + message)

    lines.append(
        "summary    : %d compiled, %d failed" % (len(result.compiled), len(result.failed))
    )
    result.log_path = write_log(output_folder, lines)
    return result

def output_stems(folder: Path, sources: list[Path]) -> dict[Path, str]:
    counts: dict[str, int] = {}
    for path in sources:
        counts[path.stem.casefold()] = counts.get(path.stem.casefold(), 0) + 1
    stems: dict[Path, str] = {}
    taken: set[str] = set()
    for path in sources:
        stem = path.stem
        if counts.get(stem.casefold(), 0) > 1:
            try:
                relative = path.relative_to(folder).parent.as_posix()
            except ValueError:
                relative = ""
            prefix = relative.replace("/", "_").strip("_")
            if prefix:
                stem = f"{prefix}_{stem}"
        candidate = stem
        counter = 2
        while candidate.casefold() in taken:
            candidate = f"{stem}_{counter}"
            counter += 1
        taken.add(candidate.casefold())
        stems[path] = candidate
    return stems

def write_disassembly(path: Path, bytecode: bytes) -> bool:
    try:
        path.write_text(disassemble(bytecode), encoding="utf-8")
        return True
    except (ShaderCompilerError, OSError, UnicodeError):
        return False

def compiler_available() -> tuple[bool, str]:
    try:
        library = load_compiler()
    except ShaderCompilerError as error:
        return False, str(error)
    missing = [name for name in ("D3DCompile", "D3DReflect") if not hasattr(library, name)]
    if missing:
        return False, f"{DLL_NAME} is missing {', '.join(missing)}"
    return True, f"{DLL_NAME} loaded"
