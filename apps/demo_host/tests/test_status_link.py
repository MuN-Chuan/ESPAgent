from espagent_demo.state import DemoState
from espagent_demo.status_link import StatusReceiver


class DiscoverySocket:
    def __init__(self) -> None:
        self.sent: list[tuple[bytes, tuple[str, int]]] = []

    def sendto(self, payload: bytes, address: tuple[str, int]) -> None:
        self.sent.append((payload, address))


def test_status_receiver_broadcasts_display_discovery():
    state = DemoState()
    sock = DiscoverySocket()
    receiver = StatusReceiver(
        39001,
        state,
        sock=sock,  # type: ignore[arg-type]
        discovery_targets_provider=lambda: [],
    )

    receiver._send_discovery(force=True)

    assert sock.sent == [(b"ESPAGENT_DISCOVER 1", ("255.255.255.255", 39000))]


def test_status_receiver_uses_directed_broadcasts_for_tun_networks():
    state = DemoState()
    sock = DiscoverySocket()
    receiver = StatusReceiver(
        39001,
        state,
        sock=sock,  # type: ignore[arg-type]
        discovery_targets_provider=lambda: ["192.168.1.255", "255.255.255.255"],
    )

    receiver._send_discovery(force=True)

    assert sock.sent == [
        (b"ESPAGENT_DISCOVER 1", ("255.255.255.255", 39000)),
        (b"ESPAGENT_DISCOVER 1", ("192.168.1.255", 39000)),
    ]


def test_status_receiver_parses_nack_tiles_and_calls_handler():
    state = DemoState()
    calls: list[tuple[int, list[int]]] = []
    receiver = StatusReceiver(39001, state, nack_handler=lambda frame_id, tile_ids: calls.append((frame_id, tile_ids)))

    receiver._handle_status(
        b"ESPAGENT_STATUS rx=10 accepted=9 rejected=0 draw_failed=0 nack_frame=7 nack_tiles=0,3,5",
        "192.168.1.7",
    )

    assert calls == [(7, [0, 3, 5])]
    assert state.link.rx_packets == 10


def test_status_receiver_parses_nack_chunks_and_calls_handler():
    state = DemoState()
    calls: list[tuple[int, list[int]]] = []
    receiver = StatusReceiver(39001, state, nack_handler=lambda frame_id, chunk_ids: calls.append((frame_id, chunk_ids)))

    receiver._handle_status(
        b"ESPAGENT_STATUS rx=18 accepted=15 rejected=0 draw_failed=0 nack_frame=11 nack_chunks=1,4",
        "192.168.1.7",
    )

    assert calls == [(11, [1, 4])]
    assert state.link.accepted_packets == 15


def test_status_receiver_publishes_displayed_frame():
    state = DemoState()
    displayed: list[int] = []
    receiver = StatusReceiver(39001, state, displayed_handler=displayed.append)

    receiver._handle_status(
        b"ESPAGENT_STATUS rx=20 accepted=20 rejected=0 draw_failed=0 displayed=42",
        "192.168.1.7",
    )

    assert displayed == [42]
    assert state.link.displayed_frame_id == 42


def test_status_receiver_tracks_active_transport_epoch():
    state = DemoState()
    transports: list[tuple[int, int]] = []
    receiver = StatusReceiver(39001, state, transport_handler=lambda mode, epoch: transports.append((mode, epoch)))

    receiver._handle_status(
        b"ESPAGENT_STATUS rx=1 accepted=1 rejected=0 draw_failed=0 mode=2 mode_epoch=17",
        "192.168.1.7",
    )

    assert state.link.esp_transport_mode == "indexed"
    assert state.link.esp_transport_epoch == 17
    assert transports == [(2, 17)]


def test_status_receiver_updates_stream_target_from_status_source():
    state = DemoState()
    targets: list[str] = []
    receiver = StatusReceiver(39001, state, target_handler=targets.append)

    receiver._handle_status(
        b"ESPAGENT_STATUS rx=0 accepted=0 rejected=0 draw_failed=0",
        "192.168.1.23",
    )

    assert targets == ["192.168.1.23"]
    assert state.link.esp32_ip == "192.168.1.23"


def test_status_receiver_uses_esp_performance_counters():
    state = DemoState()
    receiver = StatusReceiver(39001, state)

    receiver._handle_status(
        b"ESPAGENT_STATUS rx=500 accepted=500 rejected=0 draw_failed=0 displayed=80 "
        b"perf_seq=2 perf_ms=5012 perf_frames=49 lcd_fps_milli=9776 "
        b"decode_avg_us=12000 decode_max_us=14500 lcd_avg_us=18200 lcd_max_us=21000 "
        b"frame_avg_us=30200 frame_max_us=35500 jpeg_queued=90 jpeg_incomplete=7 jpeg_dropped=3 "
        b"worker_frame=79 worker_stage=0 indexed_queued=120 indexed_incomplete=5 indexed_dropped=4 "
        b"indexed_no_buffer=2 indexed_queue=1 indexed_worker_frame=80 indexed_worker_stage=3",
        "192.168.1.7",
    )

    assert state.link.display_fps == 9.776
    assert state.link.perf_window_ms == 5012
    assert state.link.perf_frames == 49
    assert state.link.decode_avg_us == 12000
    assert state.link.decode_max_us == 14500
    assert state.link.lcd_avg_us == 18200
    assert state.link.lcd_max_us == 21000
    assert state.link.frame_avg_us == 30200
    assert state.link.frame_max_us == 35500
    assert state.link.jpeg_queued_frames == 90
    assert state.link.jpeg_incomplete_frames == 7
    assert state.link.jpeg_dropped_frames == 3
    assert state.link.jpeg_worker_frame_id == 79
    assert state.link.jpeg_worker_stage == 0
    assert state.link.indexed_queued_frames == 120
    assert state.link.indexed_incomplete_frames == 5
    assert state.link.indexed_dropped_frames == 4
    assert state.link.indexed_no_buffer_frames == 2
    assert state.link.indexed_queue_depth == 1
    assert state.link.indexed_worker_frame_id == 80
    assert state.link.indexed_worker_stage == 3


def test_stale_status_marks_device_offline_and_disables_screen_control():
    state = DemoState()
    state.mark_status_seen("192.168.1.7")
    state.display_ui.keyboard_control_enabled = True
    last_seen = state.link.last_status_at

    assert last_seen is not None
    assert state.expire_status(2.0, now=last_seen + 1.9) is False
    assert state.link.esp32_online is True

    assert state.expire_status(2.0, now=last_seen + 2.1) is True
    assert state.link.esp32_online is False
    assert state.display_ui.keyboard_control_enabled is False
