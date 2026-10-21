#pragma once

#include <stdint.h>

#define ESPAGENT_MAGIC_DISPLAY 0xA15C
#define ESPAGENT_PROTOCOL_VERSION 1
#define ESPAGENT_PIXEL_RGB565 1
#define ESPAGENT_PIXEL_JPEG 2
#define ESPAGENT_PIXEL_INDEXED1 3
#define ESPAGENT_PIXEL_INDEXED2 4
#define ESPAGENT_PIXEL_INDEXED4 5
#define ESPAGENT_PIXEL_INDEXED8 6
#define ESPAGENT_COMP_RAW 0
#define ESPAGENT_COMP_RLE 1
#define ESPAGENT_COMP_LZ4 2
#define ESPAGENT_DISCOVERY_MESSAGE "ESPAGENT_DISCOVER 1"

typedef enum {
    PKT_FRAME_BEGIN = 1,
    PKT_TILE = 2,
    PKT_FRAME_END = 3,
    PKT_JPEG_CHUNK = 4,
    PKT_INDEXED_RECT = 5,
    PKT_INDEXED_FRAME_END = 6,
    PKT_STATUS = 7,
    PKT_INDEXED_FRAME_CHUNK = 8,
} packet_type_t;

typedef enum {
    ESPAGENT_TRANSPORT_TILES = 0,
    ESPAGENT_TRANSPORT_MJPEG = 1,
    ESPAGENT_TRANSPORT_INDEXED = 2,
} espagent_transport_mode_t;

typedef enum {
    ESPAGENT_DISPLAY_PORTRAIT = 0,
    ESPAGENT_DISPLAY_LANDSCAPE = 1,
} espagent_display_orientation_t;

typedef struct __attribute__((packed)) {
    uint16_t magic;
    uint8_t version;
    uint8_t type;
    uint32_t frame_id;
    uint16_t tile_id;
    uint16_t x;
    uint16_t y;
    uint16_t w;
    uint16_t h;
    uint8_t pixel_format;
    uint8_t compression;
    uint16_t raw_len;
    uint16_t payload_len;
    uint32_t crc32;
} tile_packet_header_t;

_Static_assert(sizeof(tile_packet_header_t) == 28, "display packet header must match Python HEADER_SIZE");
