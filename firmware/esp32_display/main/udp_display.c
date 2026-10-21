#include <errno.h>
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>

#include "esp_check.h"
#include "esp_heap_caps.h"
#include "esp_jpeg_dec.h"
#include "esp_log.h"
#include "esp_rom_crc.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/queue.h"
#include "freertos/semphr.h"
#include "freertos/task.h"
#include "lcd_display.h"
#include "lwip/inet.h"
#include "lwip/sockets.h"
#include "protocol.h"
#include "sdkconfig.h"
#include "udp_display.h"

#define UDP_RX_BUFFER_SIZE 2300
#define UDP_TILE_DECODE_BUFFER_SIZE (120 * 6 * 2)
#define DISPLAY_PORTRAIT_WIDTH 240
#define DISPLAY_PORTRAIT_HEIGHT 320
#define DISPLAY_LANDSCAPE_WIDTH 320
#define DISPLAY_LANDSCAPE_HEIGHT 240
#define DISPLAY_MAX_WIDTH DISPLAY_LANDSCAPE_WIDTH
#define DISPLAY_PIXEL_COUNT (DISPLAY_PORTRAIT_WIDTH * DISPLAY_PORTRAIT_HEIGHT)
#define UDP_INDEXED_FAST_RAW_BYTES (DISPLAY_PIXEL_COUNT * 2 / 8)
#define UDP_INDEXED_MAX_RAW_BYTES DISPLAY_PIXEL_COUNT
#define UDP_INDEXED_FRAME_MAX_BYTES (80 * 1024)
#define UDP_INDEXED_CHUNK_PAYLOAD_SIZE 1400
#define UDP_INDEXED_MAX_CHUNKS ((UDP_INDEXED_FRAME_MAX_BYTES + UDP_INDEXED_CHUNK_PAYLOAD_SIZE - 1) / UDP_INDEXED_CHUNK_PAYLOAD_SIZE)
#define UDP_INDEXED_CHUNK_BITMAP_BYTES ((UDP_INDEXED_MAX_CHUNKS + 7) / 8)
#define UDP_INDEXED_BLOCK_MAX_LINES 16
#define UDP_INDEXED_BLOCK_BUFFER_COUNT 2
#define UDP_INDEXED_BLOCK_BUFFER_SIZE (DISPLAY_MAX_WIDTH * UDP_INDEXED_BLOCK_MAX_LINES * sizeof(uint16_t))
#define UDP_INDEXED_FRAME_QUEUE_DEPTH 2
#define UDP_INDEXED_FRAME_BUFFER_COUNT (UDP_INDEXED_FRAME_QUEUE_DEPTH + 2)
#define UDP_INDEXED_RESULT_QUEUE_DEPTH 4
#define UDP_JPEG_CHUNK_PAYLOAD_SIZE 1400
#define UDP_JPEG_MAX_BYTES 65535
#define UDP_JPEG_MAX_CHUNKS ((UDP_JPEG_MAX_BYTES + UDP_JPEG_CHUNK_PAYLOAD_SIZE - 1) / UDP_JPEG_CHUNK_PAYLOAD_SIZE)
#define UDP_JPEG_CHUNK_BITMAP_BYTES ((UDP_JPEG_MAX_CHUNKS + 7) / 8)
#define UDP_JPEG_BLOCK_MAX_LINES 16
#define UDP_JPEG_BLOCK_BUFFER_COUNT 2
#define UDP_JPEG_LCD_MAX_IN_FLIGHT 1
#define UDP_JPEG_BLOCK_BUFFER_SIZE (DISPLAY_MAX_WIDTH * UDP_JPEG_BLOCK_MAX_LINES * sizeof(uint16_t))
#define UDP_JPEG_FRAME_BUFFER_COUNT 3
#define UDP_JPEG_FRAME_QUEUE_DEPTH 2
#define UDP_JPEG_RESULT_QUEUE_DEPTH 4
#define UDP_SOCKET_RX_BUFFER_BYTES (48 * 1024)
#define UDP_DISPLAY_TASK_STACK 6144
#define UDP_DISPLAY_TASK_PRIORITY 6
#define UDP_JPEG_WORKER_STACK 6144
#define UDP_JPEG_WORKER_PRIORITY 5
#define UDP_INDEXED_WORKER_STACK 6144
#define UDP_INDEXED_WORKER_PRIORITY 5
#define UDP_DISPLAY_TASK_CORE 0
#define UDP_JPEG_WORKER_CORE 1
#define UDP_INDEXED_WORKER_CORE 1
#define UDP_STATUS_INTERVAL_PACKETS 60
#define DISPLAY_PERF_WINDOW_US (5 * 1000 * 1000)
#define UDP_RESULT_POLL_TIMEOUT_MS 10
#define UDP_RX_COOPERATIVE_YIELD_INTERVAL_US (1000 * 1000)
#define UDP_FLUSH_IDLE_TIMEOUT_MS 20
#define UDP_MAX_FRAME_TILES 256
#define UDP_FRAME_TILE_BITMAP_BYTES (UDP_MAX_FRAME_TILES / 8)
#define STATUS_MARKER_REJECT 0xF800

static const char *TAG = "udp_display";
static uint32_t s_displayed_frame_id = 0;
static portMUX_TYPE s_display_perf_lock = portMUX_INITIALIZER_UNLOCKED;
static SemaphoreHandle_t s_display_mutex;
static volatile uint8_t s_active_transport_mode = ESPAGENT_TRANSPORT_INDEXED;
static volatile uint32_t s_transport_epoch;
static volatile uint8_t s_display_orientation = ESPAGENT_DISPLAY_PORTRAIT;
static volatile uint16_t s_display_width = DISPLAY_PORTRAIT_WIDTH;
static volatile uint16_t s_display_height = DISPLAY_PORTRAIT_HEIGHT;

#define DISPLAY_WIDTH s_display_width
#define DISPLAY_HEIGHT s_display_height

typedef struct {
    int64_t window_started_us;
    uint32_t frames;
    uint64_t decode_total_us;
    uint32_t decode_max_us;
    uint64_t lcd_total_us;
    uint32_t lcd_max_us;
    uint64_t frame_total_us;
    uint32_t frame_max_us;
    uint32_t report_sequence;
    uint32_t report_window_ms;
    uint32_t report_frames;
    uint32_t report_fps_milli;
    uint32_t report_decode_avg_us;
    uint32_t report_decode_max_us;
    uint32_t report_lcd_avg_us;
    uint32_t report_lcd_max_us;
    uint32_t report_frame_avg_us;
    uint32_t report_frame_max_us;
} display_perf_t;

static display_perf_t s_display_perf;

static void reset_display_perf(void)
{
    taskENTER_CRITICAL(&s_display_perf_lock);
    const uint32_t next_sequence = s_display_perf.report_sequence + 1;
    memset(&s_display_perf, 0, sizeof(s_display_perf));
    s_display_perf.report_sequence = next_sequence;
    taskEXIT_CRITICAL(&s_display_perf_lock);
}

static bool transport_is_active(uint8_t mode, uint32_t epoch)
{
    return s_active_transport_mode == mode && s_transport_epoch == epoch;
}

static bool transport_epoch_is_stale(uint32_t epoch)
{
    return epoch != s_transport_epoch && (int32_t)(epoch - s_transport_epoch) <= 0;
}

typedef struct {
    uint32_t frame_id;
    uint32_t transport_epoch;
    uint16_t width;
    uint16_t height;
    uint16_t jpeg_len;
    uint8_t *jpeg_buffer;
} jpeg_work_item_t;

typedef struct {
    uint32_t frame_id;
    uint32_t transport_epoch;
    esp_err_t result;
} jpeg_work_result_t;

typedef struct {
    QueueHandle_t free_buffers;
    QueueHandle_t complete_frames;
    QueueHandle_t results;
    jpeg_dec_handle_t decoder;
    TaskHandle_t worker_task;
    uint8_t *frame_buffers[UDP_JPEG_FRAME_BUFFER_COUNT];
    uint8_t *block_buffers[UDP_JPEG_BLOCK_BUFFER_COUNT];
} jpeg_pipeline_t;

static jpeg_pipeline_t s_jpeg_pipeline;
static uint32_t s_jpeg_queued_frames;
static uint32_t s_jpeg_incomplete_frames;
static uint32_t s_jpeg_dropped_frames;
static volatile uint32_t s_jpeg_worker_frame_id;
static volatile uint32_t s_jpeg_worker_stage;

typedef struct {
    bool valid;
    uint32_t frame_id;
    uint16_t min_x;
    uint16_t min_y;
    uint16_t max_x;
    uint16_t max_y;
    uint16_t received_tile_count;
    uint8_t received_tiles[UDP_FRAME_TILE_BITMAP_BYTES];
} dirty_region_t;

typedef struct {
    bool valid;
    uint32_t frame_id;
    uint16_t width;
    uint16_t height;
    uint16_t jpeg_len;
    uint16_t expected_chunks;
    uint16_t received_chunks_count;
    uint32_t received_bytes;
    uint16_t dropped_newer_chunks;
    uint8_t received_chunks[UDP_JPEG_CHUNK_BITMAP_BYTES];
} jpeg_frame_t;

typedef struct {
    bool valid;
    uint32_t frame_id;
    uint32_t frame_len;
    uint32_t raw_len;
    uint16_t expected_chunks;
    uint16_t received_chunks_count;
    uint8_t bpp;
    uint8_t received_chunks[UDP_INDEXED_CHUNK_BITMAP_BYTES];
} indexed_frame_t;

typedef struct {
    uint32_t frame_id;
    uint32_t transport_epoch;
    uint32_t frame_len;
    uint32_t raw_len;
    uint8_t bpp;
    uint8_t *frame_buffer;
} indexed_work_item_t;

typedef struct {
    uint32_t frame_id;
    uint32_t transport_epoch;
    esp_err_t result;
} indexed_work_result_t;

typedef struct {
    QueueHandle_t free_buffers;
    QueueHandle_t complete_frames;
    QueueHandle_t results;
    TaskHandle_t worker_task;
    uint8_t *frame_buffers[UDP_INDEXED_FRAME_BUFFER_COUNT];
    uint8_t *indices;
    uint8_t *extended_indices;
    uint8_t *block_buffers[UDP_INDEXED_BLOCK_BUFFER_COUNT];
} indexed_pipeline_t;

static indexed_pipeline_t s_indexed_pipeline;
static uint32_t s_indexed_queued_frames;
static uint32_t s_indexed_incomplete_frames;
static uint32_t s_indexed_dropped_frames;
static uint32_t s_indexed_no_buffer_frames;
static volatile uint32_t s_indexed_worker_frame_id;
static volatile uint32_t s_indexed_worker_stage;

static void record_display_perf(uint32_t decode_us, uint32_t lcd_us, uint32_t frame_us)
{
    const int64_t now_us = esp_timer_get_time();
    taskENTER_CRITICAL(&s_display_perf_lock);
    if (s_display_perf.window_started_us == 0) {
        s_display_perf.window_started_us = now_us;
        taskEXIT_CRITICAL(&s_display_perf_lock);
        return;
    }

    ++s_display_perf.frames;
    s_display_perf.decode_total_us += decode_us;
    s_display_perf.lcd_total_us += lcd_us;
    s_display_perf.frame_total_us += frame_us;
    if (decode_us > s_display_perf.decode_max_us) {
        s_display_perf.decode_max_us = decode_us;
    }
    if (lcd_us > s_display_perf.lcd_max_us) {
        s_display_perf.lcd_max_us = lcd_us;
    }
    if (frame_us > s_display_perf.frame_max_us) {
        s_display_perf.frame_max_us = frame_us;
    }

    const int64_t elapsed_us = now_us - s_display_perf.window_started_us;
    if (elapsed_us < DISPLAY_PERF_WINDOW_US || s_display_perf.frames == 0) {
        taskEXIT_CRITICAL(&s_display_perf_lock);
        return;
    }

    s_display_perf.report_window_ms = (uint32_t)(elapsed_us / 1000);
    s_display_perf.report_frames = s_display_perf.frames;
    s_display_perf.report_fps_milli = (uint32_t)(((uint64_t)s_display_perf.frames * 1000000000ULL) / (uint64_t)elapsed_us);
    s_display_perf.report_decode_avg_us = (uint32_t)(s_display_perf.decode_total_us / s_display_perf.frames);
    s_display_perf.report_decode_max_us = s_display_perf.decode_max_us;
    s_display_perf.report_lcd_avg_us = (uint32_t)(s_display_perf.lcd_total_us / s_display_perf.frames);
    s_display_perf.report_lcd_max_us = s_display_perf.lcd_max_us;
    s_display_perf.report_frame_avg_us = (uint32_t)(s_display_perf.frame_total_us / s_display_perf.frames);
    s_display_perf.report_frame_max_us = s_display_perf.frame_max_us;
    ++s_display_perf.report_sequence;

    s_display_perf.window_started_us = now_us;
    s_display_perf.frames = 0;
    s_display_perf.decode_total_us = 0;
    s_display_perf.decode_max_us = 0;
    s_display_perf.lcd_total_us = 0;
    s_display_perf.lcd_max_us = 0;
    s_display_perf.frame_total_us = 0;
    s_display_perf.frame_max_us = 0;
    taskEXIT_CRITICAL(&s_display_perf_lock);
}

static uint16_t read_le16(uint16_t value)
{
    return value;
}

static uint32_t read_le32(uint32_t value)
{
    return value;
}

static uint32_t crc32_ieee(const uint8_t *data, size_t len)
{
    return esp_rom_crc32_le(0, data, len);
}

static void send_status(
    int sock,
    const struct sockaddr_storage *source_addr,
    uint32_t rx_count,
    uint32_t accepted_count,
    uint32_t rejected_count,
    uint32_t draw_failed_count,
    uint32_t frame_end_count,
    uint32_t flushed_frame_count,
    uint32_t stale_frame_end_count)
{
    if (source_addr->ss_family != AF_INET) {
        return;
    }

    struct sockaddr_in status_addr = *(const struct sockaddr_in *)source_addr;
    status_addr.sin_port = htons(CONFIG_ESPAGENT_STATUS_PORT);

    display_perf_t perf_snapshot;
    taskENTER_CRITICAL(&s_display_perf_lock);
    perf_snapshot = s_display_perf;
    taskEXIT_CRITICAL(&s_display_perf_lock);

    char status[768];
    const int len = snprintf(
        status,
        sizeof(status),
        "ESPAGENT_STATUS rx=%lu accepted=%lu rejected=%lu draw_failed=%lu frame_end=%lu flushed=%lu stale_end=%lu displayed=%lu mode=%u mode_epoch=%lu perf_seq=%lu perf_ms=%lu perf_frames=%lu lcd_fps_milli=%lu decode_avg_us=%lu decode_max_us=%lu lcd_avg_us=%lu lcd_max_us=%lu frame_avg_us=%lu frame_max_us=%lu jpeg_queued=%lu jpeg_incomplete=%lu jpeg_dropped=%lu worker_frame=%lu worker_stage=%lu indexed_queued=%lu indexed_incomplete=%lu indexed_dropped=%lu indexed_no_buffer=%lu indexed_queue=%u indexed_worker_frame=%lu indexed_worker_stage=%lu",
        rx_count,
        accepted_count,
        rejected_count,
        draw_failed_count,
        frame_end_count,
        flushed_frame_count,
        stale_frame_end_count,
        s_displayed_frame_id,
        s_active_transport_mode,
        s_transport_epoch,
        perf_snapshot.report_sequence,
        perf_snapshot.report_window_ms,
        perf_snapshot.report_frames,
        perf_snapshot.report_fps_milli,
        perf_snapshot.report_decode_avg_us,
        perf_snapshot.report_decode_max_us,
        perf_snapshot.report_lcd_avg_us,
        perf_snapshot.report_lcd_max_us,
        perf_snapshot.report_frame_avg_us,
        perf_snapshot.report_frame_max_us,
        s_jpeg_queued_frames,
        s_jpeg_incomplete_frames,
        s_jpeg_dropped_frames,
        s_jpeg_worker_frame_id,
        s_jpeg_worker_stage,
        s_indexed_queued_frames,
        s_indexed_incomplete_frames,
        s_indexed_dropped_frames,
        s_indexed_no_buffer_frames,
        (unsigned int)uxQueueMessagesWaiting(s_indexed_pipeline.complete_frames),
        s_indexed_worker_frame_id,
        s_indexed_worker_stage);
    if (len <= 0 || len >= (int)sizeof(status)) {
        return;
    }

    const int sent = sendto(sock, status, len, 0, (const struct sockaddr *)&status_addr, sizeof(status_addr));
    if (sent < 0) {
        ESP_LOGW(TAG, "send status failed errno=%d", errno);
    }
}

static void send_display_ack(int sock, const struct sockaddr_storage *source_addr)
{
    if (source_addr->ss_family != AF_INET) {
        return;
    }
    struct sockaddr_in status_addr = *(const struct sockaddr_in *)source_addr;
    status_addr.sin_port = htons(CONFIG_ESPAGENT_STATUS_PORT);
    char status[64];
    const int len = snprintf(status, sizeof(status), "ESPAGENT_STATUS displayed=%lu", s_displayed_frame_id);
    if (len <= 0 || len >= (int)sizeof(status)) {
        return;
    }
    if (sendto(sock, status, len, 0, (const struct sockaddr *)&status_addr, sizeof(status_addr)) < 0) {
        ESP_LOGW(TAG, "send display ack failed errno=%d", errno);
    }
}

static void send_nack_status(
    int sock,
    const struct sockaddr_storage *source_addr,
    uint32_t rx_count,
    uint32_t accepted_count,
    uint32_t rejected_count,
    uint32_t draw_failed_count,
    uint32_t frame_end_count,
    uint32_t flushed_frame_count,
    uint32_t stale_frame_end_count,
    uint32_t frame_id,
    const dirty_region_t *dirty,
    uint16_t expected_tile_count)
{
    if (source_addr->ss_family != AF_INET) {
        return;
    }

    struct sockaddr_in status_addr = *(const struct sockaddr_in *)source_addr;
    status_addr.sin_port = htons(CONFIG_ESPAGENT_STATUS_PORT);

    char missing[384] = {0};
    size_t offset = 0;
    for (uint16_t tile_id = 0; tile_id < expected_tile_count && tile_id < UDP_MAX_FRAME_TILES; ++tile_id) {
        const uint8_t mask = (uint8_t)(1u << (tile_id & 7u));
        if ((dirty->received_tiles[tile_id >> 3] & mask) != 0) {
            continue;
        }
        const int written = snprintf(&missing[offset], sizeof(missing) - offset, "%s%u", offset == 0 ? "" : ",", tile_id);
        if (written <= 0 || written >= (int)(sizeof(missing) - offset)) {
            break;
        }
        offset += (size_t)written;
    }
    if (offset == 0) {
        return;
    }

    char status[640];
    const int len = snprintf(
        status,
        sizeof(status),
        "ESPAGENT_STATUS rx=%lu accepted=%lu rejected=%lu draw_failed=%lu frame_end=%lu flushed=%lu stale_end=%lu displayed=%lu nack_frame=%lu nack_tiles=%s",
        rx_count,
        accepted_count,
        rejected_count,
        draw_failed_count,
        frame_end_count,
        flushed_frame_count,
        stale_frame_end_count,
        s_displayed_frame_id,
        frame_id,
        missing);
    if (len <= 0 || len >= (int)sizeof(status)) {
        return;
    }

    const int sent = sendto(sock, status, len, 0, (const struct sockaddr *)&status_addr, sizeof(status_addr));
    if (sent < 0) {
        ESP_LOGW(TAG, "send nack failed errno=%d", errno);
    }
}

static void send_jpeg_nack_status(
    int sock,
    const struct sockaddr_storage *source_addr,
    uint32_t rx_count,
    uint32_t accepted_count,
    uint32_t rejected_count,
    uint32_t draw_failed_count,
    uint32_t frame_end_count,
    uint32_t flushed_frame_count,
    uint32_t stale_frame_end_count,
    const jpeg_frame_t *jpeg)
{
    if (source_addr->ss_family != AF_INET || !jpeg->valid || jpeg->expected_chunks == 0) {
        return;
    }

    struct sockaddr_in status_addr = *(const struct sockaddr_in *)source_addr;
    status_addr.sin_port = htons(CONFIG_ESPAGENT_STATUS_PORT);

    char missing[384] = {0};
    size_t offset = 0;
    for (uint16_t chunk_id = 0; chunk_id < jpeg->expected_chunks && chunk_id < UDP_JPEG_MAX_CHUNKS; ++chunk_id) {
        const uint8_t mask = (uint8_t)(1u << (chunk_id & 7u));
        if ((jpeg->received_chunks[chunk_id >> 3] & mask) != 0) {
            continue;
        }
        const int written = snprintf(&missing[offset], sizeof(missing) - offset, "%s%u", offset == 0 ? "" : ",", chunk_id);
        if (written <= 0 || written >= (int)(sizeof(missing) - offset)) {
            break;
        }
        offset += (size_t)written;
    }
    if (offset == 0) {
        return;
    }

    char status[640];
    const int len = snprintf(
        status,
        sizeof(status),
        "ESPAGENT_STATUS rx=%lu accepted=%lu rejected=%lu draw_failed=%lu frame_end=%lu flushed=%lu stale_end=%lu displayed=%lu nack_frame=%lu nack_chunks=%s",
        rx_count,
        accepted_count,
        rejected_count,
        draw_failed_count,
        frame_end_count,
        flushed_frame_count,
        stale_frame_end_count,
        s_displayed_frame_id,
        jpeg->frame_id,
        missing);
    if (len <= 0 || len >= (int)sizeof(status)) {
        return;
    }

    const int sent = sendto(sock, status, len, 0, (const struct sockaddr *)&status_addr, sizeof(status_addr));
    if (sent < 0) {
        ESP_LOGW(TAG, "send jpeg nack failed errno=%d", errno);
    }
}

static bool validate_tile_packet(const uint8_t *buffer, int len, const tile_packet_header_t **out_header, const uint8_t **out_payload)
{
    if (len < (int)sizeof(tile_packet_header_t)) {
        ESP_LOGW(TAG, "reject short packet len=%d", len);
        return false;
    }

    const tile_packet_header_t *header = (const tile_packet_header_t *)buffer;
    const uint16_t payload_len = read_le16(header->payload_len);
    const uint16_t raw_len = read_le16(header->raw_len);
    if (len != (int)(sizeof(tile_packet_header_t) + payload_len)) {
        ESP_LOGW(TAG, "reject length mismatch len=%d payload=%u", len, payload_len);
        return false;
    }

    if (read_le16(header->magic) != ESPAGENT_MAGIC_DISPLAY) {
        ESP_LOGW(TAG, "reject magic=0x%04x", read_le16(header->magic));
        return false;
    }
    if (header->version != ESPAGENT_PROTOCOL_VERSION) {
        ESP_LOGW(TAG, "reject version=%u", header->version);
        return false;
    }
    if (header->type != PKT_TILE) {
        ESP_LOGW(TAG, "reject packet type=%u", header->type);
        return false;
    }
    if (header->pixel_format != ESPAGENT_PIXEL_RGB565) {
        ESP_LOGW(TAG, "reject pixel format=%u", header->pixel_format);
        return false;
    }
    if (header->compression != ESPAGENT_COMP_RAW && header->compression != ESPAGENT_COMP_RLE && header->compression != ESPAGENT_COMP_LZ4) {
        ESP_LOGW(TAG, "reject compression=%u", header->compression);
        return false;
    }
    if (header->compression == ESPAGENT_COMP_RAW && payload_len != raw_len) {
        ESP_LOGW(TAG, "reject raw_len=%u payload_len=%u", raw_len, payload_len);
        return false;
    }

    const uint16_t x = read_le16(header->x);
    const uint16_t y = read_le16(header->y);
    const uint16_t w = read_le16(header->w);
    const uint16_t h = read_le16(header->h);
    if (w == 0 || h == 0 || x >= DISPLAY_WIDTH || y >= DISPLAY_HEIGHT || x + w > DISPLAY_WIDTH || y + h > DISPLAY_HEIGHT) {
        ESP_LOGW(TAG, "reject tile bounds x=%u y=%u w=%u h=%u", x, y, w, h);
        return false;
    }
    if (raw_len != (uint16_t)(w * h * 2)) {
        ESP_LOGW(TAG, "reject tile raw size raw=%u expected=%u", raw_len, (unsigned)(w * h * 2));
        return false;
    }

    const uint8_t *payload = buffer + sizeof(tile_packet_header_t);
    const uint32_t expected_crc = read_le32(header->crc32);
    const uint32_t actual_crc = crc32_ieee(payload, payload_len);
    if (actual_crc != expected_crc) {
        ESP_LOGW(TAG, "reject crc expected=0x%08lx actual=0x%08lx", expected_crc, actual_crc);
        return false;
    }

    *out_header = header;
    *out_payload = payload;
    return true;
}

static bool validate_frame_end_packet(const uint8_t *buffer, int len, const tile_packet_header_t **out_header, uint16_t *out_tile_count)
{
    if (len != (int)(sizeof(tile_packet_header_t) + sizeof(uint16_t))) {
        ESP_LOGW(TAG, "reject frame end length len=%d", len);
        return false;
    }

    const tile_packet_header_t *header = (const tile_packet_header_t *)buffer;
    const uint8_t *payload = buffer + sizeof(tile_packet_header_t);
    if (read_le16(header->magic) != ESPAGENT_MAGIC_DISPLAY) {
        ESP_LOGW(TAG, "reject frame end magic=0x%04x", read_le16(header->magic));
        return false;
    }
    if (header->version != ESPAGENT_PROTOCOL_VERSION) {
        ESP_LOGW(TAG, "reject frame end version=%u", header->version);
        return false;
    }
    if (header->type != PKT_FRAME_END) {
        ESP_LOGW(TAG, "reject frame end packet type=%u", header->type);
        return false;
    }
    if (read_le16(header->payload_len) != sizeof(uint16_t) || read_le16(header->raw_len) != sizeof(uint16_t)) {
        ESP_LOGW(TAG, "reject frame end payload metadata");
        return false;
    }
    if (crc32_ieee(payload, sizeof(uint16_t)) != read_le32(header->crc32)) {
        ESP_LOGW(TAG, "reject frame end crc");
        return false;
    }

    *out_header = header;
    *out_tile_count = (uint16_t)payload[0] | ((uint16_t)payload[1] << 8);
    return true;
}

static bool validate_jpeg_chunk_packet(const uint8_t *buffer, int len, const tile_packet_header_t **out_header, const uint8_t **out_payload)
{
    if (len < (int)sizeof(tile_packet_header_t)) {
        ESP_LOGW(TAG, "reject short jpeg packet len=%d", len);
        return false;
    }

    const tile_packet_header_t *header = (const tile_packet_header_t *)buffer;
    const uint16_t payload_len = read_le16(header->payload_len);
    const uint16_t jpeg_len = read_le16(header->raw_len);
    if (len != (int)(sizeof(tile_packet_header_t) + payload_len)) {
        ESP_LOGW(TAG, "reject jpeg length mismatch len=%d payload=%u", len, payload_len);
        return false;
    }
    if (read_le16(header->magic) != ESPAGENT_MAGIC_DISPLAY || header->version != ESPAGENT_PROTOCOL_VERSION) {
        ESP_LOGW(TAG, "reject jpeg protocol metadata");
        return false;
    }
    if (header->type != PKT_JPEG_CHUNK) {
        ESP_LOGW(TAG, "reject jpeg packet type=%u", header->type);
        return false;
    }
    if (header->pixel_format != ESPAGENT_PIXEL_JPEG || header->compression != ESPAGENT_COMP_RAW) {
        ESP_LOGW(TAG, "reject jpeg format=%u compression=%u", header->pixel_format, header->compression);
        return false;
    }
    if (jpeg_len == 0 || payload_len == 0 || payload_len > UDP_JPEG_CHUNK_PAYLOAD_SIZE) {
        ESP_LOGW(TAG, "reject jpeg sizes raw=%u payload=%u", jpeg_len, payload_len);
        return false;
    }
    const uint16_t width = read_le16(header->w);
    const uint16_t height = read_le16(header->h);
    if (width != DISPLAY_WIDTH || height != DISPLAY_HEIGHT) {
        ESP_LOGW(TAG, "reject jpeg dimensions w=%u h=%u", width, height);
        return false;
    }
    const uint16_t chunk_id = read_le16(header->tile_id);
    const uint32_t offset = (uint32_t)chunk_id * UDP_JPEG_CHUNK_PAYLOAD_SIZE;
    if (chunk_id >= UDP_JPEG_MAX_CHUNKS || offset >= jpeg_len || offset + payload_len > jpeg_len) {
        ESP_LOGW(TAG, "reject jpeg chunk=%u offset=%lu raw=%u payload=%u", chunk_id, offset, jpeg_len, payload_len);
        return false;
    }

    const uint8_t *payload = buffer + sizeof(tile_packet_header_t);
    const uint32_t expected_crc = read_le32(header->crc32);
    const uint32_t actual_crc = crc32_ieee(payload, payload_len);
    if (actual_crc != expected_crc) {
        ESP_LOGW(TAG, "reject jpeg crc expected=0x%08lx actual=0x%08lx", expected_crc, actual_crc);
        return false;
    }

    *out_header = header;
    *out_payload = payload;
    return true;
}

static bool decode_rle_rgb565(const uint8_t *payload, uint16_t payload_len, uint8_t *decoded, uint16_t raw_len)
{
    if ((payload_len % 3) != 0) {
        return false;
    }
    uint16_t out = 0;
    for (uint16_t index = 0; index < payload_len; index += 3) {
        const uint8_t run = payload[index];
        if (run == 0) {
            return false;
        }
        const uint8_t low = payload[index + 1];
        const uint8_t high = payload[index + 2];
        for (uint8_t count = 0; count < run; ++count) {
            if (out + 2 > raw_len) {
                return false;
            }
            decoded[out++] = low;
            decoded[out++] = high;
        }
    }
    return out == raw_len;
}

static bool read_lz4_extended_len(const uint8_t *payload, uint32_t payload_len, uint32_t *index, uint32_t *value)
{
    uint32_t total = 0;
    while (true) {
        if (*index >= payload_len) {
            return false;
        }
        const uint8_t item = payload[*index];
        (*index)++;
        if (UINT32_MAX - total < item) {
            return false;
        }
        total += item;
        if (item != 255) {
            *value = total;
            return true;
        }
    }
}

static bool decode_lz4_block(const uint8_t *payload, uint32_t payload_len, uint8_t *decoded, uint32_t raw_len)
{
    uint32_t index = 0;
    uint32_t out = 0;
    while (index < payload_len) {
        const uint8_t token = payload[index++];
        uint32_t literal_len = (uint32_t)(token >> 4);
        if (literal_len == 15) {
            uint32_t extra = 0;
            if (!read_lz4_extended_len(payload, payload_len, &index, &extra)) {
                return false;
            }
            if (UINT32_MAX - literal_len < extra) {
                return false;
            }
            literal_len += extra;
        }
        if (payload_len - index < literal_len || raw_len - out < literal_len) {
            return false;
        }
        memcpy(&decoded[out], &payload[index], literal_len);
        index += literal_len;
        out += literal_len;
        if (index == payload_len) {
            break;
        }
        if (payload_len - index < 2) {
            return false;
        }
        const uint16_t offset = (uint16_t)payload[index] | ((uint16_t)payload[index + 1] << 8);
        index += 2;
        if (offset == 0 || offset > out) {
            return false;
        }
        uint32_t match_len = (uint32_t)(token & 0x0Fu);
        if (match_len == 15) {
            uint32_t extra = 0;
            if (!read_lz4_extended_len(payload, payload_len, &index, &extra)) {
                return false;
            }
            if (UINT32_MAX - match_len < extra) {
                return false;
            }
            match_len += extra;
        }
        if (UINT32_MAX - match_len < 4) {
            return false;
        }
        match_len += 4;
        if (raw_len - out < match_len) {
            return false;
        }
        for (uint32_t count = 0; count < match_len; ++count) {
            decoded[out] = decoded[out - offset];
            out++;
        }
    }
    return out == raw_len;
}

static bool decode_tile_payload(const tile_packet_header_t *header, const uint8_t *payload, uint8_t *decoded)
{
    const uint16_t raw_len = read_le16(header->raw_len);
    const uint16_t payload_len = read_le16(header->payload_len);
    if (raw_len > UDP_TILE_DECODE_BUFFER_SIZE) {
        return false;
    }
    if (header->compression == ESPAGENT_COMP_RAW) {
        memcpy(decoded, payload, raw_len);
        return true;
    }
    if (header->compression == ESPAGENT_COMP_RLE) {
        return decode_rle_rgb565(payload, payload_len, decoded, raw_len);
    }
    if (header->compression == ESPAGENT_COMP_LZ4) {
        return decode_lz4_block(payload, payload_len, decoded, raw_len);
    }
    return false;
}

static bool validate_display_mode_packet(
    const uint8_t *buffer,
    int len,
    bool *out_unified_flush,
    bool *out_nack_enabled,
    bool *out_esp_log_enabled,
    uint8_t *out_backlight_percent,
    uint8_t *out_panel_gamma,
    uint8_t *out_transport_mode,
    uint32_t *out_transport_epoch,
    uint8_t *out_orientation)
{
    if (len < (int)(sizeof(tile_packet_header_t) + 1) || len > (int)(sizeof(tile_packet_header_t) + 7)) {
        ESP_LOGW(TAG, "reject display mode length len=%d", len);
        return false;
    }

    const tile_packet_header_t *header = (const tile_packet_header_t *)buffer;
    const uint8_t *payload = buffer + sizeof(tile_packet_header_t);
    if (read_le16(header->magic) != ESPAGENT_MAGIC_DISPLAY) {
        ESP_LOGW(TAG, "reject display mode magic=0x%04x", read_le16(header->magic));
        return false;
    }
    if (header->version != ESPAGENT_PROTOCOL_VERSION || header->type != PKT_FRAME_BEGIN) {
        ESP_LOGW(TAG, "reject display mode type=%u version=%u", header->type, header->version);
        return false;
    }
    const uint16_t payload_len = read_le16(header->payload_len);
    if ((payload_len < 1 || payload_len > 7) || read_le16(header->raw_len) != payload_len) {
        ESP_LOGW(TAG, "reject display mode payload metadata");
        return false;
    }
    if (crc32_ieee(payload, payload_len) != read_le32(header->crc32)) {
        ESP_LOGW(TAG, "reject display mode crc");
        return false;
    }

    *out_unified_flush = payload[0] != 0;
    *out_nack_enabled = payload_len < 2 || payload[1] != 0;
    *out_esp_log_enabled = payload_len >= 3 && payload[2] != 0;
    *out_backlight_percent = payload_len >= 4 && payload[3] <= 100 ? payload[3] : 100;
    *out_transport_mode = payload_len >= 5 ? payload[4] : s_active_transport_mode;
    *out_panel_gamma = payload_len >= 6 ? payload[5] : LCD_GAMMA_22;
    *out_transport_epoch = payload_len >= 5 ? read_le32(header->frame_id) : s_transport_epoch;
    *out_orientation = payload_len >= 7 ? payload[6] : ESPAGENT_DISPLAY_PORTRAIT;
    if (*out_transport_mode > ESPAGENT_TRANSPORT_INDEXED) {
        ESP_LOGW(TAG, "reject display mode transport=%u", *out_transport_mode);
        return false;
    }
    if (*out_panel_gamma != LCD_GAMMA_22 && *out_panel_gamma != LCD_GAMMA_18 &&
        *out_panel_gamma != LCD_GAMMA_25 && *out_panel_gamma != LCD_GAMMA_10) {
        ESP_LOGW(TAG, "reject panel gamma=0x%02x", *out_panel_gamma);
        return false;
    }
    if (*out_orientation > ESPAGENT_DISPLAY_LANDSCAPE) {
        ESP_LOGW(TAG, "reject display orientation=%u", *out_orientation);
        return false;
    }
    return true;
}

static bool validate_indexed_frame_chunk(
    const uint8_t *buffer,
    int len,
    const tile_packet_header_t **out_header,
    const uint8_t **out_payload,
    uint8_t *out_bpp)
{
    if (len < (int)sizeof(tile_packet_header_t)) {
        return false;
    }
    const tile_packet_header_t *header = (const tile_packet_header_t *)buffer;
    const uint16_t payload_len = read_le16(header->payload_len);
    const uint16_t chunk_id = read_le16(header->tile_id);
    const uint16_t wire_raw_len = read_le16(header->raw_len);
    uint8_t bpp = header->pixel_format == ESPAGENT_PIXEL_INDEXED1 ? 1 :
        (header->pixel_format == ESPAGENT_PIXEL_INDEXED2 ? 2 :
        (header->pixel_format == ESPAGENT_PIXEL_INDEXED4 ? 4 :
        (header->pixel_format == ESPAGENT_PIXEL_INDEXED8 ? 8 : 0)));
    const uint32_t frame_len = bpp == 8 ?
        (uint32_t)read_le16(header->x) | ((uint32_t)read_le16(header->y) << 16) :
        read_le16(header->x);
    const uint16_t chunk_count = bpp == 8 ? read_le16(header->w) : read_le16(header->y);
    const bool dimensions_valid = bpp == 8 ? read_le16(header->h) == 0 :
        read_le16(header->w) == DISPLAY_WIDTH && read_le16(header->h) == DISPLAY_HEIGHT;
    const uint32_t expected_raw_len = (uint32_t)DISPLAY_WIDTH * DISPLAY_HEIGHT * bpp / 8;
    const uint32_t raw_len = wire_raw_len == 0 && expected_raw_len > UINT16_MAX ?
        expected_raw_len : wire_raw_len;
    if (read_le16(header->magic) != ESPAGENT_MAGIC_DISPLAY || header->version != ESPAGENT_PROTOCOL_VERSION ||
        header->type != PKT_INDEXED_FRAME_CHUNK || bpp == 0 || header->compression != ESPAGENT_COMP_LZ4 ||
        !dimensions_valid ||
        len != (int)(sizeof(tile_packet_header_t) + payload_len) || payload_len == 0 ||
        payload_len > UDP_INDEXED_CHUNK_PAYLOAD_SIZE || frame_len == 0 || frame_len > UDP_INDEXED_FRAME_MAX_BYTES ||
        chunk_count == 0 || chunk_count > UDP_INDEXED_MAX_CHUNKS || chunk_id >= chunk_count ||
        (uint32_t)chunk_id * UDP_INDEXED_CHUNK_PAYLOAD_SIZE + payload_len > frame_len ||
        raw_len != expected_raw_len) {
        return false;
    }
    const uint8_t *payload = buffer + sizeof(tile_packet_header_t);
    if (crc32_ieee(payload, payload_len) != read_le32(header->crc32)) {
        return false;
    }
    *out_header = header;
    *out_payload = payload;
    *out_bpp = bpp;
    return true;
}

static void reset_indexed_frame(indexed_frame_t *frame, const tile_packet_header_t *header, uint8_t bpp)
{
    memset(frame, 0, sizeof(*frame));
    frame->valid = true;
    frame->frame_id = read_le32(header->frame_id);
    frame->frame_len = bpp == 8 ?
        (uint32_t)read_le16(header->x) | ((uint32_t)read_le16(header->y) << 16) :
        read_le16(header->x);
    frame->expected_chunks = bpp == 8 ? read_le16(header->w) : read_le16(header->y);
    const uint16_t wire_raw_len = read_le16(header->raw_len);
    const uint32_t expected_raw_len = (uint32_t)DISPLAY_WIDTH * DISPLAY_HEIGHT * bpp / 8;
    frame->raw_len = wire_raw_len == 0 && expected_raw_len > UINT16_MAX ? expected_raw_len : wire_raw_len;
    frame->bpp = bpp;
}

static bool assemble_indexed_frame_chunk(
    indexed_frame_t *frame,
    const tile_packet_header_t *header,
    const uint8_t *payload,
    uint8_t bpp,
    uint8_t *frame_buffer)
{
    const uint32_t frame_id = read_le32(header->frame_id);
    if (!frame->valid || frame->frame_id != frame_id) {
        reset_indexed_frame(frame, header, bpp);
    }
    const uint16_t wire_raw_len = read_le16(header->raw_len);
    const uint32_t expected_raw_len = (uint32_t)DISPLAY_WIDTH * DISPLAY_HEIGHT * bpp / 8;
    const uint32_t raw_len = wire_raw_len == 0 && expected_raw_len > UINT16_MAX ?
        expected_raw_len : wire_raw_len;
    const uint32_t frame_len = bpp == 8 ?
        (uint32_t)read_le16(header->x) | ((uint32_t)read_le16(header->y) << 16) :
        read_le16(header->x);
    const uint16_t chunk_count = bpp == 8 ? read_le16(header->w) : read_le16(header->y);
    if (frame->frame_len != frame_len || frame->expected_chunks != chunk_count ||
        frame->raw_len != raw_len || frame->bpp != bpp) {
        return false;
    }
    const uint16_t chunk_id = read_le16(header->tile_id);
    const uint8_t bit = (uint8_t)(1u << (chunk_id & 7));
    if ((frame->received_chunks[chunk_id >> 3] & bit) == 0) {
        memcpy(frame_buffer + (uint32_t)chunk_id * UDP_INDEXED_CHUNK_PAYLOAD_SIZE,
            payload, read_le16(header->payload_len));
        frame->received_chunks[chunk_id >> 3] |= bit;
        frame->received_chunks_count++;
    }
    return true;
}

static esp_err_t wait_indexed_lcd_pipeline(size_t *in_flight)
{
    while (*in_flight > 0) {
        ESP_RETURN_ON_ERROR(lcd_display_wait_direct_transfer(), TAG, "wait Indexed LCD DMA");
        --(*in_flight);
    }
    return ESP_OK;
}

static esp_err_t decode_and_flush_indexed(
    const indexed_work_item_t *work,
    const uint8_t *frame_buffer,
    uint8_t *indices,
    uint8_t *block_buffers[UDP_INDEXED_BLOCK_BUFFER_COUNT])
{
    if (!transport_is_active(ESPAGENT_TRANSPORT_INDEXED, work->transport_epoch)) {
        return ESP_ERR_INVALID_STATE;
    }
    const int64_t frame_started_us = esp_timer_get_time();
    const uint16_t palette_len = (uint16_t)((1u << work->bpp) * sizeof(uint16_t));
    const int64_t lz4_started_us = esp_timer_get_time();
    if (work->frame_len <= palette_len ||
        !decode_lz4_block(frame_buffer + palette_len, work->frame_len - palette_len, indices, work->raw_len)) {
        return ESP_FAIL;
    }
    uint64_t decode_total_us = (uint64_t)(esp_timer_get_time() - lz4_started_us);
    const uint8_t mask = (uint8_t)((1u << work->bpp) - 1u);
    lcd_display_begin_direct_pipeline();
    size_t in_flight = 0;
    size_t buffer_index = 0;
    int64_t lcd_started_us = 0;
    for (uint16_t output_y = 0; output_y < DISPLAY_HEIGHT; output_y += UDP_INDEXED_BLOCK_MAX_LINES) {
        if (!transport_is_active(ESPAGENT_TRANSPORT_INDEXED, work->transport_epoch)) {
            (void)wait_indexed_lcd_pipeline(&in_flight);
            return ESP_ERR_INVALID_STATE;
        }
        const uint16_t block_lines = output_y + UDP_INDEXED_BLOCK_MAX_LINES > DISPLAY_HEIGHT ?
            DISPLAY_HEIGHT - output_y : UDP_INDEXED_BLOCK_MAX_LINES;
        uint8_t *output = block_buffers[buffer_index];
        s_indexed_worker_stage = 3;
        const int64_t expand_started_us = esp_timer_get_time();
        uint32_t out = 0;
        const uint32_t first_pixel = (uint32_t)output_y * DISPLAY_WIDTH;
        const uint32_t pixel_count = (uint32_t)block_lines * DISPLAY_WIDTH;
        for (uint32_t offset = 0; offset < pixel_count; ++offset) {
            uint8_t color;
            if (work->bpp == 8) {
                color = indices[first_pixel + offset];
            } else {
                const uint32_t bit = (first_pixel + offset) * work->bpp;
                const uint8_t shift = (uint8_t)(8 - work->bpp - (bit & 7));
                color = (uint8_t)((indices[bit >> 3] >> shift) & mask);
            }
            output[out++] = frame_buffer[color * 2 + 1];
            output[out++] = frame_buffer[color * 2];
        }
        decode_total_us += (uint64_t)(esp_timer_get_time() - expand_started_us);
        if (in_flight == 1) {
            s_indexed_worker_stage = 5;
            ESP_RETURN_ON_ERROR(lcd_display_wait_direct_transfer(), TAG, "wait previous Indexed LCD transfer");
            --in_flight;
        }
        if (lcd_started_us == 0) {
            lcd_started_us = esp_timer_get_time();
        }
        s_indexed_worker_stage = 4;
        ESP_RETURN_ON_ERROR(
            lcd_display_submit_rgb565_be(
                0,
                output_y,
                DISPLAY_WIDTH,
                block_lines,
                output,
                (uint32_t)block_lines * DISPLAY_WIDTH * sizeof(uint16_t)),
            TAG,
            "submit Indexed LCD block");
        ++in_flight;
        buffer_index = (buffer_index + 1) % UDP_INDEXED_BLOCK_BUFFER_COUNT;
    }
    s_indexed_worker_stage = 5;
    ESP_RETURN_ON_ERROR(wait_indexed_lcd_pipeline(&in_flight), TAG, "finish Indexed LCD frame");
    const int64_t frame_finished_us = esp_timer_get_time();
    record_display_perf(
        (uint32_t)decode_total_us,
        (uint32_t)(frame_finished_us - lcd_started_us),
        (uint32_t)(frame_finished_us - frame_started_us));
    return ESP_OK;
}

static void reset_dirty_region(dirty_region_t *dirty, uint32_t frame_id)
{
    dirty->valid = false;
    dirty->frame_id = frame_id;
    dirty->min_x = DISPLAY_WIDTH;
    dirty->min_y = DISPLAY_HEIGHT;
    dirty->max_x = 0;
    dirty->max_y = 0;
    dirty->received_tile_count = 0;
    memset(dirty->received_tiles, 0, sizeof(dirty->received_tiles));
}

static void reset_jpeg_frame(jpeg_frame_t *jpeg, const tile_packet_header_t *header)
{
    jpeg->valid = true;
    jpeg->frame_id = read_le32(header->frame_id);
    jpeg->width = read_le16(header->w);
    jpeg->height = read_le16(header->h);
    jpeg->jpeg_len = read_le16(header->raw_len);
    jpeg->expected_chunks = (uint16_t)((jpeg->jpeg_len + UDP_JPEG_CHUNK_PAYLOAD_SIZE - 1) / UDP_JPEG_CHUNK_PAYLOAD_SIZE);
    jpeg->received_chunks_count = 0;
    jpeg->received_bytes = 0;
    jpeg->dropped_newer_chunks = 0;
    memset(jpeg->received_chunks, 0, sizeof(jpeg->received_chunks));
}

static bool jpeg_frame_complete(const jpeg_frame_t *jpeg)
{
    return jpeg->valid && jpeg->expected_chunks > 0 && jpeg->received_chunks_count >= jpeg->expected_chunks && jpeg->received_bytes >= jpeg->jpeg_len;
}

static esp_err_t wait_jpeg_lcd_pipeline(size_t *in_flight)
{
    while (*in_flight > 0) {
        ESP_RETURN_ON_ERROR(lcd_display_wait_direct_transfer(), TAG, "wait JPEG LCD DMA");
        --(*in_flight);
    }
    return ESP_OK;
}

static esp_err_t decode_and_flush_jpeg(
    jpeg_dec_handle_t handle,
    const jpeg_frame_t *jpeg,
    uint32_t transport_epoch,
    uint8_t *jpeg_buffer,
    uint8_t *block_buffers[UDP_JPEG_BLOCK_BUFFER_COUNT])
{
    if (!transport_is_active(ESPAGENT_TRANSPORT_MJPEG, transport_epoch)) {
        return ESP_ERR_INVALID_STATE;
    }
    const int64_t frame_started_us = esp_timer_get_time();
    s_jpeg_worker_stage = 2;
    jpeg_dec_io_t jpeg_io = {
        .inbuf = jpeg_buffer,
        .inbuf_len = jpeg->jpeg_len,
    };

    jpeg_dec_header_info_t out_info = {0};
    jpeg_error_t jpeg_ret = jpeg_dec_parse_header(handle, &jpeg_io, &out_info);
    if (jpeg_ret != JPEG_ERR_OK) {
        ESP_LOGW(TAG, "parse jpeg header failed ret=%d", jpeg_ret);
        return ESP_FAIL;
    }

    int outbuf_len = 0;
    jpeg_ret = jpeg_dec_get_outbuf_len(handle, &outbuf_len);
    if (jpeg_ret != JPEG_ERR_OK) {
        ESP_LOGW(TAG, "get jpeg output size failed ret=%d", jpeg_ret);
        return ESP_FAIL;
    }

    int process_count = 0;
    jpeg_ret = jpeg_dec_get_process_count(handle, &process_count);
    if (jpeg_ret != JPEG_ERR_OK) {
        ESP_LOGW(TAG, "get jpeg process count failed ret=%d", jpeg_ret);
        return ESP_FAIL;
    }

    if (out_info.width != DISPLAY_WIDTH || out_info.height != DISPLAY_HEIGHT ||
        outbuf_len <= 0 || outbuf_len > UDP_JPEG_BLOCK_BUFFER_SIZE || process_count <= 0) {
        ESP_LOGW(TAG, "jpeg output mismatch w=%u h=%u len=%d count=%d", out_info.width, out_info.height, outbuf_len, process_count);
        return ESP_ERR_INVALID_SIZE;
    }

    lcd_display_begin_direct_pipeline();
    size_t in_flight = 0;
    size_t buffer_index = 0;
    uint16_t output_y = 0;
    uint64_t decode_total_us = 0;
    int64_t lcd_started_us = 0;
    for (int block = 0; block < process_count; ++block) {
        if (!transport_is_active(ESPAGENT_TRANSPORT_MJPEG, transport_epoch)) {
            (void)wait_jpeg_lcd_pipeline(&in_flight);
            return ESP_ERR_INVALID_STATE;
        }
        jpeg_io.outbuf = block_buffers[buffer_index];
        jpeg_io.out_size = 0;
        s_jpeg_worker_stage = 3;
        const int64_t decode_started_us = esp_timer_get_time();
        jpeg_ret = jpeg_dec_process(handle, &jpeg_io);
        decode_total_us += (uint64_t)(esp_timer_get_time() - decode_started_us);
        if (jpeg_ret != JPEG_ERR_OK) {
            (void)wait_jpeg_lcd_pipeline(&in_flight);
            ESP_LOGW(TAG, "decode jpeg block=%d failed ret=%d", block, jpeg_ret);
            return ESP_FAIL;
        }
        if (jpeg_io.out_size <= 0 || (jpeg_io.out_size % (DISPLAY_WIDTH * sizeof(uint16_t))) != 0) {
            (void)wait_jpeg_lcd_pipeline(&in_flight);
            ESP_LOGW(TAG, "jpeg block=%d output size mismatch out=%d", block, jpeg_io.out_size);
            return ESP_ERR_INVALID_SIZE;
        }

        const uint16_t block_lines = (uint16_t)(jpeg_io.out_size / (DISPLAY_WIDTH * sizeof(uint16_t)));
        if (block_lines > UDP_JPEG_BLOCK_MAX_LINES || output_y + block_lines > DISPLAY_HEIGHT) {
            (void)wait_jpeg_lcd_pipeline(&in_flight);
            ESP_LOGW(TAG, "jpeg block=%d invalid lines=%u y=%u", block, block_lines, output_y);
            return ESP_ERR_INVALID_SIZE;
        }
        if (lcd_started_us == 0) {
            lcd_started_us = esp_timer_get_time();
        }
        if (in_flight == UDP_JPEG_LCD_MAX_IN_FLIGHT) {
            s_jpeg_worker_stage = 5;
            ESP_RETURN_ON_ERROR(lcd_display_wait_direct_transfer(), TAG, "wait previous JPEG LCD transfer");
            --in_flight;
        }
        s_jpeg_worker_stage = 4;
        if (!transport_is_active(ESPAGENT_TRANSPORT_MJPEG, transport_epoch)) {
            (void)wait_jpeg_lcd_pipeline(&in_flight);
            return ESP_ERR_INVALID_STATE;
        }
        esp_err_t ret = lcd_display_submit_rgb565_be(
            0,
            output_y,
            DISPLAY_WIDTH,
            block_lines,
            block_buffers[buffer_index],
            (uint32_t)jpeg_io.out_size);
        if (ret != ESP_OK) {
            (void)wait_jpeg_lcd_pipeline(&in_flight);
            return ret;
        }
        ++in_flight;
        output_y += block_lines;
        buffer_index = (buffer_index + 1) % UDP_JPEG_BLOCK_BUFFER_COUNT;
    }
    if (output_y != DISPLAY_HEIGHT) {
        (void)wait_jpeg_lcd_pipeline(&in_flight);
        ESP_LOGW(TAG, "jpeg block output height mismatch y=%u", output_y);
        return ESP_ERR_INVALID_SIZE;
    }
    s_jpeg_worker_stage = 5;
    esp_err_t ret = wait_jpeg_lcd_pipeline(&in_flight);
    const int64_t frame_finished_us = esp_timer_get_time();
    if (ret == ESP_OK) {
        record_display_perf(
            (uint32_t)decode_total_us,
            (uint32_t)(frame_finished_us - lcd_started_us),
            (uint32_t)(frame_finished_us - frame_started_us));
    }
    return ret;
}

static esp_err_t assemble_jpeg_chunk(
    const tile_packet_header_t *header,
    const uint8_t *payload,
    jpeg_frame_t *jpeg,
    uint8_t *jpeg_buffer)
{
    const uint32_t frame_id = read_le32(header->frame_id);
    const uint16_t jpeg_len = read_le16(header->raw_len);
    const uint16_t chunk_id = read_le16(header->tile_id);
    const uint16_t payload_len = read_le16(header->payload_len);
    if (!jpeg->valid || jpeg->frame_id != frame_id || jpeg->jpeg_len != jpeg_len) {
        reset_jpeg_frame(jpeg, header);
    }

    const uint8_t mask = (uint8_t)(1u << (chunk_id & 7u));
    uint8_t *slot = &jpeg->received_chunks[chunk_id >> 3];
    const uint32_t offset = (uint32_t)chunk_id * UDP_JPEG_CHUNK_PAYLOAD_SIZE;
    memcpy(&jpeg_buffer[offset], payload, payload_len);
    if ((*slot & mask) == 0) {
        *slot |= mask;
        jpeg->received_chunks_count++;
        jpeg->received_bytes += payload_len;
    }
    if (!jpeg_frame_complete(jpeg)) {
        return ESP_ERR_NOT_FINISHED;
    }

    jpeg->valid = false;
    return ESP_OK;
}

static void include_dirty_tile(dirty_region_t *dirty, const tile_packet_header_t *header)
{
    const uint16_t tile_id = read_le16(header->tile_id);
    const uint16_t x = read_le16(header->x);
    const uint16_t y = read_le16(header->y);
    const uint16_t w = read_le16(header->w);
    const uint16_t h = read_le16(header->h);
    if (tile_id < UDP_MAX_FRAME_TILES) {
        const uint8_t mask = (uint8_t)(1u << (tile_id & 7u));
        uint8_t *slot = &dirty->received_tiles[tile_id >> 3];
        if ((*slot & mask) == 0) {
            *slot |= mask;
            dirty->received_tile_count++;
        }
    }
    if (!dirty->valid) {
        dirty->min_x = x;
        dirty->min_y = y;
        dirty->max_x = x + w;
        dirty->max_y = y + h;
        dirty->valid = true;
        return;
    }
    dirty->min_x = x < dirty->min_x ? x : dirty->min_x;
    dirty->min_y = y < dirty->min_y ? y : dirty->min_y;
    dirty->max_x = x + w > dirty->max_x ? x + w : dirty->max_x;
    dirty->max_y = y + h > dirty->max_y ? y + h : dirty->max_y;
}

static bool dirty_region_has_expected_tiles(const dirty_region_t *dirty, uint16_t expected_tile_count)
{
    return expected_tile_count > 0 && expected_tile_count <= UDP_MAX_FRAME_TILES && dirty->received_tile_count >= expected_tile_count;
}

static esp_err_t flush_dirty_region(dirty_region_t *dirty)
{
    if (!dirty->valid) {
        return ESP_OK;
    }

    const uint16_t w = dirty->max_x - dirty->min_x;
    const uint16_t h = dirty->max_y - dirty->min_y;
    xSemaphoreTake(s_display_mutex, portMAX_DELAY);
    esp_err_t ret = lcd_display_flush_framebuffer(dirty->min_x, dirty->min_y, w, h);
    xSemaphoreGive(s_display_mutex);
    dirty->valid = false;
    return ret;
}

static void free_jpeg_block_buffers(uint8_t *block_buffers[UDP_JPEG_BLOCK_BUFFER_COUNT])
{
    for (size_t i = 0; i < UDP_JPEG_BLOCK_BUFFER_COUNT; ++i) {
        heap_caps_free(block_buffers[i]);
        block_buffers[i] = NULL;
    }
}

static void jpeg_pipeline_deinit(jpeg_pipeline_t *pipeline)
{
    if (pipeline->worker_task != NULL) {
        vTaskDelete(pipeline->worker_task);
        pipeline->worker_task = NULL;
    }
    if (pipeline->decoder != NULL) {
        (void)jpeg_dec_close(pipeline->decoder);
        pipeline->decoder = NULL;
    }
    free_jpeg_block_buffers(pipeline->block_buffers);
    for (size_t i = 0; i < UDP_JPEG_FRAME_BUFFER_COUNT; ++i) {
        heap_caps_free(pipeline->frame_buffers[i]);
        pipeline->frame_buffers[i] = NULL;
    }
    if (pipeline->free_buffers != NULL) {
        vQueueDelete(pipeline->free_buffers);
        pipeline->free_buffers = NULL;
    }
    if (pipeline->complete_frames != NULL) {
        vQueueDelete(pipeline->complete_frames);
        pipeline->complete_frames = NULL;
    }
    if (pipeline->results != NULL) {
        vQueueDelete(pipeline->results);
        pipeline->results = NULL;
    }
}

static void discard_queued_jpeg_frames(jpeg_pipeline_t *pipeline)
{
    jpeg_work_item_t stale;
    while (xQueueReceive(pipeline->complete_frames, &stale, 0) == pdTRUE) {
        ++s_jpeg_dropped_frames;
        (void)xQueueSend(pipeline->free_buffers, &stale.jpeg_buffer, portMAX_DELAY);
    }
}

static void indexed_pipeline_deinit(indexed_pipeline_t *pipeline)
{
    if (pipeline->worker_task != NULL) {
        vTaskDelete(pipeline->worker_task);
        pipeline->worker_task = NULL;
    }
    for (size_t i = 0; i < UDP_INDEXED_BLOCK_BUFFER_COUNT; ++i) {
        heap_caps_free(pipeline->block_buffers[i]);
        pipeline->block_buffers[i] = NULL;
    }
    heap_caps_free(pipeline->indices);
    pipeline->indices = NULL;
    heap_caps_free(pipeline->extended_indices);
    pipeline->extended_indices = NULL;
    for (size_t i = 0; i < UDP_INDEXED_FRAME_BUFFER_COUNT; ++i) {
        heap_caps_free(pipeline->frame_buffers[i]);
        pipeline->frame_buffers[i] = NULL;
    }
    if (pipeline->free_buffers != NULL) {
        vQueueDelete(pipeline->free_buffers);
        pipeline->free_buffers = NULL;
    }
    if (pipeline->complete_frames != NULL) {
        vQueueDelete(pipeline->complete_frames);
        pipeline->complete_frames = NULL;
    }
    if (pipeline->results != NULL) {
        vQueueDelete(pipeline->results);
        pipeline->results = NULL;
    }
}

static void discard_queued_indexed_frames(indexed_pipeline_t *pipeline)
{
    indexed_work_item_t stale;
    while (xQueueReceive(pipeline->complete_frames, &stale, 0) == pdTRUE) {
        ++s_indexed_dropped_frames;
        (void)xQueueSend(pipeline->free_buffers, &stale.frame_buffer, portMAX_DELAY);
    }
}

static bool switch_transport_mode(uint8_t requested_mode, uint32_t requested_epoch, uint8_t requested_orientation)
{
    if (transport_is_active(requested_mode, requested_epoch) && s_display_orientation == requested_orientation) {
        return false;
    }

    s_active_transport_mode = requested_mode;
    s_transport_epoch = requested_epoch;
    xSemaphoreTake(s_display_mutex, portMAX_DELAY);
    discard_queued_jpeg_frames(&s_jpeg_pipeline);
    discard_queued_indexed_frames(&s_indexed_pipeline);
    if (s_display_orientation != requested_orientation) {
        const bool landscape = requested_orientation == ESPAGENT_DISPLAY_LANDSCAPE;
        esp_err_t orientation_ret = lcd_display_set_orientation(landscape);
        if (orientation_ret == ESP_OK) {
            s_display_orientation = requested_orientation;
            s_display_width = landscape ? DISPLAY_LANDSCAPE_WIDTH : DISPLAY_PORTRAIT_WIDTH;
            s_display_height = landscape ? DISPLAY_LANDSCAPE_HEIGHT : DISPLAY_PORTRAIT_HEIGHT;
        } else {
            ESP_LOGE(TAG, "set display orientation failed: %s", esp_err_to_name(orientation_ret));
        }
    }
    xSemaphoreGive(s_display_mutex);
    reset_display_perf();
    return true;
}

static void jpeg_worker_task(void *arg)
{
    jpeg_pipeline_t *pipeline = arg;
    jpeg_work_item_t work;
    while (true) {
        if (xQueueReceive(pipeline->complete_frames, &work, portMAX_DELAY) != pdTRUE) {
            continue;
        }
        jpeg_frame_t jpeg = {
            .valid = true,
            .frame_id = work.frame_id,
            .width = work.width,
            .height = work.height,
            .jpeg_len = work.jpeg_len,
        };
        s_jpeg_worker_frame_id = work.frame_id;
        s_jpeg_worker_stage = 1;
        jpeg_work_result_t result = {
            .frame_id = work.frame_id,
            .transport_epoch = work.transport_epoch,
            .result = ESP_ERR_INVALID_STATE,
        };
        xSemaphoreTake(s_display_mutex, portMAX_DELAY);
        if (transport_is_active(ESPAGENT_TRANSPORT_MJPEG, work.transport_epoch)) {
            result.result = decode_and_flush_jpeg(
                pipeline->decoder,
                &jpeg,
                work.transport_epoch,
                work.jpeg_buffer,
                pipeline->block_buffers);
        }
        xSemaphoreGive(s_display_mutex);
        s_jpeg_worker_stage = 6;
        (void)xQueueSend(pipeline->results, &result, portMAX_DELAY);
        (void)xQueueSend(pipeline->free_buffers, &work.jpeg_buffer, portMAX_DELAY);
        s_jpeg_worker_stage = 0;
    }
}

static esp_err_t jpeg_pipeline_init(jpeg_pipeline_t *pipeline)
{
    memset(pipeline, 0, sizeof(*pipeline));
    pipeline->free_buffers = xQueueCreate(UDP_JPEG_FRAME_BUFFER_COUNT, sizeof(uint8_t *));
    pipeline->complete_frames = xQueueCreate(UDP_JPEG_FRAME_QUEUE_DEPTH, sizeof(jpeg_work_item_t));
    pipeline->results = xQueueCreate(UDP_JPEG_RESULT_QUEUE_DEPTH, sizeof(jpeg_work_result_t));
    if (pipeline->free_buffers == NULL || pipeline->complete_frames == NULL || pipeline->results == NULL) {
        jpeg_pipeline_deinit(pipeline);
        return ESP_ERR_NO_MEM;
    }

    for (size_t i = 0; i < UDP_JPEG_FRAME_BUFFER_COUNT; ++i) {
        pipeline->frame_buffers[i] = heap_caps_malloc(UDP_JPEG_MAX_BYTES, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
        if (pipeline->frame_buffers[i] == NULL) {
            pipeline->frame_buffers[i] = heap_caps_malloc(UDP_JPEG_MAX_BYTES, MALLOC_CAP_8BIT);
        }
        if (pipeline->frame_buffers[i] == NULL ||
            xQueueSend(pipeline->free_buffers, &pipeline->frame_buffers[i], 0) != pdTRUE) {
            jpeg_pipeline_deinit(pipeline);
            return ESP_ERR_NO_MEM;
        }
    }

    for (size_t i = 0; i < UDP_JPEG_BLOCK_BUFFER_COUNT; ++i) {
        pipeline->block_buffers[i] = heap_caps_aligned_alloc(
            16,
            UDP_JPEG_BLOCK_BUFFER_SIZE,
            MALLOC_CAP_DMA | MALLOC_CAP_INTERNAL);
        if (pipeline->block_buffers[i] == NULL) {
            jpeg_pipeline_deinit(pipeline);
            return ESP_ERR_NO_MEM;
        }
    }

    jpeg_dec_config_t jpeg_cfg = DEFAULT_JPEG_DEC_CONFIG();
    jpeg_cfg.output_type = JPEG_PIXEL_FORMAT_RGB565_BE;
    jpeg_cfg.block_enable = true;
    jpeg_error_t open_ret = jpeg_dec_open(&jpeg_cfg, &pipeline->decoder);
    if (open_ret != JPEG_ERR_OK) {
        jpeg_pipeline_deinit(pipeline);
        return ESP_FAIL;
    }

    BaseType_t created = xTaskCreatePinnedToCore(
        jpeg_worker_task,
        "jpeg_display",
        UDP_JPEG_WORKER_STACK,
        pipeline,
        UDP_JPEG_WORKER_PRIORITY,
        &pipeline->worker_task,
        UDP_JPEG_WORKER_CORE);
    if (created != pdPASS) {
        jpeg_pipeline_deinit(pipeline);
        return ESP_ERR_NO_MEM;
    }
    return ESP_OK;
}

static bool consume_jpeg_results(uint32_t *flushed_frame_count, uint32_t *draw_failed_count)
{
    bool displayed = false;
    jpeg_work_result_t result;
    while (xQueueReceive(s_jpeg_pipeline.results, &result, 0) == pdTRUE) {
        if (!transport_is_active(ESPAGENT_TRANSPORT_MJPEG, result.transport_epoch) ||
            result.result == ESP_ERR_INVALID_STATE) {
            continue;
        }
        if (result.result == ESP_OK) {
            ++(*flushed_frame_count);
            s_displayed_frame_id = result.frame_id;
            displayed = true;
        } else {
            ++(*draw_failed_count);
            ESP_LOGW(TAG, "jpeg worker frame=%lu failed: %s", result.frame_id, esp_err_to_name(result.result));
        }
    }
    return displayed;
}

static void indexed_worker_task(void *arg)
{
    indexed_pipeline_t *pipeline = arg;
    indexed_work_item_t work;
    while (true) {
        if (xQueueReceive(pipeline->complete_frames, &work, portMAX_DELAY) != pdTRUE) {
            continue;
        }
        s_indexed_worker_frame_id = work.frame_id;
        s_indexed_worker_stage = 1;
        indexed_work_result_t result = {
            .frame_id = work.frame_id,
            .transport_epoch = work.transport_epoch,
            .result = ESP_ERR_INVALID_STATE,
        };
        xSemaphoreTake(s_display_mutex, portMAX_DELAY);
        if (transport_is_active(ESPAGENT_TRANSPORT_INDEXED, work.transport_epoch)) {
            s_indexed_worker_stage = 2;
            uint8_t *indices = work.raw_len <= UDP_INDEXED_FAST_RAW_BYTES ?
                pipeline->indices : pipeline->extended_indices;
            result.result = decode_and_flush_indexed(
                &work,
                work.frame_buffer,
                indices,
                pipeline->block_buffers);
        }
        xSemaphoreGive(s_display_mutex);
        s_indexed_worker_stage = 6;
        (void)xQueueSend(pipeline->results, &result, portMAX_DELAY);
        (void)xQueueSend(pipeline->free_buffers, &work.frame_buffer, portMAX_DELAY);
        s_indexed_worker_stage = 0;
    }
}

static esp_err_t indexed_pipeline_init(indexed_pipeline_t *pipeline)
{
    memset(pipeline, 0, sizeof(*pipeline));
    pipeline->free_buffers = xQueueCreate(UDP_INDEXED_FRAME_BUFFER_COUNT, sizeof(uint8_t *));
    pipeline->complete_frames = xQueueCreate(UDP_INDEXED_FRAME_QUEUE_DEPTH, sizeof(indexed_work_item_t));
    pipeline->results = xQueueCreate(UDP_INDEXED_RESULT_QUEUE_DEPTH, sizeof(indexed_work_result_t));
    if (pipeline->free_buffers == NULL || pipeline->complete_frames == NULL || pipeline->results == NULL) {
        indexed_pipeline_deinit(pipeline);
        return ESP_ERR_NO_MEM;
    }
    for (size_t i = 0; i < UDP_INDEXED_FRAME_BUFFER_COUNT; ++i) {
        pipeline->frame_buffers[i] = heap_caps_malloc(
            UDP_INDEXED_FRAME_MAX_BYTES,
            MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
        if (pipeline->frame_buffers[i] == NULL) {
            pipeline->frame_buffers[i] = heap_caps_malloc(UDP_INDEXED_FRAME_MAX_BYTES, MALLOC_CAP_8BIT);
        }
        if (pipeline->frame_buffers[i] == NULL ||
            xQueueSend(pipeline->free_buffers, &pipeline->frame_buffers[i], 0) != pdTRUE) {
            indexed_pipeline_deinit(pipeline);
            return ESP_ERR_NO_MEM;
        }
    }
    pipeline->indices = heap_caps_malloc(UDP_INDEXED_FAST_RAW_BYTES, MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT);
    if (pipeline->indices == NULL) {
        indexed_pipeline_deinit(pipeline);
        return ESP_ERR_NO_MEM;
    }
    pipeline->extended_indices = heap_caps_malloc(
        UDP_INDEXED_MAX_RAW_BYTES,
        MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
    if (pipeline->extended_indices == NULL) {
        pipeline->extended_indices = heap_caps_malloc(UDP_INDEXED_MAX_RAW_BYTES, MALLOC_CAP_8BIT);
    }
    if (pipeline->extended_indices == NULL) {
        indexed_pipeline_deinit(pipeline);
        return ESP_ERR_NO_MEM;
    }
    for (size_t i = 0; i < UDP_INDEXED_BLOCK_BUFFER_COUNT; ++i) {
        pipeline->block_buffers[i] = heap_caps_aligned_alloc(
            16,
            UDP_INDEXED_BLOCK_BUFFER_SIZE,
            MALLOC_CAP_DMA | MALLOC_CAP_INTERNAL);
        if (pipeline->block_buffers[i] == NULL) {
            indexed_pipeline_deinit(pipeline);
            return ESP_ERR_NO_MEM;
        }
    }
    BaseType_t created = xTaskCreatePinnedToCore(
        indexed_worker_task,
        "indexed_display",
        UDP_INDEXED_WORKER_STACK,
        pipeline,
        UDP_INDEXED_WORKER_PRIORITY,
        &pipeline->worker_task,
        UDP_INDEXED_WORKER_CORE);
    if (created != pdPASS) {
        indexed_pipeline_deinit(pipeline);
        return ESP_ERR_NO_MEM;
    }
    return ESP_OK;
}

static bool consume_indexed_results(uint32_t *flushed_frame_count, uint32_t *draw_failed_count)
{
    bool displayed = false;
    indexed_work_result_t result;
    while (xQueueReceive(s_indexed_pipeline.results, &result, 0) == pdTRUE) {
        if (!transport_is_active(ESPAGENT_TRANSPORT_INDEXED, result.transport_epoch) ||
            result.result == ESP_ERR_INVALID_STATE) {
            continue;
        }
        if (result.result == ESP_OK) {
            ++(*flushed_frame_count);
            s_displayed_frame_id = result.frame_id;
            displayed = true;
        } else {
            ++(*draw_failed_count);
            ESP_LOGW(TAG, "indexed worker frame=%lu failed: %s", result.frame_id, esp_err_to_name(result.result));
        }
    }
    return displayed;
}

static void udp_display_task(void *arg)
{
    uint8_t *rx_buffer = heap_caps_malloc(UDP_RX_BUFFER_SIZE, MALLOC_CAP_8BIT);
    if (rx_buffer == NULL) {
        ESP_LOGE(TAG, "allocate UDP RX buffer failed");
        vTaskDelete(NULL);
        return;
    }
    uint8_t *tile_decode_buffer = heap_caps_malloc(UDP_TILE_DECODE_BUFFER_SIZE, MALLOC_CAP_8BIT);
    if (tile_decode_buffer == NULL) {
        ESP_LOGE(TAG, "allocate tile decode buffer failed");
        heap_caps_free(rx_buffer);
        vTaskDelete(NULL);
        return;
    }
    s_display_mutex = xSemaphoreCreateMutex();
    if (s_display_mutex == NULL) {
        ESP_LOGE(TAG, "create display mutex failed");
        heap_caps_free(tile_decode_buffer);
        heap_caps_free(rx_buffer);
        vTaskDelete(NULL);
        return;
    }
    esp_err_t pipeline_ret = jpeg_pipeline_init(&s_jpeg_pipeline);
    if (pipeline_ret != ESP_OK) {
        ESP_LOGE(TAG, "initialize jpeg pipeline failed: %s", esp_err_to_name(pipeline_ret));
        heap_caps_free(tile_decode_buffer);
        heap_caps_free(rx_buffer);
        vSemaphoreDelete(s_display_mutex);
        s_display_mutex = NULL;
        vTaskDelete(NULL);
        return;
    }
    pipeline_ret = indexed_pipeline_init(&s_indexed_pipeline);
    if (pipeline_ret != ESP_OK) {
        ESP_LOGE(TAG, "initialize indexed pipeline failed: %s", esp_err_to_name(pipeline_ret));
        jpeg_pipeline_deinit(&s_jpeg_pipeline);
        heap_caps_free(tile_decode_buffer);
        heap_caps_free(rx_buffer);
        vSemaphoreDelete(s_display_mutex);
        s_display_mutex = NULL;
        vTaskDelete(NULL);
        return;
    }
    uint8_t *jpeg_buffer = NULL;
    uint8_t *indexed_frame_buffer = NULL;
    ESP_ERROR_CHECK(xQueueReceive(s_jpeg_pipeline.free_buffers, &jpeg_buffer, 0) == pdTRUE ? ESP_OK : ESP_ERR_NO_MEM);
    ESP_ERROR_CHECK(xQueueReceive(s_indexed_pipeline.free_buffers, &indexed_frame_buffer, 0) == pdTRUE ? ESP_OK : ESP_ERR_NO_MEM);

    struct sockaddr_in listen_addr = {
        .sin_family = AF_INET,
        .sin_port = htons(CONFIG_ESPAGENT_UDP_PORT),
        .sin_addr.s_addr = htonl(INADDR_ANY),
    };

    int sock = socket(AF_INET, SOCK_DGRAM, IPPROTO_IP);
    if (sock < 0) {
        ESP_LOGE(TAG, "socket failed errno=%d", errno);
        vTaskDelete(NULL);
        return;
    }
    const int rx_buffer_bytes = UDP_SOCKET_RX_BUFFER_BYTES;
    if (setsockopt(sock, SOL_SOCKET, SO_RCVBUF, &rx_buffer_bytes, sizeof(rx_buffer_bytes)) < 0) {
        ESP_LOGW(TAG, "set UDP rx buffer failed errno=%d", errno);
    }
    const struct timeval rx_timeout = {
        .tv_sec = 0,
        .tv_usec = UDP_RESULT_POLL_TIMEOUT_MS * 1000,
    };
    if (setsockopt(sock, SOL_SOCKET, SO_RCVTIMEO, &rx_timeout, sizeof(rx_timeout)) < 0) {
        ESP_LOGW(TAG, "set UDP rx timeout failed errno=%d", errno);
    }

    if (bind(sock, (struct sockaddr *)&listen_addr, sizeof(listen_addr)) < 0) {
        ESP_LOGE(TAG, "bind port=%d failed errno=%d", CONFIG_ESPAGENT_UDP_PORT, errno);
        close(sock);
        vTaskDelete(NULL);
        return;
    }

    ESP_LOGI(TAG, "UDP display receiver listening on port %d", CONFIG_ESPAGENT_UDP_PORT);
    esp_log_level_set("*", ESP_LOG_NONE);
    uint32_t rx_count = 0;
    uint32_t accepted_count = 0;
    uint32_t rejected_count = 0;
    uint32_t draw_failed_count = 0;
    uint32_t frame_end_count = 0;
    uint32_t flushed_frame_count = 0;
    uint32_t stale_frame_end_count = 0;
    uint32_t last_sent_perf_sequence = 0;
    dirty_region_t dirty = {0};
    reset_dirty_region(&dirty, 0);
    jpeg_frame_t jpeg = {0};
    indexed_frame_t indexed_full = {0};
    uint32_t last_jpeg_no_buffer_frame_id = UINT32_MAX;
    uint32_t last_indexed_no_buffer_frame_id = UINT32_MAX;
    bool unified_flush = true;
    bool nack_enabled = true;
    bool esp_log_enabled = false;
    int64_t last_rx_at_us = 0;
    int64_t last_cooperative_yield_us = esp_timer_get_time();
    struct sockaddr_storage last_source_addr = {0};
    bool has_last_source_addr = false;
    while (true) {
        const bool displayed = consume_jpeg_results(&flushed_frame_count, &draw_failed_count) |
            consume_indexed_results(&flushed_frame_count, &draw_failed_count);
        if (displayed && has_last_source_addr) {
            send_display_ack(sock, &last_source_addr);
        }
        struct sockaddr_storage source_addr;
        socklen_t socklen = sizeof(source_addr);
        const int len = recvfrom(sock, rx_buffer, UDP_RX_BUFFER_SIZE, 0, (struct sockaddr *)&source_addr, &socklen);
        if (len < 0) {
            if (errno == EAGAIN || errno == EWOULDBLOCK) {
                if (s_active_transport_mode == ESPAGENT_TRANSPORT_TILES && !unified_flush && dirty.valid &&
                    esp_timer_get_time() - last_rx_at_us >= UDP_FLUSH_IDLE_TIMEOUT_MS * 1000) {
                    esp_err_t flush_ret = flush_dirty_region(&dirty);
                    if (flush_ret != ESP_OK) {
                        draw_failed_count++;
                        (void)lcd_display_draw_status_marker(STATUS_MARKER_REJECT);
                        ESP_LOGW(TAG, "idle flush failed: %s", esp_err_to_name(flush_ret));
                    } else {
                        flushed_frame_count++;
                        s_displayed_frame_id = dirty.frame_id;
                    }
                    if (has_last_source_addr) {
                        send_status(sock, &last_source_addr, rx_count, accepted_count, rejected_count, draw_failed_count, frame_end_count, flushed_frame_count, stale_frame_end_count);
                    }
                }
                // The socket timeout must not turn into a zero-tick polling
                // loop when FreeRTOS runs at 100 Hz.
                vTaskDelay(1);
                continue;
            }
            ESP_LOGW(TAG, "recvfrom failed errno=%d", errno);
            continue;
        }

        last_source_addr = source_addr;
        has_last_source_addr = true;
        if (len == (int)(sizeof(ESPAGENT_DISCOVERY_MESSAGE) - 1) &&
            memcmp(rx_buffer, ESPAGENT_DISCOVERY_MESSAGE, sizeof(ESPAGENT_DISCOVERY_MESSAGE) - 1) == 0) {
            send_status(
                sock,
                &source_addr,
                rx_count,
                accepted_count,
                rejected_count,
                draw_failed_count,
                frame_end_count,
                flushed_frame_count,
                stale_frame_end_count
            );
            continue;
        }
        last_rx_at_us = esp_timer_get_time();
        rx_count++;
        if (last_rx_at_us - last_cooperative_yield_us >= UDP_RX_COOPERATIVE_YIELD_INTERVAL_US) {
            // A continuously readable UDP socket never blocks, so explicitly
            // let the CPU0 idle task run often enough to service its watchdog.
            vTaskDelay(2);
            last_cooperative_yield_us = esp_timer_get_time();
        }
        if (rx_count == 1 || (rx_count % UDP_STATUS_INTERVAL_PACKETS) == 0) {
            ESP_LOGI(
                TAG,
                "udp stats rx=%lu accepted=%lu rejected=%lu draw_failed=%lu last_len=%d",
                rx_count,
                accepted_count,
                rejected_count,
                draw_failed_count,
                len);
            send_status(sock, &source_addr, rx_count, accepted_count, rejected_count, draw_failed_count, frame_end_count, flushed_frame_count, stale_frame_end_count);
        }

        if (len >= (int)sizeof(tile_packet_header_t)) {
            const tile_packet_header_t *raw_header = (const tile_packet_header_t *)rx_buffer;
            if ((raw_header->type == PKT_INDEXED_FRAME_CHUNK ||
                    raw_header->type == PKT_INDEXED_RECT ||
                    raw_header->type == PKT_INDEXED_FRAME_END) &&
                s_active_transport_mode != ESPAGENT_TRANSPORT_INDEXED) {
                continue;
            }
            if (raw_header->type == PKT_INDEXED_RECT || raw_header->type == PKT_INDEXED_FRAME_END) {
                continue;
            }
            if (raw_header->type == PKT_JPEG_CHUNK &&
                s_active_transport_mode != ESPAGENT_TRANSPORT_MJPEG) {
                continue;
            }
            if ((raw_header->type == PKT_TILE || raw_header->type == PKT_FRAME_END) &&
                s_active_transport_mode != ESPAGENT_TRANSPORT_TILES) {
                continue;
            }
            if (raw_header->type == PKT_INDEXED_FRAME_CHUNK) {
                const tile_packet_header_t *indexed_header = NULL;
                const uint8_t *indexed_payload = NULL;
                uint8_t bpp = 0;
                if (!validate_indexed_frame_chunk(rx_buffer, len, &indexed_header, &indexed_payload, &bpp)) {
                    rejected_count++;
                    indexed_full.valid = false;
                    continue;
                }
                accepted_count++;
                const uint32_t indexed_frame_id = read_le32(indexed_header->frame_id);
                const uint16_t indexed_chunk_id = read_le16(indexed_header->tile_id);
                if (indexed_frame_buffer == NULL && indexed_chunk_id == 0) {
                    (void)xQueueReceive(s_indexed_pipeline.free_buffers, &indexed_frame_buffer, 0);
                }
                if (indexed_frame_buffer == NULL) {
                    if (last_indexed_no_buffer_frame_id != indexed_frame_id) {
                        last_indexed_no_buffer_frame_id = indexed_frame_id;
                        ++s_indexed_no_buffer_frames;
                        ++s_indexed_dropped_frames;
                    }
                    continue;
                }
                if (indexed_full.valid && indexed_full.frame_id != indexed_frame_id &&
                    indexed_full.received_chunks_count < indexed_full.expected_chunks) {
                    ++s_indexed_incomplete_frames;
                }
                if (!assemble_indexed_frame_chunk(
                        &indexed_full,
                        indexed_header,
                        indexed_payload,
                        bpp,
                        indexed_frame_buffer)) {
                    rejected_count++;
                    indexed_full.valid = false;
                    continue;
                }
                if (indexed_full.received_chunks_count == indexed_full.expected_chunks) {
                    indexed_work_item_t work = {
                        .frame_id = indexed_full.frame_id,
                        .transport_epoch = s_transport_epoch,
                        .frame_len = indexed_full.frame_len,
                        .raw_len = indexed_full.raw_len,
                        .bpp = indexed_full.bpp,
                        .frame_buffer = indexed_frame_buffer,
                    };
                    bool queued = xQueueSend(s_indexed_pipeline.complete_frames, &work, 0) == pdTRUE;
                    if (!queued) {
                        indexed_work_item_t stale;
                        if (xQueueReceive(s_indexed_pipeline.complete_frames, &stale, 0) == pdTRUE) {
                            ++s_indexed_dropped_frames;
                            if (xQueueSend(s_indexed_pipeline.complete_frames, &work, 0) == pdTRUE) {
                                indexed_frame_buffer = stale.frame_buffer;
                                queued = true;
                            } else {
                                (void)xQueueSend(s_indexed_pipeline.free_buffers, &stale.frame_buffer, 0);
                            }
                        }
                    }
                    if (queued) {
                        ++s_indexed_queued_frames;
                        if (indexed_frame_buffer == work.frame_buffer) {
                            indexed_frame_buffer = NULL;
                            (void)xQueueReceive(s_indexed_pipeline.free_buffers, &indexed_frame_buffer, 0);
                        }
                    } else {
                        ++s_indexed_dropped_frames;
                    }
                    indexed_full.valid = false;
                }
                continue;
            }
            if (raw_header->type == PKT_FRAME_BEGIN) {
                bool requested_unified_flush = true;
                bool requested_nack_enabled = true;
                bool requested_esp_log_enabled = false;
                uint8_t requested_backlight_percent = 100;
                uint8_t requested_panel_gamma = LCD_GAMMA_22;
                uint8_t requested_transport_mode = s_active_transport_mode;
                uint32_t requested_transport_epoch = s_transport_epoch;
                uint8_t requested_orientation = s_display_orientation;
                if (!validate_display_mode_packet(
                        rx_buffer,
                        len,
                        &requested_unified_flush,
                        &requested_nack_enabled,
                        &requested_esp_log_enabled,
                        &requested_backlight_percent,
                        &requested_panel_gamma,
                        &requested_transport_mode,
                        &requested_transport_epoch,
                        &requested_orientation)) {
                    rejected_count++;
                    (void)lcd_display_draw_status_marker(STATUS_MARKER_REJECT);
                    send_status(sock, &source_addr, rx_count, accepted_count, rejected_count, draw_failed_count, frame_end_count, flushed_frame_count, stale_frame_end_count);
                    continue;
                }
                if (transport_epoch_is_stale(requested_transport_epoch) ||
                    (requested_transport_epoch == s_transport_epoch &&
                        requested_transport_mode != s_active_transport_mode)) {
                    send_status(sock, &source_addr, rx_count, accepted_count, rejected_count,
                        draw_failed_count, frame_end_count, flushed_frame_count, stale_frame_end_count);
                    continue;
                }
                accepted_count++;
                const bool transport_changed = switch_transport_mode(
                    requested_transport_mode,
                    requested_transport_epoch,
                    requested_orientation);
                if (transport_changed) {
                    dirty.valid = false;
                    jpeg.valid = false;
                    indexed_full.valid = false;
                    last_jpeg_no_buffer_frame_id = UINT32_MAX;
                    last_indexed_no_buffer_frame_id = UINT32_MAX;
                }
                if (!transport_changed && s_active_transport_mode == ESPAGENT_TRANSPORT_TILES &&
                    unified_flush && !requested_unified_flush && dirty.valid) {
                    esp_err_t flush_ret = flush_dirty_region(&dirty);
                    if (flush_ret != ESP_OK) {
                        draw_failed_count++;
                        (void)lcd_display_draw_status_marker(STATUS_MARKER_REJECT);
                        ESP_LOGW(TAG, "flush before mode switch failed: %s", esp_err_to_name(flush_ret));
                    } else {
                        flushed_frame_count++;
                        s_displayed_frame_id = dirty.frame_id;
                    }
                }
                unified_flush = requested_unified_flush;
                nack_enabled = requested_nack_enabled;
                if (esp_log_enabled != requested_esp_log_enabled) {
                    esp_log_enabled = requested_esp_log_enabled;
                    esp_log_level_set("*", esp_log_enabled ? ESP_LOG_INFO : ESP_LOG_NONE);
                }
                esp_err_t backlight_ret = lcd_display_set_backlight_percent(requested_backlight_percent);
                if (backlight_ret != ESP_OK) {
                    draw_failed_count++;
                    ESP_LOGW(TAG, "set backlight failed: %s", esp_err_to_name(backlight_ret));
                }
                esp_err_t gamma_ret = ESP_OK;
                if (lcd_display_get_gamma_curve() != requested_panel_gamma) {
                    xSemaphoreTake(s_display_mutex, portMAX_DELAY);
                    gamma_ret = lcd_display_set_gamma_curve(requested_panel_gamma);
                    xSemaphoreGive(s_display_mutex);
                }
                if (gamma_ret != ESP_OK) {
                    draw_failed_count++;
                    ESP_LOGW(TAG, "set panel gamma failed: %s", esp_err_to_name(gamma_ret));
                }
                if (!nack_enabled && jpeg.valid && !jpeg_frame_complete(&jpeg)) {
                    jpeg.valid = false;
                }
                ESP_LOGI(
                    TAG,
                    "display mode=%u epoch=%lu unified flush %s nack %s esp_log %s backlight %u%% gamma=0x%02x",
                    s_active_transport_mode,
                    s_transport_epoch,
                    unified_flush ? "on" : "off",
                    nack_enabled ? "on" : "off",
                    esp_log_enabled ? "on" : "off",
                    requested_backlight_percent,
                    requested_panel_gamma);
                send_status(sock, &source_addr, rx_count, accepted_count, rejected_count, draw_failed_count, frame_end_count, flushed_frame_count, stale_frame_end_count);
                continue;
            }
            if (raw_header->type == PKT_FRAME_END) {
                const tile_packet_header_t *frame_end_header = NULL;
                uint16_t expected_tile_count = 0;
                if (!validate_frame_end_packet(rx_buffer, len, &frame_end_header, &expected_tile_count)) {
                    rejected_count++;
                    (void)lcd_display_draw_status_marker(STATUS_MARKER_REJECT);
                    send_status(sock, &source_addr, rx_count, accepted_count, rejected_count, draw_failed_count, frame_end_count, flushed_frame_count, stale_frame_end_count);
                    continue;
                }

                accepted_count++;
                frame_end_count++;
                if (unified_flush && dirty.valid) {
                    if (dirty.frame_id != read_le32(frame_end_header->frame_id)) {
                        stale_frame_end_count++;
                    } else if (!dirty_region_has_expected_tiles(&dirty, expected_tile_count)) {
                        ESP_LOGI(TAG, "skip incomplete frame=%lu tiles=%u/%u", dirty.frame_id, dirty.received_tile_count, expected_tile_count);
                        if (nack_enabled) {
                            send_nack_status(
                                sock,
                                &source_addr,
                                rx_count,
                                accepted_count,
                                rejected_count,
                                draw_failed_count,
                                frame_end_count,
                                flushed_frame_count,
                                stale_frame_end_count,
                                dirty.frame_id,
                                &dirty,
                                expected_tile_count);
                        }
                    } else {
                        esp_err_t ret = flush_dirty_region(&dirty);
                        if (ret != ESP_OK) {
                            draw_failed_count++;
                            (void)lcd_display_draw_status_marker(STATUS_MARKER_REJECT);
                            ESP_LOGW(TAG, "flush frame failed: %s", esp_err_to_name(ret));
                        } else {
                            flushed_frame_count++;
                            s_displayed_frame_id = dirty.frame_id;
                        }
                    }
                }
                send_status(sock, &source_addr, rx_count, accepted_count, rejected_count, draw_failed_count, frame_end_count, flushed_frame_count, stale_frame_end_count);
                continue;
            }
            if (raw_header->type == PKT_JPEG_CHUNK) {
                const tile_packet_header_t *jpeg_header = NULL;
                const uint8_t *jpeg_payload = NULL;
                if (!validate_jpeg_chunk_packet(rx_buffer, len, &jpeg_header, &jpeg_payload)) {
                    rejected_count++;
                    (void)lcd_display_draw_status_marker(STATUS_MARKER_REJECT);
                    send_status(sock, &source_addr, rx_count, accepted_count, rejected_count, draw_failed_count, frame_end_count, flushed_frame_count, stale_frame_end_count);
                    continue;
                }

                accepted_count++;
                if (dirty.valid) {
                    dirty.valid = false;
                }
                const uint32_t jpeg_frame_id = read_le32(jpeg_header->frame_id);
                const uint16_t jpeg_chunk_id = read_le16(jpeg_header->tile_id);
                if (jpeg_buffer == NULL) {
                    if (jpeg_chunk_id == 0) {
                        (void)xQueueReceive(s_jpeg_pipeline.free_buffers, &jpeg_buffer, 0);
                    }
                    if (jpeg_buffer == NULL) {
                        if (last_jpeg_no_buffer_frame_id != jpeg_frame_id) {
                            last_jpeg_no_buffer_frame_id = jpeg_frame_id;
                            ++s_jpeg_dropped_frames;
                        }
                        continue;
                    }
                }
                if (jpeg.valid && jpeg.frame_id != jpeg_frame_id && !jpeg_frame_complete(&jpeg)) {
                    ++s_jpeg_incomplete_frames;
                    if (nack_enabled) {
                        ESP_LOGI(TAG, "drop incomplete jpeg frame=%lu chunks=%u/%u for newer frame=%lu", jpeg.frame_id, jpeg.received_chunks_count, jpeg.expected_chunks, jpeg_frame_id);
                        send_jpeg_nack_status(
                            sock,
                            &source_addr,
                            rx_count,
                            accepted_count,
                            rejected_count,
                            draw_failed_count,
                            frame_end_count,
                            flushed_frame_count,
                            stale_frame_end_count,
                            &jpeg);
                    }
                    jpeg.valid = false;
                }
                esp_err_t jpeg_ret = assemble_jpeg_chunk(jpeg_header, jpeg_payload, &jpeg, jpeg_buffer);
                if (jpeg_ret == ESP_OK) {
                    jpeg_work_item_t work = {
                        .frame_id = jpeg_frame_id,
                        .transport_epoch = s_transport_epoch,
                        .width = read_le16(jpeg_header->w),
                        .height = read_le16(jpeg_header->h),
                        .jpeg_len = read_le16(jpeg_header->raw_len),
                        .jpeg_buffer = jpeg_buffer,
                    };
                    bool queued = xQueueSend(s_jpeg_pipeline.complete_frames, &work, 0) == pdTRUE;
                    if (!queued) {
                        jpeg_work_item_t stale;
                        if (xQueueReceive(s_jpeg_pipeline.complete_frames, &stale, 0) == pdTRUE) {
                            ++s_jpeg_dropped_frames;
                            if (xQueueSend(s_jpeg_pipeline.complete_frames, &work, 0) == pdTRUE) {
                                jpeg_buffer = stale.jpeg_buffer;
                                queued = true;
                            } else {
                                (void)xQueueSend(s_jpeg_pipeline.free_buffers, &stale.jpeg_buffer, 0);
                            }
                        }
                    }
                    if (queued) {
                        ++s_jpeg_queued_frames;
                        if (jpeg_buffer == work.jpeg_buffer) {
                            jpeg_buffer = NULL;
                            (void)xQueueReceive(s_jpeg_pipeline.free_buffers, &jpeg_buffer, 0);
                        }
                    } else {
                        ++s_jpeg_dropped_frames;
                    }
                    if (accepted_count == 1 || (accepted_count % UDP_STATUS_INTERVAL_PACKETS) == 0) {
                        ESP_LOGI(TAG, "jpeg frame=%lu bytes=%u chunks=%u", read_le32(jpeg_header->frame_id), read_le16(jpeg_header->raw_len), jpeg.expected_chunks);
                    }
                } else if (jpeg_ret != ESP_ERR_NOT_FINISHED) {
                    draw_failed_count++;
                    (void)lcd_display_draw_status_marker(STATUS_MARKER_REJECT);
                    ESP_LOGW(TAG, "jpeg frame failed: %s", esp_err_to_name(jpeg_ret));
                }
                if (accepted_count == 1 ||
                    (accepted_count % UDP_STATUS_INTERVAL_PACKETS) == 0 ||
                    s_display_perf.report_sequence != last_sent_perf_sequence) {
                    send_status(sock, &source_addr, rx_count, accepted_count, rejected_count, draw_failed_count, frame_end_count, flushed_frame_count, stale_frame_end_count);
                    last_sent_perf_sequence = s_display_perf.report_sequence;
                }
                continue;
            }
        }

        const tile_packet_header_t *header = NULL;
        const uint8_t *payload = NULL;
        if (!validate_tile_packet(rx_buffer, len, &header, &payload)) {
            rejected_count++;
            (void)lcd_display_draw_status_marker(STATUS_MARKER_REJECT);
            send_status(sock, &source_addr, rx_count, accepted_count, rejected_count, draw_failed_count, frame_end_count, flushed_frame_count, stale_frame_end_count);
            continue;
        }

        accepted_count++;
        const uint32_t frame_id = read_le32(header->frame_id);
        if (dirty.frame_id != frame_id) {
            if (!unified_flush && dirty.valid) {
                esp_err_t flush_ret = flush_dirty_region(&dirty);
                if (flush_ret != ESP_OK) {
                    draw_failed_count++;
                    (void)lcd_display_draw_status_marker(STATUS_MARKER_REJECT);
                    ESP_LOGW(TAG, "flush previous frame failed: %s", esp_err_to_name(flush_ret));
                } else {
                    flushed_frame_count++;
                    s_displayed_frame_id = dirty.frame_id;
                }
            }
            reset_dirty_region(&dirty, frame_id);
        }
        if (accepted_count == 1 || (accepted_count % UDP_STATUS_INTERVAL_PACKETS) == 0) {
            ESP_LOGI(
                TAG,
                "tile frame=%lu tile=%u x=%u y=%u w=%u h=%u bytes=%u",
                read_le32(header->frame_id),
                read_le16(header->tile_id),
                read_le16(header->x),
                read_le16(header->y),
                read_le16(header->w),
                read_le16(header->h),
                read_le16(header->payload_len));
        }
        if (!decode_tile_payload(header, payload, tile_decode_buffer)) {
            draw_failed_count++;
            (void)lcd_display_draw_status_marker(STATUS_MARKER_REJECT);
            ESP_LOGW(TAG, "decode tile failed");
            send_status(sock, &source_addr, rx_count, accepted_count, rejected_count, draw_failed_count, frame_end_count, flushed_frame_count, stale_frame_end_count);
            continue;
        }

        xSemaphoreTake(s_display_mutex, portMAX_DELAY);
        esp_err_t ret = unified_flush ? lcd_display_blit_rgb565_to_framebuffer(
            read_le16(header->x),
            read_le16(header->y),
            read_le16(header->w),
            read_le16(header->h),
            tile_decode_buffer,
            read_le16(header->raw_len)) : lcd_display_draw_rgb565(
            read_le16(header->x),
            read_le16(header->y),
            read_le16(header->w),
            read_le16(header->h),
            tile_decode_buffer,
            read_le16(header->raw_len));
        xSemaphoreGive(s_display_mutex);
        if (ret != ESP_OK) {
            draw_failed_count++;
            (void)lcd_display_draw_status_marker(STATUS_MARKER_REJECT);
            ESP_LOGW(TAG, "draw tile failed: %s", esp_err_to_name(ret));
        } else {
            if (unified_flush) {
                include_dirty_tile(&dirty, header);
            }
        }
        if (accepted_count == 1 || (accepted_count % UDP_STATUS_INTERVAL_PACKETS) == 0) {
            send_status(sock, &source_addr, rx_count, accepted_count, rejected_count, draw_failed_count, frame_end_count, flushed_frame_count, stale_frame_end_count);
        }
    }
}

esp_err_t udp_display_start(void)
{
    BaseType_t created = xTaskCreatePinnedToCore(
        udp_display_task,
        "udp_display",
        UDP_DISPLAY_TASK_STACK,
        NULL,
        UDP_DISPLAY_TASK_PRIORITY,
        NULL,
        UDP_DISPLAY_TASK_CORE);
    return created == pdPASS ? ESP_OK : ESP_ERR_NO_MEM;
}
