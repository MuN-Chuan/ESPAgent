from __future__ import annotations

import colorsys
import time
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Callable, Protocol

from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageFont

from espagent_demo.display_geometry import display_geometry
from espagent_demo.paths import app_resource_dir
from espagent_demo.protocol import TilePacket, encode_tile_payload, rgb888_to_rgb565
from espagent_demo.state import DemoState

WIDTH = 240
HEIGHT = 320
TILE_WIDTH = 120
TILE_HEIGHT = 6
FONT_SIZE = 14
PAPER = (248, 250, 252)
INK = (17, 24, 39)
CJK_FONT_CANDIDATES = (
    Path("C:/Windows/Fonts/Deng.ttf"),
    Path("C:/Windows/Fonts/msyh.ttc"),
    Path("C:/Windows/Fonts/simhei.ttf"),
    Path("C:/Windows/Fonts/simsun.ttc"),
    Path("C:/Windows/Fonts/Deng.ttf"),
    Path("C:/Windows/Fonts/NotoSansSC-VF.ttf"),
    Path("/System/Library/Fonts/PingFang.ttc"),
    Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"),
    Path("/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc"),
)
DISPLAY_FONT_CANDIDATES = (
    Path("C:/Windows/Fonts/Dengb.ttf"),
    Path("C:/Windows/Fonts/msyhbd.ttc"),
    *CJK_FONT_CANDIDATES,
)
TEXT_STROKE = INK
DUOTONE_FONT_SIZES = (32, 28, 24, 20, 16, 14, 12, 10)
RAW_COLOR_DIAGNOSTIC_PATTERNS = frozenset({"palette256", "channels", "colors"})
CHATGPT_DITHER_PATH = app_resource_dir() / "display_ui" / "assets" / "chatgpt-icon-dither.png"


class VideoSource(Protocol):
    def current_frame(self) -> Image.Image:
        ...


@dataclass(frozen=True)
class EncodedTile:
    packet: TilePacket

    @property
    def x(self) -> int:
        return self.packet.x

    @property
    def w(self) -> int:
        return self.packet.w


class DisplayRenderer:
    def __init__(
        self,
        video_source: VideoSource | None = None,
        *,
        link_snapshot_interval: float = 0.25,
        clock: Callable[[], float] = time.perf_counter,
    ) -> None:
        self.font = _load_display_font()
        self._fonts: dict[int, ImageFont.ImageFont] = {FONT_SIZE: self.font}
        self._display_fonts: dict[int, ImageFont.ImageFont] = {}
        self.video_source = video_source
        self.link_snapshot_interval = link_snapshot_interval
        self.clock = clock
        self._link_snapshot_until = 0.0
        self._link_snapshot_image: Image.Image | None = None
        self._chatgpt_icon = _load_chatgpt_icon()
        self._width = WIDTH
        self._height = HEIGHT

    def render(self, state: DemoState) -> Image.Image:
        geometry = display_geometry(state.display_ui.orientation)
        if (geometry.width, geometry.height) != (self._width, self._height):
            self._width, self._height = geometry.width, geometry.height
            self._link_snapshot_image = None
            self._link_snapshot_until = 0.0
        if state.active_scene == "video":
            image = self._render_video()
        elif state.active_scene == "test":
            image = self._render_test_pattern(state)
        elif state.active_scene == "lights":
            image = self._render_lights(state)
        elif state.active_scene == "link":
            image = self._render_link(state)
        elif state.active_scene == "codex":
            image = self._render_codex(state)
        else:
            image = self._render_hermes(state)
        if state.active_scene == "test" and state.test_pattern in RAW_COLOR_DIAGNOSTIC_PATTERNS:
            return image
        return _apply_display_tone(image, state)

    def _base(self, title: str, page: str) -> tuple[Image.Image, ImageDraw.ImageDraw]:
        image = Image.new("RGB", (self._width, self._height), PAPER)
        draw = ImageDraw.Draw(image)
        draw.rectangle((12, 13, 19, 20), fill=INK)
        _draw_binary_text(image, (28, 7), title, self._display_font(14), INK)
        page_width = draw.textlength(page, font=self._font(11))
        _draw_binary_text(image, (self._width - 12 - int(page_width), 9), page, self._font(11), INK)
        draw.line((12, 34, self._width - 12, 34), fill=INK, width=1)
        draw.line((12, self._height - 34, self._width - 12, self._height - 34), fill=INK, width=1)
        return image, draw

    def _render_hermes(self, state: DemoState) -> Image.Image:
        image, draw = self._base("HERMES", "01 / 03")
        status = _hermes_status_label(state.hermes.online, state.hermes.status)
        _draw_binary_text(image, (12, 47), status, self._display_font(13), INK)
        _draw_binary_text(image, (12, 68), "最近回复", self._font(11), INK)

        reply = state.hermes.last_reply.strip() or (state.hermes.interim_message or "").strip() or "等待 Hermes 回复"
        font, lines, line_height = self._fit_text(
            reply,
            max_width=self._width - 24,
            max_height=max(40, self._height - 128),
        )
        for index, line in enumerate(lines):
            _draw_binary_text(image, (12, 88 + index * line_height), line, font, INK)

        audio = _audio_status_label(state.hermes.audio_status)
        footer_y = self._height - 26
        _draw_binary_text(image, (12, footer_y), f"语音  {audio}", self._font(11), INK)
        link = "在线" if state.link.esp32_online else "离线"
        right = f"屏幕  {link}"
        right_width = draw.textlength(right, font=self._font(11))
        _draw_binary_text(image, (self._width - 12 - int(right_width), footer_y), right, self._font(11), INK)
        return image

    def _render_lights(self, state: DemoState) -> Image.Image:
        image, draw = self._base("HOME", "02 / 03")
        name = _truncate_text(draw, state.light.name or "灯具", self._display_font(13), self._width - 24)
        _draw_binary_text(image, (12, 47), name, self._display_font(13), INK)
        brightness = max(0, min(100, state.light.brightness))
        value = f"{brightness:02d}%"
        if self._width > self._height:
            _draw_binary_text(image, (12, 76), "已开启" if state.light.on else "已关闭", self._display_font(27), INK)
            _draw_binary_text(image, (12, 119), value, self._display_font(48), INK)
            _draw_binary_text(image, (109, 76), "亮度", self._font(11), INK)
            self._draw_segmented_bar(draw, brightness, x=109, y=98, segment_width=13, gap=5)
            kelvin = state.light.color_temp_kelvin or state.light.min_color_temp_kelvin
            temperature_range = max(1, state.light.max_color_temp_kelvin - state.light.min_color_temp_kelvin)
            temperature = round((kelvin - state.light.min_color_temp_kelvin) * 100 / temperature_range)
            _draw_binary_text(image, (109, 137), "色温", self._font(11), INK)
            self._draw_segmented_bar(draw, temperature, x=109, y=159, segment_width=13, gap=5)
        else:
            _draw_binary_text(image, (12, 76), "已开启" if state.light.on else "已关闭", self._display_font(31), INK)
            _draw_binary_text(image, (12, 125), value, self._display_font(52), INK)
            _draw_binary_text(image, (14, 185), "亮度", self._font(11), INK)
            self._draw_segmented_bar(draw, brightness, y=210)

        if state.light.error:
            footer = _truncate_text(draw, state.light.error, self._font(11), 216)
        elif state.light.online:
            footer = "HOME ASSISTANT  已同步"
        else:
            footer = "HOME ASSISTANT  离线"
        _draw_binary_text(image, (12, self._height - 26), footer, self._font(11), INK)
        return image

    def _render_link(self, state: DemoState) -> Image.Image:
        now = self.clock()
        if self._link_snapshot_image is not None and now < self._link_snapshot_until:
            return self._link_snapshot_image
        image, draw = self._base("DISPLAY", "03 / 03")
        status = "链路在线" if state.link.esp32_online else "等待设备"
        _draw_binary_text(image, (12, 47), status, self._display_font(13), INK)
        errors = state.link.rejected_packets + state.link.draw_failed_packets
        if self._width > self._height:
            _draw_binary_text(image, (12, 72), f"{state.link.display_fps:.1f}", self._display_font(48), INK)
            _draw_binary_text(image, (12, 128), "LCD FPS", self._font(11), INK)
            draw.line((145, 47, 145, self._height - 45), fill=INK, width=1)
            self._draw_metric(image, draw, 158, 49, "发送帧", str(state.link.frame_id), max_width=68)
            self._draw_metric(image, draw, 238, 49, "脏块", str(state.link.dirty_tiles), max_width=68)
            self._draw_metric(image, draw, 158, 112, "接收 / 通过", f"{state.link.rx_packets}/{state.link.accepted_packets}", max_width=68)
            self._draw_metric(image, draw, 238, 112, "异常", str(errors), max_width=68)
        else:
            _draw_binary_text(image, (12, 72), f"{state.link.display_fps:.1f}", self._display_font(48), INK)
            _draw_binary_text(image, (142, 101), "LCD FPS", self._font(11), INK)
            draw.line((12, 133, self._width - 12, 133), fill=INK, width=1)
            self._draw_metric(image, draw, 12, 148, "发送帧", str(state.link.frame_id))
            self._draw_metric(image, draw, 126, 148, "脏块", str(state.link.dirty_tiles))
            self._draw_metric(image, draw, 12, 199, "接收 / 通过", f"{state.link.rx_packets}/{state.link.accepted_packets}")
            self._draw_metric(image, draw, 126, 199, "异常", str(errors))

        ip = state.link.esp32_ip or "--"
        footer = f"{state.link.transport_mode.upper()}  ·  {ip}"
        _draw_binary_text(image, (12, self._height - 26), _truncate_text(draw, footer, self._font(11), self._width - 24), self._font(11), INK)
        self._link_snapshot_image = image
        self._link_snapshot_until = now + max(0.0, self.link_snapshot_interval)
        return image

    def _render_codex(self, state: DemoState) -> Image.Image:
        image, draw = self._base("CODEX", "04 / 04")
        usage = state.modules.get(state.active_module_id or "builtin.codex-usage", {})
        quota_value = usage.get("quota") or usage.get("primary") or usage.get("secondary")
        quota = quota_value if isinstance(quota_value, dict) else {}
        remaining = quota.get("remaining_percent", "--")
        landscape = self._width > self._height
        watermark = (
            self._chatgpt_icon.resize((116, 116), Image.Resampling.NEAREST)
            if landscape
            else self._chatgpt_icon
        )
        image.paste(watermark, (21 if landscape else 96, 35), watermark)
        number_text = str(remaining)
        number_font = self._display_font(76)
        number_origin = (12, 48)
        number_box = draw.textbbox(number_origin, number_text, font=number_font)
        _draw_binary_text(image, (12, 48), number_text, number_font, INK)
        suffix = "%"
        suffix_font = self._display_font(30)
        suffix_width = int(draw.textlength(suffix, font=suffix_font))
        suffix_limit = (132 if landscape else self._width - 12) - suffix_width
        suffix_x = min(number_box[2] + 8, suffix_limit)
        _draw_binary_text(image, (suffix_x, 91), suffix, suffix_font, INK)
        if landscape:
            draw.line((143, 47, 143, self._height - 45), fill=INK, width=1)
            self._draw_metric(image, draw, 155, 49, "额度周期", _quota_window_label(quota), max_width=70)
            self._draw_metric(image, draw, 237, 49, "额度组", str(usage.get("limit_name") or usage.get("limit_id") or "--"), max_width=70)
            self._draw_metric(image, draw, 155, 112, "计划", str(usage.get("plan_type") or "--").upper(), max_width=70)
            self._draw_metric(image, draw, 237, 112, "额度重置", _quota_reset_label(quota.get("resets_at")), max_width=70)
        else:
            draw.line((12, 145, self._width - 12, 145), fill=INK, width=1)
            self._draw_metric(image, draw, 12, 158, "额度周期", _quota_window_label(quota))
            self._draw_metric(image, draw, 126, 158, "额度组", str(usage.get("limit_name") or usage.get("limit_id") or "--"))
            self._draw_metric(image, draw, 12, 211, "计划", str(usage.get("plan_type") or "--").upper())
            self._draw_metric(image, draw, 126, 211, "额度重置", _quota_reset_label(quota.get("resets_at")))
        _draw_binary_text(image, (12, self._height - 26), "CHATGPT  ·  CODEX", self._font(11), INK)
        return image

    def _render_test_pattern(self, state: DemoState) -> Image.Image:
        pattern = state.test_pattern
        if pattern == "duotone":
            return self._render_duotone_test(state)
        if pattern == "palette256":
            return _build_256_color_test_pattern(self._width, self._height).copy()
        if pattern == "channels":
            return _build_channel_ramp_test_pattern(self._width, self._height).copy()
        if pattern == "colors":
            return _build_color_test_pattern(self._width, self._height).copy()
        image = Image.new("RGB", (self._width, self._height), (0, 0, 0))
        draw = ImageDraw.Draw(image)
        if pattern == "grid":
            draw.rectangle((0, 0, self._width, self._height), fill=(8, 13, 24))
            for x in range(0, self._width, 20):
                color = (56, 189, 248) if x % 40 == 0 else (30, 64, 175)
                draw.line((x, 0, x, self._height), fill=color)
            for y in range(0, self._height, 20):
                color = (250, 204, 21) if y % 40 == 0 else (51, 65, 85)
                draw.line((0, y, self._width, y), fill=color)
        elif pattern == "checker":
            tile = 20
            for y in range(0, self._height, tile):
                for x in range(0, self._width, tile):
                    fill = (255, 255, 255) if ((x // tile) + (y // tile)) % 2 == 0 else (0, 0, 0)
                    draw.rectangle((x, y, x + tile - 1, y + tile - 1), fill=fill)
        else:
            bars = [
                (255, 255, 255),
                (255, 255, 0),
                (0, 255, 255),
                (0, 255, 0),
                (255, 0, 255),
                (255, 0, 0),
                (0, 0, 255),
                (0, 0, 0),
            ]
            bar_w = self._width // len(bars)
            for index, color in enumerate(bars):
                x0 = index * bar_w
                x1 = self._width if index == len(bars) - 1 else (index + 1) * bar_w
                draw.rectangle((x0, 0, x1, self._height), fill=color)
        draw.rectangle((0, 0, self._width - 1, self._height - 1), outline=(255, 255, 255), width=2)
        _draw_text(draw, (8, self._height - 30), f"TEST: {pattern}", self.font, (255, 255, 255))
        return image

    def _render_duotone_test(self, state: DemoState) -> Image.Image:
        background = _parse_hex_color(state.duotone.background, (255, 255, 255))
        text_color = _parse_hex_color(state.duotone.text_color, (17, 17, 17))
        sample = state.duotone.text.strip() or "Aa 汉字 123 字号排版"
        image = Image.new("RGB", (self._width, self._height), background)
        draw = ImageDraw.Draw(image)
        y = 8
        for size in DUOTONE_FONT_SIZES:
            font = self._font(size)
            line = _truncate_text(draw, f"{size}px {sample}", font, self._width - 16)
            _draw_binary_text(image, (8, y), line, font, text_color)
            y += size + (9 if size >= 20 else 7)
            if y > self._height - 18:
                break
        footer = _truncate_text(draw, "TEST: duotone", self.font, self._width - 16)
        _draw_binary_text(image, (8, self._height - 24), footer, self.font, text_color)
        return image

    def _font(self, size: int) -> ImageFont.ImageFont:
        font = self._fonts.get(size)
        if font is None:
            font = _load_display_font(size)
            self._fonts[size] = font
        return font

    def _display_font(self, size: int) -> ImageFont.ImageFont:
        font = self._display_fonts.get(size)
        if font is None:
            font = _load_display_font(size, bold=True)
            self._display_fonts[size] = font
        return font

    def _fit_text(self, text: str, *, max_width: int, max_height: int) -> tuple[ImageFont.ImageFont, list[str], int]:
        probe = ImageDraw.Draw(Image.new("L", (1, 1)))
        for size in (22, 20, 18, 16):
            font = self._display_font(size) if size >= 20 else self._font(size)
            line_height = size + 7
            max_lines = max(1, max_height // line_height)
            lines, truncated = _wrap_text_pixels(probe, text, font, max_width, max_lines)
            if not truncated or size == 16:
                return font, lines, line_height
        return self._font(16), [text], 23

    def _draw_segmented_bar(
        self,
        draw: ImageDraw.ImageDraw,
        percent: int,
        *,
        y: int,
        x: int = 12,
        segment_width: int = 16,
        gap: int = 6,
    ) -> None:
        active = round(percent / 10)
        for index in range(10):
            x0 = x + index * (segment_width + gap)
            x1 = x0 + segment_width
            draw.rectangle((x0, y, x1, y + 18), outline=INK, width=1)
            if index < active:
                draw.rectangle((x0 + 3, y + 3, x1 - 3, y + 15), fill=INK)

    def _draw_metric(
        self,
        image: Image.Image,
        draw: ImageDraw.ImageDraw,
        x: int,
        y: int,
        label: str,
        value: str,
        *,
        max_width: int = 102,
    ) -> None:
        _draw_binary_text(image, (x, y), label, self._font(11), INK)
        value_font = self._display_font(20)
        for size in (20, 18, 16):
            candidate = self._display_font(size)
            if draw.textlength(value, font=candidate) <= max_width or size == 16:
                value_font = candidate
                break
        value = _truncate_text(draw, value, value_font, max_width)
        _draw_binary_text(image, (x, y + 18), value, value_font, INK)

    def _render_video(self) -> Image.Image:
        if self.video_source is None:
            image, draw = self._base("VIDEO", "-- / --")
            _draw_binary_text(image, (12, 72), "无视频信号", self._display_font(22), INK)
            return image
        try:
            frame = self.video_source.current_frame(size=(self._width, self._height))
        except TypeError:
            frame = self.video_source.current_frame()
        return frame.convert("RGB").resize((self._width, self._height), Image.Resampling.BILINEAR)


def _wrap_text(text: str, width: int) -> str:
    return "\n".join(text[i : i + width] for i in range(0, len(text), width))


def _hermes_status_label(online: bool, status: str) -> str:
    if not online:
        return "离线 · 等待连接"
    return {
        "thinking": "在线 · 正在思考",
        "replying": "在线 · 正在回复",
        "error": "在线 · 需要留意",
    }.get(status, "在线 · 待命")


def _audio_status_label(status: str) -> str:
    return {
        "ready": "就绪",
        "pending": "排队中",
        "generating": "生成中",
        "playing": "播放中",
        "interrupted": "已打断",
        "failed": "不可用",
        "disabled": "静音",
    }.get(status, status or "静音")


def _wrap_text_pixels(
    draw: ImageDraw.ImageDraw,
    text: str,
    font: ImageFont.ImageFont,
    max_width: int,
    max_lines: int,
) -> tuple[list[str], bool]:
    lines: list[str] = []
    current = ""
    truncated = False
    for char in text.replace("\r", ""):
        if char == "\n":
            lines.append(current.rstrip())
            current = ""
        elif draw.textlength(current + char, font=font) <= max_width:
            current += char
        else:
            lines.append(current.rstrip())
            current = char
        if len(lines) >= max_lines:
            truncated = True
            break
    if not truncated and (current or not lines):
        lines.append(current.rstrip())
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        truncated = True
    if truncated and lines:
        lines[-1] = _truncate_text(draw, lines[-1] + "...", font, max_width)
    return lines, truncated


def _draw_text(
    draw: ImageDraw.ImageDraw,
    xy: tuple[int, int],
    text: str,
    font: ImageFont.ImageFont,
    fill: tuple[int, int, int],
    *,
    stroke_fill: tuple[int, int, int] = TEXT_STROKE,
) -> None:
    draw.text(xy, text, font=font, fill=fill, stroke_width=1, stroke_fill=stroke_fill)


def _draw_multiline_text(
    draw: ImageDraw.ImageDraw,
    xy: tuple[int, int],
    text: str,
    font: ImageFont.ImageFont,
    fill: tuple[int, int, int],
    *,
    spacing: int,
) -> None:
    draw.multiline_text(xy, text, font=font, fill=fill, spacing=spacing, stroke_width=1, stroke_fill=TEXT_STROKE)


def _draw_binary_text(
    image: Image.Image,
    xy: tuple[int, int],
    text: str,
    font: ImageFont.ImageFont,
    fill: tuple[int, int, int],
) -> None:
    mask = Image.new("L", image.size, 0)
    ImageDraw.Draw(mask).text(xy, text, font=font, fill=255)
    mask = mask.point(lambda pixel: 255 if pixel >= 128 else 0)
    image.paste(Image.new("RGB", image.size, fill), mask=mask)


def _truncate_text(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.ImageFont, max_width: int) -> str:
    if draw.textlength(text, font=font) <= max_width:
        return text
    suffix = "..."
    available = max_width - draw.textlength(suffix, font=font)
    result = ""
    for char in text:
        if draw.textlength(result + char, font=font) > available:
            break
        result += char
    return result + suffix if result else suffix


def _parse_hex_color(value: str, fallback: tuple[int, int, int]) -> tuple[int, int, int]:
    color = value.strip()
    if len(color) == 7 and color.startswith("#"):
        try:
            return int(color[1:3], 16), int(color[3:5], 16), int(color[5:7], 16)
        except ValueError:
            return fallback
    return fallback


@lru_cache(maxsize=2)
def _build_256_color_test_pattern(width: int = WIDTH, height: int = HEIGHT) -> Image.Image:
    image = Image.new("RGB", (width, height), (0, 0, 0))
    _draw_256_color_cube(ImageDraw.Draw(image), width=width, cell_height=max(1, height // 16))
    return image


@lru_cache(maxsize=2)
def _build_channel_ramp_test_pattern(width: int = WIDTH, height: int = HEIGHT) -> Image.Image:
    image = Image.new("RGB", (width, height), (0, 0, 0))
    draw = ImageDraw.Draw(image)
    ramps = (
        lambda value: (value, value, value),
        lambda value: (value, 0, 0),
        lambda value: (0, value, 0),
        lambda value: (0, 0, value),
    )
    band_height = max(1, height // len(ramps))
    for band, make_color in enumerate(ramps):
        y0 = band * band_height
        y1 = height - 1 if band == len(ramps) - 1 else y0 + band_height - 1
        for x in range(width):
            value = round(x * 255 / (width - 1))
            draw.line((x, y0, x, y1), fill=make_color(value))
    return image


def _draw_256_color_cube(draw: ImageDraw.ImageDraw, *, width: int = WIDTH, cell_height: int) -> None:
    red_levels = tuple(round(index * 255 / 7) for index in range(8))
    green_levels = tuple(round(index * 255 / 7) for index in range(8))
    blue_levels = tuple(round(index * 255 / 3) for index in range(4))
    cell_width = width // 16
    for row in range(16):
        for column in range(16):
            blue_index = (column // 8) * 2 + (row // 8)
            color = (
                red_levels[column % 8],
                green_levels[row % 8],
                blue_levels[blue_index],
            )
            x0 = column * cell_width
            y0 = row * cell_height
            draw.rectangle((x0, y0, x0 + cell_width - 1, y0 + cell_height - 1), fill=color)


@lru_cache(maxsize=2)
def _build_color_test_pattern(width: int = WIDTH, height: int = HEIGHT) -> Image.Image:
    image = Image.new("RGB", (width, height), (0, 0, 0))
    draw = ImageDraw.Draw(image)

    scale_y = height / HEIGHT
    sy = lambda value: round(value * scale_y)

    # A 3:3:2 color cube flattened into four 8x8 RGB planes: exactly 256 colors.
    _draw_256_color_cube(draw, width=width, cell_height=max(1, sy(8)))
    cell_width = width // 16

    # Full-width ramps expose banding and channel clipping.
    ramps = (
        lambda value: (value, value, value),
        lambda value: (value, 0, 0),
        lambda value: (0, value, 0),
        lambda value: (0, 0, value),
    )
    for band, make_color in enumerate(ramps):
        y0 = sy(128 + band * 18)
        y1 = sy(128 + (band + 1) * 18) - 1
        for x in range(width):
            value = round(x * 255 / (width - 1))
            draw.line((x, y0, x, y1), fill=make_color(value))

    # Saturation increases downward while hue spans the width.
    color_pixels = image.load()
    for y in range(sy(200), sy(256)):
        saturation = 0.25 + 0.75 * (y - sy(200)) / max(1, sy(256) - sy(200) - 1)
        for x in range(width):
            red, green, blue = colorsys.hsv_to_rgb(x / width, saturation, 1.0)
            color_pixels[x, y] = (round(red * 255), round(green * 255), round(blue * 255))

    # Low-luminance hues expose black crush and weak-channel tinting.
    for y in range(sy(256), sy(288)):
        value = 0.08 + 0.42 * (y - sy(256)) / max(1, sy(288) - sy(256) - 1)
        for x in range(width):
            red, green, blue = colorsys.hsv_to_rgb(x / width, 1.0, value)
            color_pixels[x, y] = (round(red * 255), round(green * 255), round(blue * 255))

    # Sixteen equal grayscale steps make the visible tonal count easy to compare.
    for index in range(16):
        value = round(index * 255 / 15)
        x0 = index * cell_width
        x1 = width - 1 if index == 15 else x0 + cell_width - 1
        draw.rectangle((x0, sy(288), x1, height - 1), fill=(value, value, value))
    return image


def _apply_display_tone(image: Image.Image, state: DemoState) -> Image.Image:
    tone = state.display_tone
    if not tone.enabled:
        return image
    tuned = image
    if tone.binary_threshold > 0:
        threshold = max(1, min(254, tone.binary_threshold))
        mask = tuned.convert("L").point(lambda pixel: 255 if pixel >= threshold else 0)
        return Image.merge("RGB", (mask, mask, mask))
    if tone.contrast_percent != 100:
        tuned = ImageEnhance.Contrast(tuned).enhance(tone.contrast_percent / 100.0)
    if tone.brightness_percent != 100:
        tuned = ImageEnhance.Brightness(tuned).enhance(tone.brightness_percent / 100.0)
    if tone.color_percent != 100:
        tuned = ImageEnhance.Color(tuned).enhance(tone.color_percent / 100.0)
    if tone.sharpness_percent != 100:
        tuned = ImageEnhance.Sharpness(tuned).enhance(tone.sharpness_percent / 100.0)
    if tone.sharpness_percent >= 125:
        tuned = tuned.filter(ImageFilter.SHARPEN)
    return tuned


def _quota_window_label(value: dict[str, object]) -> str:
    minutes = int(value.get("window_duration_minutes") or 0)
    if not minutes:
        return "--"
    if minutes % 1440 == 0:
        return f"{minutes // 1440} 天"
    if minutes % 60 == 0:
        return f"{minutes // 60} 小时"
    return f"{minutes} 分钟"


def _quota_reset_label(value: object) -> str:
    try:
        return time.strftime("%m-%d", time.localtime(float(value))) if value else "--"
    except (TypeError, ValueError, OverflowError):
        return "--"


def _load_chatgpt_icon() -> Image.Image:
    try:
        with Image.open(CHATGPT_DITHER_PATH) as source:
            return source.convert("RGBA").resize((132, 132), Image.Resampling.NEAREST)
    except OSError:
        fallback = Image.new("RGBA", (132, 132), (0, 0, 0, 0))
        ImageDraw.Draw(fallback).ellipse((12, 12, 119, 119), outline=(*INK, 255), width=1)
        return fallback


def _load_display_font(size: int = FONT_SIZE, *, bold: bool = False) -> ImageFont.ImageFont:
    candidates = DISPLAY_FONT_CANDIDATES if bold else CJK_FONT_CANDIDATES
    for path in candidates:
        if path.exists():
            try:
                return ImageFont.truetype(str(path), size)
            except OSError:
                continue
    return ImageFont.load_default()


def split_dirty_tiles(
    image: Image.Image,
    previous: Image.Image | None,
    frame_id: int,
    compression_mode: str = "auto",
    tile_width: int = TILE_WIDTH,
    tile_height: int = TILE_HEIGHT,
) -> list[EncodedTile]:
    if tile_width <= 0 or tile_height <= 0:
        raise ValueError("tile_width and tile_height must be positive")
    width, height = image.size
    if previous is not None and previous.size != image.size:
        previous = None
    tiles: list[EncodedTile] = []
    for y in range(0, height, tile_height):
        for x in range(0, width, tile_width):
            w = min(tile_width, width - x)
            h = min(tile_height, height - y)
            current = image.crop((x, y, x + w, y + h))
            if previous is not None and current.tobytes() == previous.crop((x, y, x + w, y + h)).tobytes():
                continue
            raw_payload = _image_to_rgb565_bytes(current)
            compression, payload = encode_tile_payload(raw_payload, compression_mode)
            tiles.append(
                EncodedTile(
                    TilePacket(
                        frame_id=frame_id,
                        tile_id=len(tiles),
                        x=x,
                        y=y,
                        w=w,
                        h=h,
                        payload=payload,
                        compression=compression,
                        raw_len=len(raw_payload),
                    )
                )
            )
    return tiles


def _image_to_rgb565_bytes(image: Image.Image) -> bytes:
    return image_to_rgb565_bytes(image)


def image_to_rgb565_bytes(image: Image.Image) -> bytes:
    out = bytearray()
    data = image.convert("RGB").tobytes()
    for i in range(0, len(data), 3):
        r, g, b = data[i], data[i + 1], data[i + 2]
        value = rgb888_to_rgb565(r, g, b)
        out.append(value & 0xFF)
        out.append((value >> 8) & 0xFF)
    return bytes(out)
