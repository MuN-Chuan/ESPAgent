from espagent_demo.renderer import DisplayRenderer
from espagent_demo.protocol import IndexedFrameChunkPacket, JpegChunkPacket, PKT_FRAME_END, PKT_INDEXED_FRAME_CHUNK, PKT_JPEG_CHUNK, TilePacket
from espagent_demo.state import DemoState
from espagent_demo.udp_link import FrameStreamer
from PIL import Image
import pytest
import threading


class FakeSocket:
    def __init__(self) -> None:
        self.sent: list[tuple[bytes, tuple[str, int]]] = []
        self.closed = False

    def sendto(self, payload: bytes, target: tuple[str, int]) -> None:
        self.sent.append((payload, target))

    def close(self) -> None:
        self.closed = True


class FakeSleeper:
    def __init__(self) -> None:
        self.calls: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


def test_frame_streamer_can_switch_to_discovered_target_ip():
    streamer = FrameStreamer("192.168.1.7", 39000, dry_run=True)

    streamer.set_target_ip("192.168.1.23")

    assert streamer.target == ("192.168.1.23", 39000)


def test_frame_streamer_sends_complete_indexed_frame_chunks():
    sock = FakeSocket()
    state = DemoState(active_scene="test", test_pattern="checker")
    streamer = FrameStreamer("127.0.0.1", 39000, sock=sock, packet_gap_seconds=0)
    streamer.set_transport_options(transport_mode="indexed")

    result = streamer.render_and_send(DisplayRenderer(), state)

    packet_types = [payload[3] for payload, _target in sock.sent]
    assert result.dirty_tiles == packet_types.count(PKT_INDEXED_FRAME_CHUNK)
    assert packet_types[-result.dirty_tiles:] == [PKT_INDEXED_FRAME_CHUNK] * result.dirty_tiles


def test_frame_streamer_sends_i8_frame_chunks_with_full_raw_length():
    sock = FakeSocket()
    state = DemoState(active_scene="test", test_pattern="colorbars")
    streamer = FrameStreamer("127.0.0.1", 39000, sock=sock, packet_gap_seconds=0)
    streamer.set_transport_options(transport_mode="indexed", indexed_bpp=8)

    result = streamer.render_and_send(DisplayRenderer(), state)
    chunks = [
        IndexedFrameChunkPacket.decode(payload)
        for payload, _target in sock.sent
        if payload[3] == PKT_INDEXED_FRAME_CHUNK
    ]

    assert len(chunks) == result.dirty_tiles
    assert chunks[0].bpp == 8
    assert chunks[0].raw_len == 240 * 320
    assert chunks[-1].chunk_id == chunks[0].chunk_count - 1


def test_frame_streamer_rebuilds_indexed_encoder_with_generic_protected_color_slots():
    streamer = FrameStreamer("127.0.0.1", 39000, dry_run=True)
    protected_colors = ["#123456"] + [None] * 15

    streamer.set_transport_options(
        transport_mode="indexed",
        indexed_bpp=4,
        indexed_protected_colors=protected_colors,
    )

    assert streamer.indexed_protected_colors == tuple(protected_colors)
    assert streamer._indexed_encoder.bpp == 4
    assert streamer._indexed_encoder.protected_colors == ((16, 52, 82),)
    assert streamer.force_full_refresh_frames > 0


def test_frame_streamer_resizes_protected_slots_when_indexed_format_changes():
    streamer = FrameStreamer("127.0.0.1", 39000, dry_run=True)
    streamer.set_transport_options(
        indexed_bpp=2,
        indexed_protected_colors=["#000000", "#FFFFFF", "#FF0000", "#00FF00"],
    )

    streamer.set_transport_options(indexed_bpp=1)
    assert streamer.indexed_protected_colors == ("#000000", "#FFFFFF")

    streamer.set_transport_options(indexed_bpp=8)
    assert len(streamer.indexed_protected_colors) == 256
    assert streamer.indexed_protected_colors[:2] == ("#000000", "#FFFFFF")
    assert all(color is None for color in streamer.indexed_protected_colors[2:])


@pytest.mark.parametrize(
    "colors",
    [
        ["#000000"],
        ["#000000", "#FFFFFF", None, None, None],
        ["invalid", None, None, None],
    ],
)
def test_frame_streamer_rejects_invalid_protected_color_slots(colors):
    streamer = FrameStreamer("127.0.0.1", 39000, dry_run=True)

    with pytest.raises(ValueError):
        streamer.set_transport_options(indexed_protected_colors=colors)


def test_mjpeg_pacing_only_sleeps_for_remaining_frame_interval():
    now = [10.0]
    sleeps: list[float] = []

    def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        now[0] += seconds

    streamer = FrameStreamer(
        "127.0.0.1",
        39000,
        dry_run=True,
        clock=lambda: now[0],
        sleep=sleep,
        mjpeg_max_fps=30,
    )

    streamer._pace_mjpeg_frame()
    now[0] += 0.01
    streamer._pace_mjpeg_frame()

    assert sleeps == pytest.approx([1 / 30 - 0.01])


def test_frame_streamer_dry_run_counts_dirty_tiles_without_socket_send():
    state = DemoState()
    streamer = FrameStreamer("127.0.0.1", 39000, dry_run=True)
    renderer = DisplayRenderer()

    first = streamer.render_and_send(renderer, state)
    second = streamer.render_and_send(renderer, state)

    assert streamer.sock is None
    assert first.frame_id == 1
    assert first.dirty_tiles == 108
    assert second.frame_id == 2
    assert second.dirty_tiles == 0
    assert state.link.frame_id == 2
    assert state.link.dirty_tiles == 0
    assert state.link.fps == 0


def test_frame_streamer_reports_windowed_frame_rate():
    timestamps = iter((0.0, 0.01, 0.5, 0.51, 1.0, 1.01, 1.5, 1.51, 2.01, 2.02))
    state = DemoState()
    streamer = FrameStreamer("127.0.0.1", 39000, dry_run=True, clock=lambda: next(timestamps))
    renderer = DisplayRenderer()

    for _ in range(5):
        streamer.render_and_send(renderer, state)

    assert state.link.fps == pytest.approx(4.0 / 2.01)


def test_frame_streamer_dry_run_does_not_send_to_injected_socket():
    sock = FakeSocket()
    state = DemoState()
    streamer = FrameStreamer("127.0.0.1", 39000, dry_run=True, sock=sock)

    result = streamer.render_and_send(DisplayRenderer(), state)

    assert result.dirty_tiles == 108
    assert sock.sent == []


def test_frame_streamer_sends_dirty_tiles_to_injected_socket():
    sock = FakeSocket()
    state = DemoState()
    streamer = FrameStreamer("127.0.0.1", 39000, sock=sock)

    result = streamer.render_and_send(DisplayRenderer(), state)

    assert result.dirty_tiles == 108
    assert len(sock.sent) == 112
    assert {target for _, target in sock.sent} == {("127.0.0.1", 39000)}
    assert sock.sent[-1][0][3] == PKT_FRAME_END


def test_frame_streamer_paces_large_tile_bursts():
    sock = FakeSocket()
    sleeper = FakeSleeper()
    state = DemoState()
    streamer = FrameStreamer(
        "127.0.0.1",
        39000,
        sock=sock,
        packet_burst_size=4,
        packet_gap_seconds=0.01,
        sleep=sleeper,
    )

    result = streamer.render_and_send(DisplayRenderer(), state)

    assert result.dirty_tiles == 108
    assert len(sock.sent) == 112
    assert sleeper.calls == [0.01] * 29


def test_frame_streamer_periodically_forces_full_refresh():
    sock = FakeSocket()
    state = DemoState()
    streamer = FrameStreamer("127.0.0.1", 39000, sock=sock, full_refresh_interval=3)
    renderer = DisplayRenderer()

    first = streamer.render_and_send(renderer, state)
    streamer.handle_transport_status(0, streamer.transport_epoch)
    second = streamer.render_and_send(renderer, state)
    third = streamer.render_and_send(renderer, state)

    assert first.dirty_tiles == 108
    assert second.dirty_tiles == 0
    assert third.dirty_tiles == 108
    assert len(sock.sent) == 331


def test_frame_streamer_can_force_multiple_full_refresh_frames():
    state = DemoState()
    streamer = FrameStreamer("127.0.0.1", 39000, dry_run=True, full_refresh_interval=0)
    renderer = DisplayRenderer()

    first = streamer.render_and_send(renderer, state)
    second = streamer.render_and_send(renderer, state)
    streamer.request_full_refresh(frames=2)
    forced_first = streamer.render_and_send(renderer, state)
    forced_second = streamer.render_and_send(renderer, state)
    normal = streamer.render_and_send(renderer, state)

    assert first.dirty_tiles == 108
    assert second.dirty_tiles == 0
    assert forced_first.dirty_tiles == 108
    assert forced_second.dirty_tiles == 108
    assert normal.dirty_tiles == 0


def test_forced_full_refresh_repeats_and_rotates_tile_order():
    sock = FakeSocket()
    state = DemoState()
    streamer = FrameStreamer(
        "127.0.0.1",
        39000,
        sock=sock,
        full_refresh_interval=0,
        packet_gap_seconds=0,
    )

    streamer.render_and_send(DisplayRenderer(), state)
    streamer.render_and_send(DisplayRenderer(), state)
    streamer.request_full_refresh(frames=1)
    result = streamer.render_and_send(DisplayRenderer(), state)

    forced_payloads = [payload for payload, _ in sock.sent[-219:] if payload[3] != PKT_FRAME_END]
    first_pass = [TilePacket.decode(payload).tile_id for payload in forced_payloads[:108]]
    second_pass = [TilePacket.decode(payload).tile_id for payload in forced_payloads[108:]]

    assert result.dirty_tiles == 108
    assert len(forced_payloads) == 216
    assert len(set(first_pass)) == 108
    assert len(set(second_pass)) == 108
    assert first_pass[0] != second_pass[0]


def test_frame_streamer_sends_display_mode_when_toggled():
    sock = FakeSocket()
    state = DemoState()
    streamer = FrameStreamer("127.0.0.1", 39000, sock=sock, packet_gap_seconds=0)

    streamer.render_and_send(DisplayRenderer(), state)
    first_count = len(sock.sent)
    streamer.render_and_send(DisplayRenderer(), state)
    streamer.set_unified_flush(False)
    streamer.render_and_send(DisplayRenderer(), state)

    assert sock.sent[0][0][3] == 1
    assert len(sock.sent) == first_count + 1 + 216 + 3
    assert sock.sent[first_count][0][3] == 1
    assert sock.sent[first_count][0][-6:] == bytes([0, 1, 0, 100, 0, 0x02])


def test_frame_streamer_retransmits_cached_tiles_from_nack():
    sock = FakeSocket()
    state = DemoState()
    streamer = FrameStreamer("127.0.0.1", 39000, sock=sock, full_refresh_interval=0, packet_gap_seconds=0)

    result = streamer.render_and_send(DisplayRenderer(), state)
    sent_before_nack = len(sock.sent)
    streamer.handle_nack(result.frame_id, [0, 5, 107])
    retransmits = sock.sent[sent_before_nack:]

    assert len(retransmits) == 4
    assert [TilePacket.decode(payload).tile_id for payload, _ in retransmits[:-1]] == [0, 5, 107]
    assert retransmits[-1][0][3] == PKT_FRAME_END


def test_frame_streamer_ignores_nack_for_unknown_frame_or_tile():
    sock = FakeSocket()
    state = DemoState()
    streamer = FrameStreamer("127.0.0.1", 39000, sock=sock, packet_gap_seconds=0)

    streamer.render_and_send(DisplayRenderer(), state)
    sent_before_nack = len(sock.sent)
    streamer.handle_nack(999, [0, 1])
    streamer.handle_nack(1, [999])

    assert len(sock.sent) == sent_before_nack


def test_frame_streamer_can_disable_nack_retransmit():
    sock = FakeSocket()
    state = DemoState()
    streamer = FrameStreamer("127.0.0.1", 39000, sock=sock, packet_gap_seconds=0)

    result = streamer.render_and_send(DisplayRenderer(), state)
    sent_before_nack = len(sock.sent)
    streamer.set_transport_options(nack_enabled=False)
    streamer.handle_nack(result.frame_id, [0, 1])

    assert len(sock.sent) == sent_before_nack


def test_frame_streamer_can_skip_frame_end_after_nack_retransmit():
    sock = FakeSocket()
    state = DemoState()
    streamer = FrameStreamer("127.0.0.1", 39000, sock=sock, packet_gap_seconds=0)

    result = streamer.render_and_send(DisplayRenderer(), state)
    sent_before_nack = len(sock.sent)
    streamer.set_transport_options(resend_frame_end_on_nack=False)
    streamer.handle_nack(result.frame_id, [0, 1])
    retransmits = sock.sent[sent_before_nack:]

    assert len(retransmits) == 2
    assert all(payload[3] != PKT_FRAME_END for payload, _ in retransmits)


def test_frame_streamer_can_force_raw_compression_mode():
    sock = FakeSocket()
    state = DemoState()
    streamer = FrameStreamer("127.0.0.1", 39000, sock=sock, packet_gap_seconds=0)

    streamer.set_transport_options(compression_mode="raw")
    streamer.render_and_send(DisplayRenderer(), state)
    first_tile = next(payload for payload, _ in sock.sent if payload[3] == 2)

    assert TilePacket.decode(first_tile).compression == 0


def test_frame_streamer_can_force_lz4_compression_mode():
    sock = FakeSocket()
    state = DemoState()
    streamer = FrameStreamer("127.0.0.1", 39000, sock=sock, packet_gap_seconds=0)

    streamer.set_transport_options(compression_mode="lz4")
    streamer.render_and_send(DisplayRenderer(), state)
    first_tile = next(payload for payload, _ in sock.sent if payload[3] == 2)

    assert TilePacket.decode(first_tile).compression == 2


def test_frame_streamer_uses_runtime_tile_size_for_dirty_split():
    state = DemoState()
    streamer = FrameStreamer("127.0.0.1", 39000, dry_run=True, full_refresh_interval=0)
    renderer = DisplayRenderer()

    first = streamer.render_and_send(renderer, state)
    second = streamer.render_and_send(renderer, state)
    streamer.set_transport_options(tile_width=80, tile_height=8)
    resized = streamer.render_and_send(renderer, state)
    unchanged = streamer.render_and_send(renderer, state)

    assert first.dirty_tiles == 108
    assert second.dirty_tiles == 0
    assert resized.dirty_tiles == 120
    assert unchanged.dirty_tiles == 120


def test_frame_streamer_accepts_fast_video_pacing_options():
    sock = FakeSocket()
    sleeper = FakeSleeper()
    state = DemoState()
    streamer = FrameStreamer("127.0.0.1", 39000, sock=sock, sleep=sleeper)

    streamer.set_transport_options(
        tile_width=80,
        tile_height=8,
        packet_burst_size=32,
        packet_gap_seconds=0.0005,
        frame_end_repeats=1,
        full_refresh_tile_repeats=1,
        full_refresh_interval=0,
    )
    streamer.request_full_refresh(frames=1)
    result = streamer.render_and_send(DisplayRenderer(), state)

    assert result.dirty_tiles == 120
    assert streamer.packet_burst_size == 32
    assert streamer.packet_gap_seconds == 0.0005
    assert streamer.frame_end_repeats == 1
    assert streamer.full_refresh_tile_repeats == 1
    assert streamer.full_refresh_interval == 0
    assert len(sock.sent) == 122
    assert len(sleeper.calls) == 4
    assert sum(sleeper.calls) == 0.002


def test_frame_streamer_sends_jpeg_chunks_in_mjpeg_mode():
    sock = FakeSocket()
    state = DemoState(active_scene="video")
    streamer = FrameStreamer("127.0.0.1", 39000, sock=sock, packet_gap_seconds=0)
    streamer.set_transport_options(transport_mode="mjpeg")

    result = streamer.render_and_send(DisplayRenderer(), state)

    payloads = [payload for payload, _ in sock.sent]
    jpeg_payloads = [payload for payload in payloads if payload[3] == PKT_JPEG_CHUNK]
    assert result.dirty_tiles == len(jpeg_payloads)
    assert jpeg_payloads
    assert all(payload[3] != 2 for payload in payloads)
    chunks = [JpegChunkPacket.decode(payload) for payload in jpeg_payloads]
    assert chunks[0].payload.startswith(b"\xff\xd8")
    assert chunks[-1].payload.endswith(b"\xff\xd9")
    cached = streamer.latest_mjpeg_frame()
    assert cached is not None
    assert cached[0] == result.frame_id
    assert cached[1] == b"".join(chunk.payload for chunk in chunks)

    assert streamer.latest_displayed_mjpeg_frame() is None
    streamer.mark_displayed(result.frame_id)
    assert streamer.latest_displayed_mjpeg_frame() == cached


def test_indexed_pacing_waits_for_display_ack_when_three_frames_are_in_flight():
    streamer = FrameStreamer("127.0.0.1", 39000, dry_run=True)
    state = DemoState()
    state.link.esp32_online = True
    streamer.frame_id = 13
    streamer.mark_displayed(9)
    completed = threading.Event()

    thread = threading.Thread(target=lambda: (streamer._pace_indexed_frame(state), completed.set()))
    thread.start()
    assert not completed.wait(timeout=0.02)

    streamer.mark_displayed(10)
    assert completed.wait(timeout=0.2)
    thread.join(timeout=0.2)


def test_indexed_pacing_does_not_wait_without_online_display_ack():
    streamer = FrameStreamer("127.0.0.1", 39000, dry_run=True)
    state = DemoState()
    streamer.frame_id = 12
    streamer.mark_displayed(9)

    streamer._pace_indexed_frame(state)

    assert state.link.esp32_online is False


def test_frame_streamer_reuses_jpeg_encode_for_same_image_object(monkeypatch: pytest.MonkeyPatch):
    class StableRenderer:
        def __init__(self) -> None:
            self.image = Image.new("RGB", (240, 320), (4, 5, 6))

        def render(self, state: DemoState) -> Image.Image:
            return self.image

    calls = 0

    def fake_jpeg_bytes(image: Image.Image, quality: int = 60, subsampling: str = "420") -> bytes:
        nonlocal calls
        calls += 1
        return b"\xff\xd8cached-jpeg\xff\xd9"

    monkeypatch.setattr("espagent_demo.udp_link.image_to_jpeg_bytes", fake_jpeg_bytes)
    streamer = FrameStreamer("127.0.0.1", 39000, dry_run=True)
    streamer.set_transport_options(transport_mode="mjpeg")
    renderer = StableRenderer()

    first = streamer.render_and_send(renderer, DemoState(active_scene="link"))
    second = streamer.render_and_send(renderer, DemoState(active_scene="link"))

    assert first.frame_id == 1
    assert second.frame_id == 2
    assert calls == 1


def test_frame_streamer_passes_mjpeg_subsampling_to_encoder(monkeypatch: pytest.MonkeyPatch):
    class StableRenderer:
        def __init__(self) -> None:
            self.image = Image.new("RGB", (240, 320), (4, 5, 6))

        def render(self, state: DemoState) -> Image.Image:
            return self.image

    calls: list[tuple[int, str]] = []

    def fake_jpeg_bytes(image: Image.Image, quality: int = 60, subsampling: str = "420") -> bytes:
        calls.append((quality, subsampling))
        return b"\xff\xd8subsampled-jpeg\xff\xd9"

    monkeypatch.setattr("espagent_demo.udp_link.image_to_jpeg_bytes", fake_jpeg_bytes)
    streamer = FrameStreamer("127.0.0.1", 39000, dry_run=True)
    streamer.set_transport_options(transport_mode="mjpeg", mjpeg_quality=88, mjpeg_subsampling="444")

    streamer.render_and_send(StableRenderer(), DemoState(active_scene="link"))

    assert calls == [(88, "444")]


def test_frame_streamer_applies_mjpeg_quality_to_jpeg_size():
    class NoisyRenderer:
        def render(self, state: DemoState) -> Image.Image:
            image = Image.new("RGB", (240, 320))
            pixels = [((x * 17 + y * 3) % 256, (x * 5 + y * 11) % 256, (x * 13 + y * 7) % 256) for y in range(320) for x in range(240)]
            image.putdata(pixels)
            return image

    low_sock = FakeSocket()
    high_sock = FakeSocket()
    low = FrameStreamer("127.0.0.1", 39000, sock=low_sock, packet_gap_seconds=0)
    high = FrameStreamer("127.0.0.1", 39000, sock=high_sock, packet_gap_seconds=0)
    low.set_transport_options(transport_mode="mjpeg", mjpeg_quality=20)
    high.set_transport_options(transport_mode="mjpeg", mjpeg_quality=90)

    low.render_and_send(NoisyRenderer(), DemoState(active_scene="video"))
    high.render_and_send(NoisyRenderer(), DemoState(active_scene="video"))

    low_chunk = JpegChunkPacket.decode(next(payload for payload, _ in low_sock.sent if payload[3] == PKT_JPEG_CHUNK))
    high_chunk = JpegChunkPacket.decode(next(payload for payload, _ in high_sock.sent if payload[3] == PKT_JPEG_CHUNK))
    assert low_chunk.jpeg_len < high_chunk.jpeg_len


def test_frame_streamer_retransmits_cached_jpeg_chunks_from_nack():
    sock = FakeSocket()
    state = DemoState(active_scene="video")
    streamer = FrameStreamer("127.0.0.1", 39000, sock=sock, packet_gap_seconds=0)
    streamer.set_transport_options(transport_mode="mjpeg")

    result = streamer.render_and_send(DisplayRenderer(), state)
    sent_before_nack = len(sock.sent)
    streamer.handle_nack(result.frame_id, [0, 1])
    retransmits = sock.sent[sent_before_nack:]

    assert len(retransmits) == 2
    assert [JpegChunkPacket.decode(payload).chunk_id for payload, _ in retransmits] == [0, 1]
    assert all(payload[3] == PKT_JPEG_CHUNK for payload, _ in retransmits)


def test_frame_streamer_sends_nack_mode_control_in_mjpeg_mode():
    sock = FakeSocket()
    state = DemoState(active_scene="video")
    streamer = FrameStreamer("127.0.0.1", 39000, sock=sock, packet_gap_seconds=0)
    streamer.set_transport_options(transport_mode="mjpeg")

    streamer.render_and_send(DisplayRenderer(), state)
    streamer.set_transport_options(nack_enabled=False)
    streamer.render_and_send(DisplayRenderer(), state)

    mode_packets = [payload for payload, _ in sock.sent if payload[3] == 1]
    assert len(mode_packets) == 2
    assert mode_packets[0][-6:] == bytes([1, 1, 0, 100, 1, 0x02])
    assert mode_packets[1][-6:] == bytes([1, 0, 0, 100, 1, 0x02])


def test_frame_streamer_increments_transport_epoch_on_mode_switch():
    sock = FakeSocket()
    state = DemoState(active_scene="video")
    streamer = FrameStreamer("127.0.0.1", 39000, sock=sock, packet_gap_seconds=0)

    streamer.set_transport_options(transport_mode="mjpeg")
    streamer.render_and_send(DisplayRenderer(), state)
    first_epoch = int.from_bytes(sock.sent[0][0][4:8], "little")
    streamer.set_transport_options(transport_mode="indexed")
    before_second_frame = len(sock.sent)
    streamer.render_and_send(DisplayRenderer(), state)
    second_epoch = int.from_bytes(sock.sent[before_second_frame][0][4:8], "little")

    assert second_epoch == first_epoch + 1


def test_frame_streamer_switches_epoch_and_dimensions_for_landscape():
    sock = FakeSocket()
    state = DemoState(active_scene="test", test_pattern="checker")
    state.display_ui.orientation = "landscape"
    streamer = FrameStreamer("127.0.0.1", 39000, sock=sock, packet_gap_seconds=0)
    streamer.set_transport_options(transport_mode="indexed", indexed_bpp=2)
    initial_epoch = streamer.transport_epoch

    streamer.render_and_send(DisplayRenderer(), state)

    mode_packet = next(payload for payload, _ in sock.sent if payload[3] == 1)
    indexed_packet = next(
        IndexedFrameChunkPacket.decode(payload)
        for payload, _ in sock.sent
        if payload[3] == PKT_INDEXED_FRAME_CHUNK
    )
    assert mode_packet[-1] == 1
    assert streamer.transport_epoch == initial_epoch + 1
    assert (indexed_packet.width, indexed_packet.height) == (320, 240)


def test_frame_streamer_seeds_epoch_from_wall_clock(monkeypatch):
    monkeypatch.setattr("espagent_demo.udp_link.time.time_ns", lambda: 12_345_678_000_000)

    streamer = FrameStreamer("127.0.0.1", 39000, dry_run=True)

    assert streamer.transport_epoch == 12_345_678


def test_frame_streamer_retries_mode_control_until_acknowledged():
    sock = FakeSocket()
    now = [0.0]
    streamer = FrameStreamer("127.0.0.1", 39000, sock=sock, clock=lambda: now[0])
    streamer.set_transport_options(transport_mode="indexed")

    streamer._send_display_mode_if_needed()
    now[0] = 0.1
    streamer._send_display_mode_if_needed()
    now[0] = 0.21
    streamer._send_display_mode_if_needed()
    streamer.handle_transport_status(2, streamer.transport_epoch)
    now[0] = 0.5
    streamer._send_display_mode_if_needed()

    assert len(sock.sent) == 2


def test_frame_streamer_rebases_epoch_rejected_as_stale_by_firmware():
    streamer = FrameStreamer("127.0.0.1", 39000, sock=FakeSocket())
    streamer.transport_epoch = 0x97500001
    streamer._sent_display_mode = (True, True, False, 100, 2, streamer.transport_epoch, "1.8", "portrait")

    streamer.handle_transport_status(2, 0)

    assert streamer.transport_epoch == 1
    assert streamer._sent_display_mode is None
    assert streamer.force_full_refresh_frames >= 3


def test_frame_streamer_does_not_rebase_epoch_that_is_newer_than_firmware():
    streamer = FrameStreamer("127.0.0.1", 39000, dry_run=True)
    streamer.transport_epoch = 42

    streamer.handle_transport_status(2, 17)

    assert streamer.transport_epoch == 42
    assert streamer._acknowledged_transport_epoch == 17


def test_frame_streamer_resends_display_mode_after_forced_mjpeg_refresh():
    sock = FakeSocket()
    state = DemoState(active_scene="hermes")
    streamer = FrameStreamer("127.0.0.1", 39000, sock=sock, packet_gap_seconds=0)
    streamer.set_transport_options(transport_mode="mjpeg", nack_enabled=False)

    streamer.render_and_send(DisplayRenderer(), state)
    streamer.render_and_send(DisplayRenderer(), state)
    streamer.request_full_refresh()
    streamer.render_and_send(DisplayRenderer(), state)

    mode_packets = [payload for payload, _ in sock.sent if payload[3] == 1]
    assert len(mode_packets) == 2
    assert mode_packets[0][-6:] == bytes([1, 0, 0, 100, 1, 0x02])
    assert mode_packets[1][-6:] == bytes([1, 0, 0, 100, 1, 0x02])


def test_frame_streamer_sends_esp_log_mode_control():
    sock = FakeSocket()
    state = DemoState(active_scene="video")
    streamer = FrameStreamer("127.0.0.1", 39000, sock=sock, packet_gap_seconds=0)
    streamer.set_transport_options(transport_mode="mjpeg", esp_log_enabled=True)

    streamer.render_and_send(DisplayRenderer(), state)

    mode_packets = [payload for payload, _ in sock.sent if payload[3] == 1]
    assert len(mode_packets) == 1
    assert mode_packets[0][-6:] == bytes([1, 1, 1, 100, 1, 0x02])


def test_frame_streamer_sends_backlight_mode_control():
    sock = FakeSocket()
    state = DemoState(active_scene="video")
    streamer = FrameStreamer("127.0.0.1", 39000, sock=sock, packet_gap_seconds=0)
    streamer.set_transport_options(transport_mode="mjpeg", backlight_percent=35)

    streamer.render_and_send(DisplayRenderer(), state)

    mode_packets = [payload for payload, _ in sock.sent if payload[3] == 1]
    assert len(mode_packets) == 1
    assert mode_packets[0][-6:] == bytes([1, 1, 0, 35, 1, 0x02])


def test_frame_streamer_sends_selected_panel_gamma():
    sock = FakeSocket()
    streamer = FrameStreamer("127.0.0.1", 39000, sock=sock, packet_gap_seconds=0)
    streamer.set_transport_options(panel_gamma="2.2")

    streamer.render_and_send(DisplayRenderer(), DemoState())

    mode_packets = [payload for payload, _ in sock.sent if payload[3] == 1]
    assert mode_packets[0][-1] == 0x01


def test_frame_streamer_closes_socket_and_supports_context_manager():
    sock = FakeSocket()

    with FrameStreamer("127.0.0.1", 39000, sock=sock) as streamer:
        assert streamer.sock is sock

    assert sock.closed
