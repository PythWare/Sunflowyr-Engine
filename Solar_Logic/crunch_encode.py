from __future__ import annotations
import struct
from collections import Counter
from dataclasses import dataclass

class CrunchEncodeError(ValueError):
    pass

C_MAX_SUPPORTED_SYMS = 8192
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
DXT1_TO_LINEAR = (0, 3, 1, 2)
DXT5_TO_LINEAR = (0, 7, 1, 2, 3, 4, 5, 6)

@dataclass(slots=True)
class HuffmanModel:
    code_sizes: list[int]
    codes: list[int]

class BitWriter:
    def __init__(self) -> None:
        self.data = bytearray()
        self.bit_buffer = 0
        self.bit_count = 0

    def write_bits(self, value: int, count: int) -> None:
        if count <= 0:
            return
        if value < 0 or value >= (1 << count):
            raise CrunchEncodeError(f"value {value} doesnt fit in {count} bits")
        self.bit_buffer = (self.bit_buffer << count) | value
        self.bit_count += count
        while self.bit_count >= 8:
            shift = self.bit_count - 8
            self.data.append((self.bit_buffer >> shift) & 0xFF)
            self.bit_buffer &= (1 << shift) - 1 if shift else 0
            self.bit_count = shift

    def write_symbol(self, symbol: int, model: HuffmanModel) -> None:
        if symbol < 0 or symbol >= len(model.code_sizes):
            raise CrunchEncodeError(f"Huffman symbol {symbol} is outside its model")
        size = model.code_sizes[symbol]
        if size <= 0:
            raise CrunchEncodeError(f"Huffman symbol {symbol} has no code")
        self.write_bits(model.codes[symbol], size)

    def finish(self) -> bytes:
        if self.bit_count:
            self.data.append((self.bit_buffer << (8 - self.bit_count)) & 0xFF)
            self.bit_buffer = 0
            self.bit_count = 0
        return bytes(self.data)

def encode_crn_from_dxt(
    dxt_levels: list[bytes],
    width: int,
    height: int,
    unity_texture_format: int,
) -> bytes:
    if unity_texture_format == 28:
        crn_format = 0
        block_size = 8
    elif unity_texture_format == 29:
        crn_format = 2
        block_size = 16
    else:
        raise CrunchEncodeError(
            f"Crunch writer expected Unity format 28 or 29, found {unity_texture_format}"
        )
    if not dxt_levels or len(dxt_levels) > 16:
        raise CrunchEncodeError(f"CRN holds 1 to 16 mip levels, found {len(dxt_levels)}")
    level_shapes: list[tuple[int, int]] = []
    level_blocks: list[list[bytearray]] = []
    for level, dxt_data in enumerate(dxt_levels):
        blocks_x = (max(1, width >> level) + 3) // 4
        blocks_y = (max(1, height >> level) + 3) // 4
        expected_size = blocks_x * blocks_y * block_size
        if len(dxt_data) != expected_size:
            raise CrunchEncodeError(
                f"mip {level} DXT data size {len(dxt_data)} doesnt match expected {expected_size}"
            )
        level_shapes.append((blocks_x, blocks_y))
        level_blocks.append(
            [
                bytearray(dxt_data[offset : offset + block_size])
                for offset in range(0, len(dxt_data), block_size)
            ]
        )

    blocks = [block for level in level_blocks for block in level]
    if block_size == 16:
        limit_alpha_codebooks(blocks)
    color_endpoints = unique_values(
        struct.unpack_from("<I", block, 0 if block_size == 8 else 8)[0]
        for block in blocks
    )
    color_selectors = unique_values(
        struct.unpack_from("<I", block, 4 if block_size == 8 else 12)[0]
        for block in blocks
    )
    alpha_endpoints: list[int] = []
    alpha_selectors: list[int] = []
    if block_size == 16:
        alpha_endpoints = unique_values(
            struct.unpack_from("<H", block, 0)[0] for block in blocks
        )
        alpha_selectors = unique_values(
            int.from_bytes(block[2:8], "little") for block in blocks
        )

    validate_palette_size("color endpoints", color_endpoints)
    validate_palette_size("color selectors", color_selectors)
    if alpha_endpoints:
        validate_palette_size("alpha endpoints", alpha_endpoints)
        validate_palette_size("alpha selectors", alpha_selectors)

    color_endpoint_stream = encode_color_endpoint_palette(color_endpoints)
    color_selector_stream = encode_selector_palette(
        color_selectors,
        selector_bits=2,
    )
    alpha_endpoint_stream = (
        encode_alpha_endpoint_palette(alpha_endpoints) if alpha_endpoints else b""
    )
    alpha_selector_stream = (
        encode_selector_palette(alpha_selectors, selector_bits=3)
        if alpha_selectors
        else b""
    )

    color_endpoint_indexes = {value: index for index, value in enumerate(color_endpoints)}
    color_selector_indexes = {value: index for index, value in enumerate(color_selectors)}
    alpha_endpoint_indexes = {value: index for index, value in enumerate(alpha_endpoints)}
    alpha_selector_indexes = {value: index for index, value in enumerate(alpha_selectors)}

    level_events = [
        build_chunk_events(
            level,
            blocks_x,
            blocks_y,
            block_size,
            color_endpoint_indexes,
            color_selector_indexes,
            alpha_endpoint_indexes,
            alpha_selector_indexes,
        )
        for level, (blocks_x, blocks_y) in zip(level_blocks, level_shapes)
    ]
    models = build_event_models(
        [event for events in level_events for event in events],
        len(color_endpoints),
        len(color_selectors),
        len(alpha_endpoints),
        len(alpha_selectors),
    )
    table_writer = BitWriter()
    transmit_model(table_writer, models["chunk"])
    transmit_model(table_writer, models["color_endpoint"])
    transmit_model(table_writer, models["color_selector"])
    if block_size == 16:
        transmit_model(table_writer, models["alpha_endpoint"])
        transmit_model(table_writer, models["alpha_selector"])
    table_data = table_writer.finish()

    level_data = []
    for events in level_events:
        level_writer = BitWriter()
        for model_name, symbol in events:
            level_writer.write_symbol(symbol, models[model_name])
        level_data.append(level_writer.finish())

    header_size = 70 + 4 * len(level_data)
    sections = [
        color_endpoint_stream,
        color_selector_stream,
        alpha_endpoint_stream,
        alpha_selector_stream,
        table_data,
        *level_data,
    ]
    offsets = []
    position = header_size
    for section in sections:
        offsets.append(position if section else 0)
        position += len(section)

    header = bytearray(header_size)
    write_be(header, 0, 2, 0x4878)
    write_be(header, 2, 2, header_size)
    write_be(header, 6, 4, position)
    write_be(header, 12, 2, width)
    write_be(header, 14, 2, height)
    header[16] = len(level_data)
    header[17] = 1
    header[18] = crn_format
    write_palette(
        header,
        33,
        offsets[0],
        len(color_endpoint_stream),
        len(color_endpoints),
    )
    write_palette(
        header,
        41,
        offsets[1],
        len(color_selector_stream),
        len(color_selectors),
    )
    write_palette(
        header,
        49,
        offsets[2],
        len(alpha_endpoint_stream),
        len(alpha_endpoints),
    )
    write_palette(
        header,
        57,
        offsets[3],
        len(alpha_selector_stream),
        len(alpha_selectors),
    )
    write_be(header, 65, 2, len(table_data))
    write_be(header, 67, 3, offsets[4])
    for level, offset in enumerate(offsets[5:]):
        write_be(header, 70 + 4 * level, 4, offset)

    body = b"".join(sections)
    write_be(header, 10, 2, crc16(body))
    write_be(header, 4, 2, crc16(bytes(header[6:])))
    return bytes(header) + body

def limit_alpha_codebooks(blocks: list[bytearray]) -> None:
    endpoint_values = [struct.unpack_from("<H", block, 0)[0] for block in blocks]
    if len(set(endpoint_values)) > C_MAX_SUPPORTED_SYMS:
        for block in blocks:
            block[0] &= 0xFC
            block[1] &= 0xFC

    selector_values = [int.from_bytes(block[2:8], "little") for block in blocks]
    selector_counts = Counter(selector_values)
    if len(selector_counts) <= C_MAX_SUPPORTED_SYMS:
        return
    kept = [
        value
        for value, count in sorted(
            selector_counts.items(),
            key=lambda item: (-item[1], item[0]),
        )[:C_MAX_SUPPORTED_SYMS]
    ]
    kept_set = set(kept)
    replacements = {}
    for value in selector_counts:
        if value in kept_set:
            continue
        replacements[value] = min(
            kept,
            key=lambda candidate: ((value ^ candidate).bit_count(), candidate),
        )
    for block in blocks:
        value = int.from_bytes(block[2:8], "little")
        replacement = replacements.get(value)
        if replacement is not None:
            block[2:8] = replacement.to_bytes(6, "little")

def unique_values(values) -> list[int]:
    seen = set()
    output = []
    for value in values:
        if value not in seen:
            seen.add(value)
            output.append(value)
    return output

def validate_palette_size(label: str, values: list[int]) -> None:
    if not values:
        raise CrunchEncodeError(f"CRN {label} palette is empty")
    if len(values) > C_MAX_SUPPORTED_SYMS:
        raise CrunchEncodeError(
            f"CRN {label} palette has {len(values)} entries; maximum is "
            f"{C_MAX_SUPPORTED_SYMS}"
        )

def encode_color_endpoint_palette(values: list[int]) -> bytes:
    symbols0 = []
    symbols1 = []
    previous = [0, 0, 0, 0, 0, 0]
    for value in values:
        low = value & 0xFFFF
        high = (value >> 16) & 0xFFFF
        current = [
            (low >> 11) & 31,
            (low >> 5) & 63,
            low & 31,
            (high >> 11) & 31,
            (high >> 5) & 63,
            high & 31,
        ]
        for index, component in enumerate(current):
            limit = 64 if index in (1, 4) else 32
            symbol = (component - previous[index]) % limit
            if index in (1, 4):
                symbols1.append(symbol)
            else:
                symbols0.append(symbol)
            previous[index] = component

    model0 = balanced_model(32, symbols0)
    model1 = balanced_model(64, symbols1)
    writer = BitWriter()
    transmit_model(writer, model0)
    transmit_model(writer, model1)
    index0 = 0
    index1 = 0
    for value_index in range(len(values)):
        for component_index in range(6):
            if component_index in (1, 4):
                writer.write_symbol(symbols1[index1], model1)
                index1 += 1
            else:
                writer.write_symbol(symbols0[index0], model0)
                index0 += 1
    return writer.finish()

def encode_alpha_endpoint_palette(values: list[int]) -> bytes:
    symbols = []
    previous0 = 0
    previous1 = 0
    for value in values:
        current0 = value & 0xFF
        current1 = (value >> 8) & 0xFF
        symbols.append((current0 - previous0) & 0xFF)
        symbols.append((current1 - previous1) & 0xFF)
        previous0 = current0
        previous1 = current1
    model = balanced_model(256, symbols)
    writer = BitWriter()
    transmit_model(writer, model)
    for symbol in symbols:
        writer.write_symbol(symbol, model)
    return writer.finish()

def encode_selector_palette(values: list[int], selector_bits: int) -> bytes:
    if selector_bits == 2:
        to_linear = DXT1_TO_LINEAR
        selector_max = 3
    elif selector_bits == 3:
        to_linear = DXT5_TO_LINEAR
        selector_max = 7
    else:
        raise CrunchEncodeError(f"unsupported CRN selector width {selector_bits}")
    unique = selector_max * 2 + 1
    previous = [0] * 16
    symbols = []
    mask = (1 << selector_bits) - 1
    for value in values:
        current = [
            to_linear[(value >> (selector_bits * index)) & mask]
            for index in range(16)
        ]
        for index in range(0, 16, 2):
            delta0 = current[index] - previous[index]
            delta1 = current[index + 1] - previous[index + 1]
            symbols.append((delta1 + selector_max) * unique + delta0 + selector_max)
        previous = current
    model = balanced_model(unique * unique, symbols)
    writer = BitWriter()
    transmit_model(writer, model)
    for symbol in symbols:
        writer.write_symbol(symbol, model)
    return writer.finish()

def build_chunk_events(
    blocks: list[bytes],
    blocks_x: int,
    blocks_y: int,
    block_size: int,
    color_endpoint_indexes: dict[int, int],
    color_selector_indexes: dict[int, int],
    alpha_endpoint_indexes: dict[int, int],
    alpha_selector_indexes: dict[int, int],
) -> list[tuple[str, int]]:
    events: list[tuple[str, int]] = []
    previous = {
        "color_endpoint": 0,
        "color_selector": 0,
        "alpha_endpoint": 0,
        "alpha_selector": 0,
    }
    chunks_x = (blocks_x + 1) // 2
    chunks_y = (blocks_y + 1) // 2
    chunk_index = 0
    for chunk_y in range(chunks_y):
        chunk_range = (
            range(chunks_x - 1, -1, -1)
            if chunk_y & 1
            else range(chunks_x)
        )
        for chunk_x in chunk_range:
            if chunk_index % 3 == 0:
                events.append(("chunk", 511))
            chunk_index += 1
            chunk_blocks = []
            for block_y in range(2):
                y = min(chunk_y * 2 + block_y, blocks_y - 1)
                for block_x in range(2):
                    x = min(chunk_x * 2 + block_x, blocks_x - 1)
                    chunk_blocks.append(blocks[y * blocks_x + x])

            if block_size == 16:
                for block in chunk_blocks:
                    endpoint = struct.unpack_from("<H", block, 0)[0]
                    append_delta_event(
                        events,
                        previous,
                        "alpha_endpoint",
                        alpha_endpoint_indexes[endpoint],
                        len(alpha_endpoint_indexes),
                    )
            for block in chunk_blocks:
                endpoint_offset = 0 if block_size == 8 else 8
                endpoint = struct.unpack_from("<I", block, endpoint_offset)[0]
                append_delta_event(
                    events,
                    previous,
                    "color_endpoint",
                    color_endpoint_indexes[endpoint],
                    len(color_endpoint_indexes),
                )
            for block in chunk_blocks:
                if block_size == 16:
                    selector = int.from_bytes(block[2:8], "little")
                    append_delta_event(
                        events,
                        previous,
                        "alpha_selector",
                        alpha_selector_indexes[selector],
                        len(alpha_selector_indexes),
                    )
                selector_offset = 4 if block_size == 8 else 12
                selector = struct.unpack_from("<I", block, selector_offset)[0]
                append_delta_event(
                    events,
                    previous,
                    "color_selector",
                    color_selector_indexes[selector],
                    len(color_selector_indexes),
                )
    return events

def append_delta_event(
    events: list[tuple[str, int]],
    previous: dict[str, int],
    model_name: str,
    current: int,
    count: int,
) -> None:
    events.append((model_name, (current - previous[model_name]) % count))
    previous[model_name] = current

def build_event_models(
    events: list[tuple[str, int]],
    color_endpoint_count: int,
    color_selector_count: int,
    alpha_endpoint_count: int,
    alpha_selector_count: int,
) -> dict[str, HuffmanModel]:
    symbols: dict[str, list[int]] = {
        "chunk": [],
        "color_endpoint": [],
        "color_selector": [],
        "alpha_endpoint": [],
        "alpha_selector": [],
    }
    for model_name, symbol in events:
        symbols[model_name].append(symbol)
    models = {
        "chunk": balanced_model(512, symbols["chunk"]),
        "color_endpoint": balanced_model(
            color_endpoint_count,
            symbols["color_endpoint"],
        ),
        "color_selector": balanced_model(
            color_selector_count,
            symbols["color_selector"],
        ),
    }
    if alpha_endpoint_count:
        models["alpha_endpoint"] = balanced_model(
            alpha_endpoint_count,
            symbols["alpha_endpoint"],
        )
        models["alpha_selector"] = balanced_model(
            alpha_selector_count,
            symbols["alpha_selector"],
        )
    return models

def balanced_model(total_symbols: int, symbols) -> HuffmanModel:
    if total_symbols <= 0 or total_symbols > C_MAX_SUPPORTED_SYMS:
        raise CrunchEncodeError(f"invalid Huffman symbol count {total_symbols}")
    active = sorted(set(symbols))
    if not active:
        raise CrunchEncodeError("cant build an empty Huffman model")
    code_sizes = [0] * total_symbols
    if len(active) == 1:
        code_sizes[active[0]] = 1
    else:
        long_size = (len(active) - 1).bit_length()
        short_count = (1 << long_size) - len(active)
        for index, symbol in enumerate(active):
            code_sizes[symbol] = long_size - 1 if index < short_count else long_size
    return HuffmanModel(code_sizes, canonical_codes(code_sizes))

def canonical_codes(code_sizes: list[int]) -> list[int]:
    maximum = max(code_sizes, default=0)
    counts = [0] * (maximum + 1)
    for size in code_sizes:
        if size:
            counts[size] += 1
    next_codes = [0] * (maximum + 1)
    code = 0
    for size in range(1, maximum + 1):
        next_codes[size] = code
        code = (code + counts[size]) << 1
    codes = [0] * len(code_sizes)
    for symbol, size in enumerate(code_sizes):
        if size:
            codes[symbol] = next_codes[size]
            next_codes[size] += 1
    return codes

def transmit_model(writer: BitWriter, model: HuffmanModel) -> None:
    total_used = 0
    for index in range(len(model.code_sizes) - 1, -1, -1):
        if model.code_sizes[index]:
            total_used = index + 1
            break
    writer.write_bits(total_used, 14)
    if not total_used:
        return

    lengths = model.code_sizes[:total_used]
    length_model = balanced_model(21, lengths)
    sent_count = 0
    for index, symbol in enumerate(MOST_PROBABLE_CODELENGTH_CODES):
        if length_model.code_sizes[symbol]:
            sent_count = index + 1
    writer.write_bits(sent_count, 5)
    for index in range(sent_count):
        symbol = MOST_PROBABLE_CODELENGTH_CODES[index]
        writer.write_bits(length_model.code_sizes[symbol], 3)
    for length in lengths:
        writer.write_symbol(length, length_model)

def write_palette(
    header: bytearray,
    offset: int,
    data_offset: int,
    data_size: int,
    count: int,
) -> None:
    write_be(header, offset, 3, data_offset)
    write_be(header, offset + 3, 3, data_size)
    write_be(header, offset + 6, 2, count)

def write_be(buffer: bytearray, offset: int, size: int, value: int) -> None:
    maximum = (1 << (size * 8)) - 1
    if value < 0 or value > maximum:
        raise CrunchEncodeError(f"value {value} doesnt fit in {size} bytes")
    buffer[offset : offset + size] = int(value).to_bytes(size, "big")

def crc16(data: bytes, initial: int = 0) -> int:
    crc = (~initial) & 0xFFFF
    for value in data:
        quotient = value ^ (crc >> 8)
        crc = (crc << 8) & 0xFFFF
        remainder = (quotient >> 4) ^ quotient
        crc ^= remainder
        remainder = (remainder << 5) & 0xFFFF
        crc ^= remainder
        remainder = (remainder << 7) & 0xFFFF
        crc ^= remainder
    return (~crc) & 0xFFFF
