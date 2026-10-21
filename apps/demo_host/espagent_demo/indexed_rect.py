from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from PIL import Image

from espagent_demo.protocol import (
    INDEXED_CHUNK_PAYLOAD_SIZE,
    INDEXED_FRAME_MAX_BYTES,
    IndexedFrameChunkPacket,
    _encode_lz4_block,
    rgb888_to_rgb565,
)

WIDTH = 240
HEIGHT = 320
COLOR_SNAP_DISTANCE = 40
EDGE_PROTECTION_DISTANCE = 24
MAX_FIRST_PASS_COLORS = 256
GLOBAL_FREQUENT_COLORS = 192
NEUTRAL_PALETTE_CHROMA = 32
BINARY_DARK_COVERAGE = 0.66
_RGB_DISTANCE_WEIGHTS = np.array((2, 4, 1), dtype=np.int32)
_RGB_DISTANCE_WEIGHT_SUM = 7


@dataclass(frozen=True)
class IndexedFrame:
    frame_id: int
    chunks: tuple[IndexedFrameChunkPacket, ...]
    preview: Image.Image

    def packets(self) -> list[bytes]:
        return [chunk.encode() for chunk in self.chunks]


class IndexedRectEncoder:
    """Encode an independent full indexed frame; the legacy name keeps callers stable."""

    def __init__(
        self,
        *,
        bpp: int = 2,
        keyframe_interval: int = 60,
        protected_colors: tuple[tuple[int, int, int], ...] = (),
    ) -> None:
        if bpp not in {1, 2, 4, 8}:
            raise ValueError("bpp must be 1, 2, 4, or 8")
        self.bpp = bpp
        self.protected_colors = _normalize_protected_colors(protected_colors, 1 << bpp)
        self._cached_source_image: Image.Image | None = None
        self._cached_grayscale_antialiasing = False
        self._cached_blob: bytes | None = None
        self._cached_raw_len = 0
        self._cached_preview: Image.Image | None = None

    def encode(
        self,
        image: Image.Image,
        frame_id: int,
        *,
        force_keyframe: bool = False,
        grayscale_antialiasing: bool = False,
    ) -> IndexedFrame:
        if (
            image is self._cached_source_image
            and grayscale_antialiasing == self._cached_grayscale_antialiasing
            and self._cached_blob is not None
            and self._cached_preview is not None
        ):
            blob = self._cached_blob
            raw_len = self._cached_raw_len
            preview = self._cached_preview
        else:
            source_image = image
            image = image.convert("RGB")
            width, height = image.size
            if (width, height) not in {(240, 320), (320, 240)}:
                raise ValueError("indexed frame must be 240x320 or 320x240")
            palette, indices = _snap_to_dynamic_palette(image, self.bpp)
            if _palette_is_neutral(palette):
                darkest = min(palette, key=_color_luminance)
                lightest = max(palette, key=_color_luminance)
                image = _normalize_neutral_edges(
                    image,
                    darkest=darkest,
                    lightest=lightest,
                    bpp=self.bpp,
                    grayscale_antialiasing=grayscale_antialiasing,
                )
                palette, indices = _snap_to_dynamic_palette(image, self.bpp)
            palette, indices = _apply_protected_colors(image, palette, indices, self.protected_colors)
            packed = _pack_indices(indices, self.bpp)
            palette565 = tuple(rgb888_to_rgb565(*color) for color in palette)
            preview = _indexed_preview(indices, palette565, width, height)
            palette_bytes = b"".join(color.to_bytes(2, "little") for color in palette565)
            blob = palette_bytes + _encode_lz4_block(packed)
            raw_len = len(packed)
            self._cached_source_image = source_image
            self._cached_grayscale_antialiasing = grayscale_antialiasing
            self._cached_blob = blob
            self._cached_raw_len = raw_len
            self._cached_preview = preview
        if len(blob) > INDEXED_FRAME_MAX_BYTES:
            raise ValueError("compressed indexed frame exceeds the protocol limit")
        chunk_count = (len(blob) + INDEXED_CHUNK_PAYLOAD_SIZE - 1) // INDEXED_CHUNK_PAYLOAD_SIZE
        chunks = tuple(
            IndexedFrameChunkPacket(
                frame_id=frame_id,
                chunk_id=chunk_id,
                chunk_count=chunk_count,
                frame_len=len(blob),
                bpp=self.bpp,
                raw_len=raw_len,
                payload=blob[offset:offset + INDEXED_CHUNK_PAYLOAD_SIZE],
                width=image.width,
                height=image.height,
            )
            for chunk_id, offset in enumerate(range(0, len(blob), INDEXED_CHUNK_PAYLOAD_SIZE))
        )
        return IndexedFrame(frame_id, chunks, preview)

    def reset(self) -> None:
        self._cached_source_image = None
        self._cached_blob = None
        self._cached_raw_len = 0
        self._cached_preview = None


def _select_palette(image: Image.Image, bpp: int) -> tuple[tuple[int, int, int], ...]:
    palette, _indices = _snap_to_dynamic_palette(image.convert("RGB"), bpp)
    return palette


@dataclass
class _ColorCluster:
    representative_index: int
    pixel_count: int
    member_values: set[int]


def _normalize_protected_colors(
    protected_colors: tuple[tuple[int, int, int], ...],
    max_colors: int,
) -> tuple[tuple[int, int, int], ...]:
    if len(protected_colors) > max_colors:
        raise ValueError(f"at most {max_colors} protected colors are supported for this format")
    normalized: list[tuple[int, int, int]] = []
    for color in protected_colors:
        if len(color) != 3 or any(not 0 <= int(channel) <= 255 for channel in color):
            raise ValueError("protected colors must be RGB tuples")
        aligned = _rgb565_to_rgb888(rgb888_to_rgb565(*(int(channel) for channel in color)))
        if aligned not in normalized:
            normalized.append(aligned)
    return tuple(normalized)


def _apply_protected_colors(
    image: Image.Image,
    palette: tuple[tuple[int, int, int], ...],
    indices: bytes,
    protected_colors: tuple[tuple[int, int, int], ...],
) -> tuple[tuple[tuple[int, int, int], ...], bytes]:
    if not protected_colors:
        return palette, indices

    palette_colors = list(palette)
    palette565 = [rgb888_to_rgb565(*color) for color in palette_colors]
    source565 = _rgb888_array_to_rgb565(np.asarray(image.convert("RGB"), dtype=np.uint8)).ravel()
    source_values = set(int(value) for value in np.unique(source565))
    remapped = np.frombuffer(indices, dtype=np.uint8).copy()
    usage = np.bincount(remapped, minlength=len(palette_colors))
    reserved_slots: set[int] = set()
    slot_for_value: dict[int, int] = {}
    replaced_positions: list[tuple[int, np.ndarray]] = []

    for color in protected_colors:
        value = rgb888_to_rgb565(*color)
        if value not in source_values:
            continue
        if value in slot_for_value:
            continue
        matching = [
            slot for slot, palette_value in enumerate(palette565)
            if palette_value == value and slot not in reserved_slots
        ]
        if matching:
            slot = max(matching, key=lambda candidate: (int(usage[candidate]), -candidate))
        else:
            available = [slot for slot in range(len(palette_colors)) if slot not in reserved_slots]
            slot = min(available, key=lambda candidate: (int(usage[candidate]), candidate))
            positions = np.flatnonzero(remapped == slot)
            palette_colors[slot] = color
            palette565[slot] = value
            replaced_positions.append((slot, positions))
        reserved_slots.add(slot)
        slot_for_value[value] = slot

    palette_rgb = np.asarray(palette_colors, dtype=np.int32)
    for _slot, positions in replaced_positions:
        for start in range(0, len(positions), 4096):
            batch_positions = positions[start:start + 4096]
            source_rgb = _rgb565_array_to_rgb888(source565[batch_positions])
            differences = source_rgb[:, None, :] - palette_rgb[None, :, :]
            distances = np.sum(differences * differences * _RGB_DISTANCE_WEIGHTS, axis=2)
            remapped[batch_positions] = np.argmin(distances, axis=1).astype(np.uint8)

    for value, slot in slot_for_value.items():
        remapped[source565 == value] = slot
    return tuple(palette_colors), remapped.tobytes()


def _snap_to_dynamic_palette(image: Image.Image, bpp: int) -> tuple[tuple[tuple[int, int, int], ...], bytes]:
    if bpp not in {1, 2, 4, 8}:
        raise ValueError("bpp must be 1, 2, 4, or 8")
    if bpp >= 4:
        return _quantize_high_color_palette(image, bpp)

    rgb = np.asarray(image.convert("RGB"), dtype=np.uint8)
    rgb565 = _rgb888_array_to_rgb565(rgb)
    unique_values, inverse, counts = np.unique(rgb565, return_inverse=True, return_counts=True)
    unique_rgb = _rgb565_array_to_rgb888(unique_values)
    first_pass_colors = _select_first_pass_colors(unique_rgb, counts)
    hard_neighbors = _find_hard_edge_neighbors(rgb565, unique_values[first_pass_colors])
    cluster_for_color = np.empty(len(unique_values), dtype=np.int32)
    clusters: list[_ColorCluster] = []

    # Starting with the most common source colors makes every representative a
    # real, dominant frame color instead of an average synthesized by quantization.
    for color_index in first_pass_colors:
        value = int(unique_values[color_index])
        protected = hard_neighbors.get(value, set())
        best_cluster = -1
        best_distance: int | None = None
        for cluster_index, cluster in enumerate(clusters):
            if not protected.isdisjoint(cluster.member_values):
                continue
            distance = _weighted_distance(unique_rgb[color_index], unique_rgb[cluster.representative_index])
            if distance > _distance_limit(COLOR_SNAP_DISTANCE):
                continue
            if best_distance is None or distance < best_distance:
                best_cluster = cluster_index
                best_distance = distance
        if best_cluster < 0:
            best_cluster = len(clusters)
            clusters.append(_ColorCluster(int(color_index), int(counts[color_index]), {value}))
        else:
            clusters[best_cluster].pixel_count += int(counts[color_index])
            clusters[best_cluster].member_values.add(value)
        cluster_for_color[color_index] = best_cluster

    first_pass_mask = np.zeros(len(unique_values), dtype=bool)
    first_pass_mask[first_pass_colors] = True
    deferred_colors = np.flatnonzero(~first_pass_mask)
    if len(deferred_colors):
        representative_rgb = np.stack(
            [unique_rgb[cluster.representative_index] for cluster in clusters],
            axis=0,
        )
        for start in range(0, len(deferred_colors), 4096):
            batch = deferred_colors[start:start + 4096]
            differences = unique_rgb[batch, None, :] - representative_rgb[None, :, :]
            distances = np.sum(differences * differences * _RGB_DISTANCE_WEIGHTS, axis=2)
            nearest = np.argmin(distances, axis=1).astype(np.int32)
            cluster_for_color[batch] = nearest
            for cluster_index in np.unique(nearest):
                assigned = batch[nearest == cluster_index]
                clusters[int(cluster_index)].pixel_count += int(np.sum(counts[assigned]))

    palette_size = 1 << bpp
    kept_clusters = sorted(
        range(len(clusters)),
        key=lambda index: (-clusters[index].pixel_count, clusters[index].representative_index),
    )[:palette_size]
    kept_clusters.sort(key=lambda index: _color_sort_key(unique_rgb[clusters[index].representative_index]))
    slot_for_cluster = {cluster_index: slot for slot, cluster_index in enumerate(kept_clusters)}

    for cluster_index, cluster in enumerate(clusters):
        if cluster_index in slot_for_cluster:
            continue
        nearest = min(
            kept_clusters,
            key=lambda kept: _weighted_distance(
                unique_rgb[cluster.representative_index],
                unique_rgb[clusters[kept].representative_index],
            ),
        )
        slot_for_cluster[cluster_index] = slot_for_cluster[nearest]

    color_slots = np.fromiter(
        (slot_for_cluster[int(cluster_index)] for cluster_index in cluster_for_color),
        dtype=np.uint8,
        count=len(cluster_for_color),
    )
    indices = color_slots[inverse].tobytes()
    palette = [tuple(int(channel) for channel in unique_rgb[clusters[index].representative_index]) for index in kept_clusters]
    while len(palette) < palette_size:
        palette.append(palette[-1] if palette else (0, 0, 0))
    return tuple(palette), indices


def _select_first_pass_colors(unique_rgb: np.ndarray, counts: np.ndarray) -> np.ndarray:
    frequency_order = np.argsort(-counts, kind="stable")
    if len(frequency_order) <= MAX_FIRST_PASS_COLORS:
        return frequency_order

    # The 2-bit color cells are only an acceleration index. Every selected
    # representative remains the most frequent real RGB565 color in that cell.
    coarse_cells = (
        ((unique_rgb[:, 0] >> 6) << 4)
        | ((unique_rgb[:, 1] >> 6) << 2)
        | (unique_rgb[:, 2] >> 6)
    )
    selected = list(int(index) for index in frequency_order[:GLOBAL_FREQUENT_COLORS])
    selected_set = set(selected)
    represented_cells = {int(coarse_cells[index]) for index in selected}
    for index in frequency_order:
        cell = int(coarse_cells[index])
        if cell in represented_cells:
            continue
        selected.append(int(index))
        selected_set.add(int(index))
        represented_cells.add(cell)
        if len(selected) >= MAX_FIRST_PASS_COLORS:
            break
    return np.array(sorted(selected_set, key=lambda index: (-int(counts[index]), index)), dtype=np.int64)


def _quantize_high_color_palette(
    image: Image.Image,
    bpp: int,
) -> tuple[tuple[tuple[int, int, int], ...], bytes]:
    rgb = np.asarray(image.convert("RGB"), dtype=np.uint8)
    aligned_rgb = _rgb565_array_to_rgb888(_rgb888_array_to_rgb565(rgb)).astype(np.uint8)
    aligned = Image.fromarray(aligned_rgb, "RGB")
    palette_size = 1 << bpp
    quantized = aligned.quantize(
        colors=palette_size,
        method=Image.Quantize.MAXCOVERAGE,
        dither=Image.Dither.NONE,
    )
    raw_palette = quantized.getpalette()
    palette = tuple(
        _rgb565_to_rgb888(
            rgb888_to_rgb565(
                raw_palette[index * 3],
                raw_palette[index * 3 + 1],
                raw_palette[index * 3 + 2],
            )
        )
        for index in range(palette_size)
    )
    return palette, quantized.tobytes()


def _rgb888_array_to_rgb565(rgb: np.ndarray) -> np.ndarray:
    values = rgb.astype(np.uint16)
    return ((values[..., 0] >> 3) << 11) | ((values[..., 1] >> 2) << 5) | (values[..., 2] >> 3)


def _rgb565_array_to_rgb888(values: np.ndarray) -> np.ndarray:
    values = values.astype(np.uint16, copy=False)
    red = (values >> 11) & 0x1F
    green = (values >> 5) & 0x3F
    blue = values & 0x1F
    return np.stack(
        (
            (red << 3) | (red >> 2),
            (green << 2) | (green >> 4),
            (blue << 3) | (blue >> 2),
        ),
        axis=-1,
    ).astype(np.int32)


def _find_hard_edge_neighbors(rgb565: np.ndarray, tracked_values: np.ndarray) -> dict[int, set[int]]:
    horizontal = np.stack((rgb565[:, :-1].ravel(), rgb565[:, 1:].ravel()), axis=1)
    vertical = np.stack((rgb565[:-1, :].ravel(), rgb565[1:, :].ravel()), axis=1)
    pairs = np.concatenate((horizontal, vertical), axis=0)
    tracked = np.zeros(1 << 16, dtype=bool)
    tracked[tracked_values] = True
    pairs = pairs[
        (pairs[:, 0] != pairs[:, 1])
        & tracked[pairs[:, 0]]
        & tracked[pairs[:, 1]]
    ]
    if not len(pairs):
        return {}
    pairs.sort(axis=1)
    pair_keys = (pairs[:, 0].astype(np.uint32) << 16) | pairs[:, 1].astype(np.uint32)
    pair_keys = np.unique(pair_keys)
    left_values = (pair_keys >> 16).astype(np.uint16)
    right_values = (pair_keys & 0xFFFF).astype(np.uint16)
    left_rgb = _rgb565_array_to_rgb888(left_values)
    right_rgb = _rgb565_array_to_rgb888(right_values)
    differences = left_rgb - right_rgb
    distances = np.sum(differences * differences * _RGB_DISTANCE_WEIGHTS, axis=1)
    protected_keys = pair_keys[distances > _distance_limit(EDGE_PROTECTION_DISTANCE)]
    neighbors: dict[int, set[int]] = {}
    for key in protected_keys:
        left = int(key >> np.uint32(16))
        right = int(key & np.uint32(0xFFFF))
        neighbors.setdefault(left, set()).add(right)
        neighbors.setdefault(right, set()).add(left)
    return neighbors


def _weighted_distance(left: np.ndarray, right: np.ndarray) -> int:
    red = int(left[0]) - int(right[0])
    green = int(left[1]) - int(right[1])
    blue = int(left[2]) - int(right[2])
    return 2 * red * red + 4 * green * green + blue * blue


def _distance_limit(distance: int) -> int:
    return _RGB_DISTANCE_WEIGHT_SUM * distance * distance


def _color_sort_key(color: np.ndarray) -> tuple[int, int, int, int]:
    red, green, blue = (int(channel) for channel in color)
    return 2 * red + 4 * green + blue, red, green, blue


def _color_luminance(color: tuple[int, int, int]) -> int:
    red, green, blue = color
    return 77 * red + 150 * green + 29 * blue


def _palette_is_neutral(palette: tuple[tuple[int, int, int], ...]) -> bool:
    return all(max(color) - min(color) <= NEUTRAL_PALETTE_CHROMA for color in palette)


def _normalize_neutral_edges(
    image: Image.Image,
    *,
    darkest: tuple[int, int, int],
    lightest: tuple[int, int, int],
    bpp: int,
    grayscale_antialiasing: bool,
) -> Image.Image:
    rgb = np.asarray(image.convert("RGB"), dtype=np.uint8).astype(np.int32)
    luminance = (77 * rgb[..., 0] + 150 * rgb[..., 1] + 29 * rgb[..., 2] + 128) >> 8
    dark = np.asarray(darkest, dtype=np.int32)
    light = np.asarray(lightest, dtype=np.int32)
    dark_luminance = int((77 * dark[0] + 150 * dark[1] + 29 * dark[2] + 128) >> 8)
    light_luminance = int((77 * light[0] + 150 * light[1] + 29 * light[2] + 128) >> 8)
    luminance_range = max(1, light_luminance - dark_luminance)
    coverage = np.clip((luminance - dark_luminance) / luminance_range, 0.0, 1.0)
    if grayscale_antialiasing:
        level_count = 1 << bpp
        coverage = np.rint(coverage * (level_count - 1)) / (level_count - 1)
    else:
        coverage = (coverage >= BINARY_DARK_COVERAGE).astype(np.float64)
    normalized = dark[None, None, :] + coverage[..., None] * (light - dark)[None, None, :]
    return Image.fromarray(np.rint(normalized).astype(np.uint8), "RGB")


def _indexed_preview(indices: bytes, palette565: tuple[int, ...], width: int, height: int) -> Image.Image:
    palette_rgb = [_rgb565_to_rgb888(color) for color in palette565]
    indexed = Image.frombytes("P", (width, height), indices)
    flat = [channel for color in palette_rgb for channel in color]
    indexed.putpalette(flat + [0] * (768 - len(flat)))
    return indexed.convert("RGB")


def _rgb565_to_rgb888(color: int) -> tuple[int, int, int]:
    red = (color >> 11) & 0x1F
    green = (color >> 5) & 0x3F
    blue = color & 0x1F
    return (red << 3) | (red >> 2), (green << 2) | (green >> 4), (blue << 3) | (blue >> 2)


def _pack_indices(indices: bytes, bpp: int) -> bytes:
    per_byte = 8 // bpp
    mask = (1 << bpp) - 1
    values = np.frombuffer(indices, dtype=np.uint8) & mask
    padding = (-len(values)) % per_byte
    if padding:
        values = np.pad(values, (0, padding), mode="constant")
    shifts = np.arange(per_byte - 1, -1, -1, dtype=np.uint8) * bpp
    grouped = values.reshape(-1, per_byte)
    return np.bitwise_or.reduce(grouped << shifts, axis=1).astype(np.uint8, copy=False).tobytes()
