from __future__ import annotations
from dataclasses import dataclass
from struct import pack_into

class CrunchDecodeError(ValueError):
    pass

@dataclass(slots=True)
class CrnPalette:
    offset: int
    size: int
    count: int

@dataclass(slots=True)
class CrnHeader:
    header_size: int
    data_size: int
    width: int
    height: int
    levels: int
    faces: int
    crn_format: int
    color_endpoints: CrnPalette
    color_selectors: CrnPalette
    alpha_endpoints: CrnPalette
    alpha_selectors: CrnPalette
    tables_size: int
    tables_offset: int
    level_offsets: list[int]

C_CRN_SIG = 0x4878
C_CRN_HEADER_MIN_SIZE = 62
C_CRN_FMT_DXT1 = 0
C_CRN_FMT_DXT5 = 2
C_MAX_SUPPORTED_SYMS = 8192
C_MAX_EXPECTED_CODE_SIZE = 16
C_MAX_TABLE_BITS = 11
C_UINT32_MAX = 0xFFFFFFFF
U32_MASK = 0xFFFFFFFF

DXT1_FROM_LINEAR = (0, 2, 3, 1)
DXT5_FROM_LINEAR = (0, 2, 3, 4, 5, 6, 7, 1)
CHUNK_ENCODING_TILES = (
    (0, 0, 0, 0),
    (0, 0, 1, 1),
    (0, 1, 0, 1),
    (0, 0, 1, 2),
    (1, 2, 0, 0),
    (0, 1, 0, 2),
    (1, 0, 2, 0),
    (0, 1, 2, 3),
)
CHUNK_ENCODING_TILE_COUNTS = (1, 2, 2, 3, 3, 3, 3, 4)
MOST_PROBABLE_CODELENGTH_CODES = (
    17,
    18,
    19,
    20,
    0,
    8,
    7,
    9,
    6,
    10,
    5,
    11,
    4,
    12,
    3,
    13,
    2,
    14,
    1,
    15,
    16,
)

def decode_crn_to_dxt(
    data: bytes,
    *,
    level_index: int = 0,
    expected_unity_format: int | None = None,
) -> tuple[bytes, CrnHeader]:
    header = parse_header(data)
    if level_index < 0 or level_index >= header.levels:
        raise CrunchDecodeError(f"CRN mip level {level_index} isnt available")
    if header.faces != 1:
        raise CrunchDecodeError(f"CRN cubemaps arent supported yet (faces={header.faces})")
    if header.crn_format not in (C_CRN_FMT_DXT1, C_CRN_FMT_DXT5):
        raise CrunchDecodeError(f"unsupported CRN internal format {header.crn_format}")
    if expected_unity_format == 28 and header.crn_format != C_CRN_FMT_DXT1:
        raise CrunchDecodeError("DXT1Crunched texture contains non-DXT1 CRN data")
    if expected_unity_format == 29 and header.crn_format != C_CRN_FMT_DXT5:
        raise CrunchDecodeError("DXT5Crunched texture contains non-DXT5 CRN data")

    unpacker = CrnUnpacker(data, header)
    return unpacker.unpack_level(level_index), header

def parse_header(data: bytes) -> CrnHeader:
    if len(data) < C_CRN_HEADER_MIN_SIZE:
        raise CrunchDecodeError("CRN data is too small for a header")
    sig = be(data, 0, 2)
    if sig != C_CRN_SIG:
        raise CrunchDecodeError("CRN signature is missing")

    header_size = be(data, 2, 2)
    data_size = be(data, 6, 4)
    if header_size < C_CRN_HEADER_MIN_SIZE or header_size > len(data):
        raise CrunchDecodeError(f"invalid CRN header size {header_size}")
    if data_size > len(data):
        raise CrunchDecodeError(f"CRN header references {data_size} bytes, got {len(data)}")

    width = be(data, 12, 2)
    height = be(data, 14, 2)
    levels = data[16]
    faces = data[17]
    crn_format = data[18]
    if width <= 0 or height <= 0 or levels <= 0:
        raise CrunchDecodeError("CRN texture dimensions or mip count are invalid")

    pos = 33
    color_endpoints, pos = read_palette(data, pos)
    color_selectors, pos = read_palette(data, pos)
    alpha_endpoints, pos = read_palette(data, pos)
    alpha_selectors, pos = read_palette(data, pos)
    tables_size = be(data, pos, 2)
    tables_offset = be(data, pos + 2, 3)
    pos += 5
    if pos + levels * 4 > header_size:
        raise CrunchDecodeError("CRN header doesnt contain all mip offsets")
    level_offsets = [be(data, pos + i * 4, 4) for i in range(levels)]

    for label, offset, size in (
        ("tables", tables_offset, tables_size),
        ("color endpoint palette", color_endpoints.offset, color_endpoints.size),
        ("color selector palette", color_selectors.offset, color_selectors.size),
        ("alpha endpoint palette", alpha_endpoints.offset, alpha_endpoints.size),
        ("alpha selector palette", alpha_selectors.offset, alpha_selectors.size),
    ):
        if size and (offset < header_size or offset + size > data_size):
            raise CrunchDecodeError(f"invalid CRN {label} range")
    for offset in level_offsets:
        if offset < header_size or offset > data_size:
            raise CrunchDecodeError("invalid CRN mip offset")

    return CrnHeader(
        header_size=header_size,
        data_size=data_size,
        width=width,
        height=height,
        levels=levels,
        faces=faces,
        crn_format=crn_format,
        color_endpoints=color_endpoints,
        color_selectors=color_selectors,
        alpha_endpoints=alpha_endpoints,
        alpha_selectors=alpha_selectors,
        tables_size=tables_size,
        tables_offset=tables_offset,
        level_offsets=level_offsets,
    )

def read_palette(data: bytes, pos: int) -> tuple[CrnPalette, int]:
    return CrnPalette(be(data, pos, 3), be(data, pos + 3, 3), be(data, pos + 6, 2)), pos + 8

def be(data: bytes, pos: int, size: int) -> int:
    if pos < 0 or pos + size > len(data):
        raise CrunchDecodeError("CRN header read ran past the end")
    return int.from_bytes(data[pos : pos + size], "big")

def total_bits(value: int) -> int:
    bits = 0
    while value > 0:
        value >>= 1
        bits += 1
    return bits

def ceil_log2(value: int) -> int:
    if value <= 1:
        return 0
    return (value - 1).bit_length()

def next_pow2(value: int) -> int:
    if value <= 1:
        return 1
    return 1 << (value - 1).bit_length()

def limit_once(value: int, count: int) -> int:
    if count <= 0:
        raise CrunchDecodeError("CRN stream referenced an empty palette")
    return value - count if value >= count else value

class DecoderTables:
    __slots__ = (
        "num_syms",
        "total_used_syms",
        "table_bits",
        "table_max_code",
        "decode_start_code_size",
        "max_codes",
        "val_ptrs",
        "lookup",
        "sorted_symbol_order",
        "min_code_size",
        "max_code_size",
    )

    def __init__(self, num_syms: int, code_sizes: list[int], table_bits: int):
        if num_syms <= 0 or table_bits > C_MAX_TABLE_BITS:
            raise CrunchDecodeError("invalid Huffman table size")

        self.num_syms = num_syms
        self.table_bits = table_bits
        self.max_codes = [0] * (C_MAX_EXPECTED_CODE_SIZE + 1)
        self.val_ptrs = [0] * (C_MAX_EXPECTED_CODE_SIZE + 1)
        self.lookup: list[int] = []
        self.sorted_symbol_order: list[int] = []

        min_codes = [0] * C_MAX_EXPECTED_CODE_SIZE
        num_codes = [0] * (C_MAX_EXPECTED_CODE_SIZE + 1)
        for size in code_sizes:
            if size < 0 or size > C_MAX_EXPECTED_CODE_SIZE:
                raise CrunchDecodeError("invalid Huffman code size")
            if size:
                num_codes[size] += 1

        sorted_positions = [0] * (C_MAX_EXPECTED_CODE_SIZE + 1)
        cur_code = 0
        total_used = 0
        max_code_size = 0
        min_code_size = C_UINT32_MAX
        for size in range(1, C_MAX_EXPECTED_CODE_SIZE + 1):
            count = num_codes[size]
            if not count:
                self.max_codes[size - 1] = 0
            else:
                min_code_size = min(min_code_size, size)
                max_code_size = max(max_code_size, size)
                min_codes[size - 1] = cur_code
                max_code = cur_code + count - 1
                self.max_codes[size - 1] = 1 + ((max_code << (16 - size)) | ((1 << (16 - size)) - 1))
                self.val_ptrs[size - 1] = total_used
                sorted_positions[size] = total_used
                cur_code += count
                total_used += count
            cur_code <<= 1

        if not total_used:
            raise CrunchDecodeError("empty Huffman model")

        self.total_used_syms = total_used
        self.sorted_symbol_order = [0] * min(num_syms, next_pow2(total_used))
        if len(self.sorted_symbol_order) < total_used:
            self.sorted_symbol_order = [0] * total_used

        self.min_code_size = min_code_size
        self.max_code_size = max_code_size
        for sym, size in enumerate(code_sizes):
            if size:
                sorted_pos = sorted_positions[size]
                if sorted_pos >= len(self.sorted_symbol_order):
                    raise CrunchDecodeError("Huffman symbol order overflow")
                self.sorted_symbol_order[sorted_pos] = sym
                sorted_positions[size] += 1

        if table_bits <= min_code_size:
            table_bits = 0
        self.table_bits = table_bits

        if table_bits:
            table_size = 1 << table_bits
            self.lookup = [C_UINT32_MAX] * table_size
            for code_size in range(1, table_bits + 1):
                if not num_codes[code_size]:
                    continue
                fill_size = table_bits - code_size
                fill_count = 1 << fill_size
                min_code = min_codes[code_size - 1]
                max_code = self.get_unshifted_max_code(code_size)
                val_ptr = self.val_ptrs[code_size - 1]
                for code in range(min_code, max_code + 1):
                    sym_index = self.sorted_symbol_order[val_ptr + code - min_code]
                    for fill in range(fill_count):
                        self.lookup[fill + (code << fill_size)] = sym_index | (code_size << 16)

        for index in range(C_MAX_EXPECTED_CODE_SIZE):
            self.val_ptrs[index] -= min_codes[index]

        self.table_max_code = 0
        self.decode_start_code_size = min_code_size
        if table_bits:
            for size in range(table_bits, 0, -1):
                if num_codes[size]:
                    self.table_max_code = self.max_codes[size - 1]
                    break
            if self.table_max_code:
                self.decode_start_code_size = table_bits + 1
                for size in range(table_bits + 1, max_code_size + 1):
                    if num_codes[size]:
                        self.decode_start_code_size = size
                        break

        self.max_codes[C_MAX_EXPECTED_CODE_SIZE] = C_UINT32_MAX
        self.val_ptrs[C_MAX_EXPECTED_CODE_SIZE] = 0xFFFFF

    def get_unshifted_max_code(self, length: int) -> int:
        value = self.max_codes[length - 1]
        if not value:
            return C_UINT32_MAX
        return (value - 1) >> (16 - length)

class StaticHuffmanDataModel:
    __slots__ = ("total_syms", "code_sizes", "decode_tables")

    def __init__(self):
        self.total_syms = 0
        self.code_sizes: list[int] = []
        self.decode_tables: DecoderTables | None = None

    def clear(self) -> None:
        self.total_syms = 0
        self.code_sizes = []
        self.decode_tables = None

    def prepare_decoder_tables(self) -> None:
        total_syms = len(self.code_sizes)
        if total_syms < 1 or total_syms > C_MAX_SUPPORTED_SYMS:
            raise CrunchDecodeError("invalid Huffman model symbol count")
        self.total_syms = total_syms
        table_bits = 0
        if total_syms > 16:
            table_bits = min(1 + ceil_log2(total_syms), C_MAX_TABLE_BITS)
        self.decode_tables = DecoderTables(total_syms, self.code_sizes, table_bits)

class SymbolCodec:
    __slots__ = ("data", "pos", "end", "bit_buf", "bit_count")

    def __init__(self):
        self.data = b""
        self.pos = 0
        self.end = 0
        self.bit_buf = 0
        self.bit_count = 0

    def start_decoding(self, data: bytes, offset: int, size: int) -> None:
        if size <= 0 or offset < 0 or offset + size > len(data):
            raise CrunchDecodeError("invalid CRN bitstream range")
        self.data = data
        self.pos = offset
        self.end = offset + size
        self.bit_buf = 0
        self.bit_count = 0

    def stop_decoding(self) -> int:
        return self.pos

    def decode_bits(self, num_bits: int) -> int:
        if num_bits <= 0:
            return 0
        if num_bits > 16:
            return (self.get_bits(num_bits - 16) << 16) | self.get_bits(16)
        return self.get_bits(num_bits)

    def get_bits(self, num_bits: int) -> int:
        while self.bit_count < num_bits:
            value = self.data[self.pos] if self.pos < self.end else 0
            if self.pos < self.end:
                self.pos += 1
            self.bit_count += 8
            self.bit_buf = (self.bit_buf | (value << (32 - self.bit_count))) & U32_MASK

        result = self.bit_buf >> (32 - num_bits)
        self.bit_buf = (self.bit_buf << num_bits) & U32_MASK
        self.bit_count -= num_bits
        return result

    def decode_receive_static_data_model(self, model: StaticHuffmanDataModel) -> None:
        total_used_syms = self.decode_bits(total_bits(C_MAX_SUPPORTED_SYMS))
        if total_used_syms == 0:
            model.clear()
            return
        if total_used_syms > C_MAX_SUPPORTED_SYMS:
            raise CrunchDecodeError("Huffman model has too many symbols")

        code_sizes = [0] * total_used_syms
        sent_code_count = self.decode_bits(5)
        if sent_code_count < 1 or sent_code_count > len(MOST_PROBABLE_CODELENGTH_CODES):
            raise CrunchDecodeError("invalid CRN code-length model")

        code_length_model = StaticHuffmanDataModel()
        code_length_model.code_sizes = [0] * 21
        for index in range(sent_code_count):
            code_length_model.code_sizes[MOST_PROBABLE_CODELENGTH_CODES[index]] = self.decode_bits(3)
        code_length_model.prepare_decoder_tables()

        offset = 0
        while offset < total_used_syms:
            remaining = total_used_syms - offset
            code = self.decode(code_length_model)
            if code <= 16:
                code_sizes[offset] = code
                offset += 1
            elif code == 17:
                run = self.decode_bits(3) + 3
                if run > remaining:
                    raise CrunchDecodeError("CRN small zero run exceeds model")
                offset += run
            elif code == 18:
                run = self.decode_bits(7) + 11
                if run > remaining:
                    raise CrunchDecodeError("CRN large zero run exceeds model")
                offset += run
            elif code in (19, 20):
                run = self.decode_bits(2 if code == 19 else 6) + (3 if code == 19 else 7)
                if offset == 0 or run > remaining:
                    raise CrunchDecodeError("CRN repeat run exceeds model")
                previous = code_sizes[offset - 1]
                if previous == 0:
                    raise CrunchDecodeError("CRN repeat run follows a zero code length")
                for _ in range(run):
                    code_sizes[offset] = previous
                    offset += 1
            else:
                raise CrunchDecodeError("invalid CRN code-length symbol")

        model.code_sizes = code_sizes
        model.prepare_decoder_tables()

    def decode(self, model: StaticHuffmanDataModel) -> int:
        tables = model.decode_tables
        if tables is None:
            raise CrunchDecodeError("attempted to decode with an empty Huffman model")

        if self.bit_count < 24:
            if self.bit_count < 16:
                c0 = self.data[self.pos] if self.pos < self.end else 0
                if self.pos < self.end:
                    self.pos += 1
                c1 = self.data[self.pos] if self.pos < self.end else 0
                if self.pos < self.end:
                    self.pos += 1
                self.bit_count += 16
                value = (c0 << 8) | c1
                self.bit_buf = (self.bit_buf | (value << (32 - self.bit_count))) & U32_MASK
            else:
                value = self.data[self.pos] if self.pos < self.end else 0
                if self.pos < self.end:
                    self.pos += 1
                self.bit_count += 8
                self.bit_buf = (self.bit_buf | (value << (32 - self.bit_count))) & U32_MASK

        key = (self.bit_buf >> 16) + 1
        if key <= tables.table_max_code:
            lookup_index = self.bit_buf >> (32 - tables.table_bits)
            entry = tables.lookup[lookup_index]
            if entry == C_UINT32_MAX:
                raise CrunchDecodeError("invalid CRN Huffman lookup")
            symbol = entry & 0xFFFF
            length = entry >> 16
        else:
            length = tables.decode_start_code_size
            while key > tables.max_codes[length - 1]:
                length += 1
                if length > C_MAX_EXPECTED_CODE_SIZE:
                    raise CrunchDecodeError("invalid CRN Huffman code")
            val_ptr = tables.val_ptrs[length - 1] + (self.bit_buf >> (32 - length))
            if val_ptr < 0 or val_ptr >= len(tables.sorted_symbol_order):
                raise CrunchDecodeError("invalid CRN Huffman symbol index")
            symbol = tables.sorted_symbol_order[val_ptr]

        self.bit_buf = (self.bit_buf << length) & U32_MASK
        self.bit_count -= length
        return symbol

class CrnUnpacker:
    __slots__ = (
        "data",
        "header",
        "codec",
        "chunk_encoding_dm",
        "endpoint_delta_dm",
        "selector_delta_dm",
        "color_endpoints",
        "color_selectors",
        "alpha_endpoints",
        "alpha_selectors",
    )

    def __init__(self, data: bytes, header: CrnHeader):
        self.data = data
        self.header = header
        self.codec = SymbolCodec()
        self.chunk_encoding_dm = StaticHuffmanDataModel()
        self.endpoint_delta_dm = [StaticHuffmanDataModel(), StaticHuffmanDataModel()]
        self.selector_delta_dm = [StaticHuffmanDataModel(), StaticHuffmanDataModel()]
        self.color_endpoints: list[int] = []
        self.color_selectors: list[int] = []
        self.alpha_endpoints: list[int] = []
        self.alpha_selectors: list[int] = []
        self.init_tables()
        self.decode_palettes()

    def unpack_level(self, level_index: int) -> bytes:
        width = max(self.header.width >> level_index, 1)
        height = max(self.header.height >> level_index, 1)
        blocks_x = (width + 3) >> 2
        blocks_y = (height + 3) >> 2
        block_size = 8 if self.header.crn_format == C_CRN_FMT_DXT1 else 16
        row_pitch = block_size * blocks_x
        output = bytearray(row_pitch * blocks_y)

        current_offset = self.header.level_offsets[level_index]
        next_offset = self.header.data_size
        if level_index + 1 < self.header.levels:
            next_offset = self.header.level_offsets[level_index + 1]
        if next_offset <= current_offset:
            raise CrunchDecodeError("invalid CRN mip data range")

        self.codec.start_decoding(self.data, current_offset, next_offset - current_offset)
        chunks_x = (blocks_x + 1) >> 1
        chunks_y = (blocks_y + 1) >> 1
        if self.header.crn_format == C_CRN_FMT_DXT1:
            self.unpack_dxt1(output, blocks_x, blocks_y, chunks_x, chunks_y)
        elif self.header.crn_format == C_CRN_FMT_DXT5:
            self.unpack_dxt5(output, blocks_x, blocks_y, chunks_x, chunks_y)
        else:
            raise CrunchDecodeError(f"unsupported CRN internal format {self.header.crn_format}")
        self.codec.stop_decoding()
        return bytes(output)

    def init_tables(self) -> None:
        h = self.header
        self.codec.start_decoding(self.data, h.tables_offset, h.tables_size)
        self.codec.decode_receive_static_data_model(self.chunk_encoding_dm)
        if not h.color_endpoints.count and not h.alpha_endpoints.count:
            raise CrunchDecodeError("CRN has no endpoint palettes")
        if h.color_endpoints.count:
            self.codec.decode_receive_static_data_model(self.endpoint_delta_dm[0])
            self.codec.decode_receive_static_data_model(self.selector_delta_dm[0])
        if h.alpha_endpoints.count:
            self.codec.decode_receive_static_data_model(self.endpoint_delta_dm[1])
            self.codec.decode_receive_static_data_model(self.selector_delta_dm[1])
        self.codec.stop_decoding()

    def decode_palettes(self) -> None:
        if self.header.color_endpoints.count:
            self.decode_color_endpoints()
            self.decode_color_selectors()
        if self.header.alpha_endpoints.count:
            self.decode_alpha_endpoints()
            self.decode_alpha_selectors()

    def decode_color_endpoints(self) -> None:
        palette = self.header.color_endpoints
        self.codec.start_decoding(self.data, palette.offset, palette.size)
        dm = [StaticHuffmanDataModel(), StaticHuffmanDataModel()]
        self.codec.decode_receive_static_data_model(dm[0])
        self.codec.decode_receive_static_data_model(dm[1])

        a = b = c = d = e = f = 0
        out: list[int] = []
        for _ in range(palette.count):
            a = (a + self.codec.decode(dm[0])) & 31
            b = (b + self.codec.decode(dm[1])) & 63
            c = (c + self.codec.decode(dm[0])) & 31
            d = (d + self.codec.decode(dm[0])) & 31
            e = (e + self.codec.decode(dm[1])) & 63
            f = (f + self.codec.decode(dm[0])) & 31
            out.append(c | (b << 5) | (a << 11) | (f << 16) | (e << 21) | (d << 27))
        self.codec.stop_decoding()
        self.color_endpoints = out

    def decode_color_selectors(self) -> None:
        palette = self.header.color_selectors
        self.codec.start_decoding(self.data, palette.offset, palette.size)
        dm = StaticHuffmanDataModel()
        self.codec.decode_receive_static_data_model(dm)
        delta0, delta1 = selector_delta_tables(3)
        current = [0] * 16
        out: list[int] = []
        for _ in range(palette.count):
            for index in range(8):
                symbol = self.codec.decode(dm)
                current[index * 2] = (delta0[symbol] + current[index * 2]) & 3
                current[index * 2 + 1] = (delta1[symbol] + current[index * 2 + 1]) & 3
            word = 0
            for index, value in enumerate(current):
                word |= DXT1_FROM_LINEAR[value] << (2 * index)
            out.append(word)
        self.codec.stop_decoding()
        self.color_selectors = out

    def decode_alpha_endpoints(self) -> None:
        palette = self.header.alpha_endpoints
        self.codec.start_decoding(self.data, palette.offset, palette.size)
        dm = StaticHuffmanDataModel()
        self.codec.decode_receive_static_data_model(dm)
        a = b = 0
        out: list[int] = []
        for _ in range(palette.count):
            a = (a + self.codec.decode(dm)) & 255
            b = (b + self.codec.decode(dm)) & 255
            out.append(a | (b << 8))
        self.codec.stop_decoding()
        self.alpha_endpoints = out

    def decode_alpha_selectors(self) -> None:
        palette = self.header.alpha_selectors
        self.codec.start_decoding(self.data, palette.offset, palette.size)
        dm = StaticHuffmanDataModel()
        self.codec.decode_receive_static_data_model(dm)
        delta0, delta1 = selector_delta_tables(7)
        current = [0] * 16
        out: list[int] = []
        for _ in range(palette.count):
            for index in range(8):
                symbol = self.codec.decode(dm)
                current[index * 2] = (delta0[symbol] + current[index * 2]) & 7
                current[index * 2 + 1] = (delta1[symbol] + current[index * 2 + 1]) & 7

            selectors = [DXT5_FROM_LINEAR[value] for value in current]
            out.append(
                (
                    selectors[0]
                    | (selectors[1] << 3)
                    | (selectors[2] << 6)
                    | (selectors[3] << 9)
                    | (selectors[4] << 12)
                    | (selectors[5] << 15)
                )
                & 0xFFFF
            )
            out.append(
                (
                    (selectors[5] >> 1)
                    | (selectors[6] << 2)
                    | (selectors[7] << 5)
                    | (selectors[8] << 8)
                    | (selectors[9] << 11)
                    | (selectors[10] << 14)
                )
                & 0xFFFF
            )
            out.append(
                (
                    (selectors[10] >> 2)
                    | (selectors[11] << 1)
                    | (selectors[12] << 4)
                    | (selectors[13] << 7)
                    | (selectors[14] << 10)
                    | (selectors[15] << 13)
                )
                & 0xFFFF
            )
        self.codec.stop_decoding()
        self.alpha_selectors = out

    def unpack_dxt1(self, output: bytearray, blocks_x: int, blocks_y: int, chunks_x: int, chunks_y: int) -> None:
        chunk_encoding_bits = 1
        prev_endpoint = 0
        prev_selector = 0
        for chunk_y in range(chunks_y):
            x_range = range(chunks_x - 1, -1, -1) if chunk_y & 1 else range(chunks_x)
            skip_bottom = chunk_y == chunks_y - 1 and bool(blocks_y & 1)
            for chunk_x in x_range:
                if chunk_encoding_bits == 1:
                    chunk_encoding_bits = self.codec.decode(self.chunk_encoding_dm) | 512
                encoding_index = chunk_encoding_bits & 7
                chunk_encoding_bits >>= 3
                tile_count = CHUNK_ENCODING_TILE_COUNTS[encoding_index]
                color_endpoints = [0] * tile_count
                for index in range(tile_count):
                    prev_endpoint = limit_once(prev_endpoint + self.codec.decode(self.endpoint_delta_dm[0]), len(self.color_endpoints))
                    color_endpoints[index] = self.color_endpoints[prev_endpoint]

                tile_indices = CHUNK_ENCODING_TILES[encoding_index]
                skip_right = bool(blocks_x & 1) and chunk_x == chunks_x - 1
                for by in range(2):
                    for bx in range(2):
                        prev_selector = limit_once(prev_selector + self.codec.decode(self.selector_delta_dm[0]), len(self.color_selectors))
                        if (bx and skip_right) or (by and skip_bottom):
                            continue
                        tile_index = tile_indices[bx + by * 2]
                        block_offset = ((chunk_y * 2 + by) * blocks_x + (chunk_x * 2 + bx)) * 8
                        pack_into("<I", output, block_offset, color_endpoints[tile_index])
                        pack_into("<I", output, block_offset + 4, self.color_selectors[prev_selector])

    def unpack_dxt5(self, output: bytearray, blocks_x: int, blocks_y: int, chunks_x: int, chunks_y: int) -> None:
        chunk_encoding_bits = 1
        prev_color_endpoint = 0
        prev_color_selector = 0
        prev_alpha_endpoint = 0
        prev_alpha_selector = 0
        for chunk_y in range(chunks_y):
            x_range = range(chunks_x - 1, -1, -1) if chunk_y & 1 else range(chunks_x)
            skip_bottom = chunk_y == chunks_y - 1 and bool(blocks_y & 1)
            for chunk_x in x_range:
                if chunk_encoding_bits == 1:
                    chunk_encoding_bits = self.codec.decode(self.chunk_encoding_dm) | 512
                encoding_index = chunk_encoding_bits & 7
                chunk_encoding_bits >>= 3
                tile_count = CHUNK_ENCODING_TILE_COUNTS[encoding_index]
                tile_indices = CHUNK_ENCODING_TILES[encoding_index]
                skip_right = bool(blocks_x & 1) and chunk_x == chunks_x - 1

                alpha_endpoints = [0] * tile_count
                color_endpoints = [0] * tile_count
                for index in range(tile_count):
                    prev_alpha_endpoint = limit_once(
                        prev_alpha_endpoint + self.codec.decode(self.endpoint_delta_dm[1]),
                        len(self.alpha_endpoints),
                    )
                    alpha_endpoints[index] = self.alpha_endpoints[prev_alpha_endpoint]
                for index in range(tile_count):
                    prev_color_endpoint = limit_once(
                        prev_color_endpoint + self.codec.decode(self.endpoint_delta_dm[0]),
                        len(self.color_endpoints),
                    )
                    color_endpoints[index] = self.color_endpoints[prev_color_endpoint]

                for by in range(2):
                    for bx in range(2):
                        prev_alpha_selector = limit_once(
                            prev_alpha_selector + self.codec.decode(self.selector_delta_dm[1]),
                            self.header.alpha_selectors.count,
                        )
                        prev_color_selector = limit_once(
                            prev_color_selector + self.codec.decode(self.selector_delta_dm[0]),
                            len(self.color_selectors),
                        )
                        if (bx and skip_right) or (by and skip_bottom):
                            continue
                        tile_index = tile_indices[bx + by * 2]
                        selector_base = prev_alpha_selector * 3
                        if selector_base + 2 >= len(self.alpha_selectors):
                            raise CrunchDecodeError("CRN alpha selector index is out of range")
                        block_offset = ((chunk_y * 2 + by) * blocks_x + (chunk_x * 2 + bx)) * 16
                        pack_into("<I", output, block_offset, alpha_endpoints[tile_index] | (self.alpha_selectors[selector_base] << 16))
                        pack_into("<I", output, block_offset + 4, self.alpha_selectors[selector_base + 1] | (self.alpha_selectors[selector_base + 2] << 16))
                        pack_into("<I", output, block_offset + 8, color_endpoints[tile_index])
                        pack_into("<I", output, block_offset + 12, self.color_selectors[prev_color_selector])

def selector_delta_tables(max_selector_value: int) -> tuple[list[int], list[int]]:
    unique = max_selector_value * 2 + 1
    delta0: list[int] = []
    delta1: list[int] = []
    left = -max_selector_value
    right = -max_selector_value
    for _ in range(unique * unique):
        delta0.append(left)
        delta1.append(right)
        left += 1
        if left > max_selector_value:
            left = -max_selector_value
            right += 1
    return delta0, delta1
