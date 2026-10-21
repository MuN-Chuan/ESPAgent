#include "lcd_display.h"

#include <stdbool.h>
#include <string.h>

#include "driver/gpio.h"
#include "driver/ledc.h"
#include "driver/spi_master.h"
#include "display_gpio_config.h"
#include "esp_check.h"
#include "esp_heap_caps.h"
#include "esp_lcd_panel_io.h"
#include "esp_lcd_panel_ops.h"
#include "esp_lcd_panel_vendor.h"
#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"

#define LCD_HOST SPI2_HOST
#define LCD_PIXEL_CLOCK_HZ (80 * 1000 * 1000)
#define LCD_CMD_BITS 8
#define LCD_PARAM_BITS 8
#define LCD_BACKLIGHT_ON_LEVEL 1
#define LCD_BACKLIGHT_LEDC_MODE LEDC_LOW_SPEED_MODE
#define LCD_BACKLIGHT_LEDC_TIMER LEDC_TIMER_0
#define LCD_BACKLIGHT_LEDC_CHANNEL LEDC_CHANNEL_0
#define LCD_BACKLIGHT_LEDC_RESOLUTION LEDC_TIMER_10_BIT
#define LCD_BACKLIGHT_LEDC_MAX_DUTY ((1 << 10) - 1)
#define LCD_BACKLIGHT_LEDC_FREQ_HZ 5000
#define LCD_TRANSFER_LINES 40
#define LCD_DMA_BUFFER_COUNT 2
#define LCD_DMA_BUFFER_BYTES (LCD_MAX_LINE_PIXELS * LCD_TRANSFER_LINES * sizeof(uint16_t))
#define LCD_FRAMEBUFFER_BYTES (LCD_PIXEL_COUNT * sizeof(uint16_t))
#define LCD_DRAW_TIMEOUT_MS 1000
#define LCD_STATUS_MARKER_SIZE 16
#define LCD_CMD_GAMSET 0x26

static const char *TAG = "lcd";
static esp_lcd_panel_handle_t s_panel;
static esp_lcd_panel_io_handle_t s_panel_io;
static SemaphoreHandle_t s_color_done;
static uint8_t *s_dma_buffers[LCD_DMA_BUFFER_COUNT];
static uint8_t *s_framebuffer;
static uint8_t s_backlight_percent = 100;
static uint8_t s_gamma_curve;
static display_gpio_config_t s_gpio_config;
static uint16_t s_width = LCD_PORTRAIT_H_RES;
static uint16_t s_height = LCD_PORTRAIT_V_RES;

#define LCD_H_RES s_width
#define LCD_V_RES s_height

static bool on_color_trans_done(esp_lcd_panel_io_handle_t panel_io, esp_lcd_panel_io_event_data_t *edata, void *user_ctx)
{
    BaseType_t high_task_wakeup = pdFALSE;
    xSemaphoreGiveFromISR(s_color_done, &high_task_wakeup);
    return high_task_wakeup == pdTRUE;
}

static esp_err_t wait_color_done(void)
{
    if (xSemaphoreTake(s_color_done, pdMS_TO_TICKS(LCD_DRAW_TIMEOUT_MS)) != pdTRUE) {
        return ESP_ERR_TIMEOUT;
    }
    return ESP_OK;
}

static void drain_color_done(void)
{
    while (xSemaphoreTake(s_color_done, 0) == pdTRUE) {
    }
}

static esp_err_t submit_dma_buffer(uint8_t *buffer, uint16_t x, uint16_t y, uint16_t w, uint16_t h)
{
    return esp_lcd_panel_draw_bitmap(s_panel, x, y, x + w, y + h, buffer);
}

static esp_err_t draw_dma_buffer(uint8_t *buffer, uint16_t x, uint16_t y, uint16_t w, uint16_t h)
{
    ESP_RETURN_ON_ERROR(submit_dma_buffer(buffer, x, y, w, h), TAG, "draw bitmap");
    return wait_color_done();
}

static esp_err_t wait_dma_pipeline(size_t *in_flight)
{
    while (*in_flight > 0) {
        ESP_RETURN_ON_ERROR(wait_color_done(), TAG, "wait DMA pipeline");
        --(*in_flight);
    }
    return ESP_OK;
}

static esp_err_t clear_panel(void)
{
    memset(s_dma_buffers[0], 0, LCD_DMA_BUFFER_BYTES);
    if (s_framebuffer != NULL) {
        memset(s_framebuffer, 0, LCD_FRAMEBUFFER_BYTES);
    }
    for (uint16_t y = 0; y < LCD_V_RES; y += 32) {
        const uint16_t h = (y + 32 > LCD_V_RES) ? (LCD_V_RES - y) : 32;
        for (uint16_t x = 0; x < LCD_H_RES; x += 32) {
            const uint16_t w = (x + 32 > LCD_H_RES) ? (LCD_H_RES - x) : 32;
            ESP_RETURN_ON_ERROR(draw_dma_buffer(s_dma_buffers[0], x, y, w, h), TAG, "clear panel");
        }
    }
    return ESP_OK;
}

static uint16_t rgb565(uint8_t r, uint8_t g, uint8_t b)
{
    return ((uint16_t)(r & 0xF8) << 8) | ((uint16_t)(g & 0xFC) << 3) | (uint16_t)(b >> 3);
}

static void write_rgb565_be(uint8_t *target, uint16_t color)
{
    target[0] = (uint8_t)(color >> 8);
    target[1] = (uint8_t)(color & 0xFF);
}

static uint16_t boot_pattern_color(uint16_t x, uint16_t y)
{
    static const uint16_t bars[] = {
        0xFFFF, // white
        0xFFE0, // yellow
        0x07FF, // cyan
        0x07E0, // green
        0xF81F, // magenta
        0xF800, // red
        0x001F, // blue
        0x0000, // black
    };
    const uint16_t bar_width = LCD_H_RES / (uint16_t)(sizeof(bars) / sizeof(bars[0]));
    uint16_t color = bars[x / bar_width < 8 ? x / bar_width : 7];
    if ((x % 40) == 0 || (y % 40) == 0 || x == 0 || y == 0 || x == LCD_H_RES - 1 || y == LCD_V_RES - 1) {
        color = rgb565(255, 128, 0);
    }
    return color;
}

esp_err_t lcd_display_draw_boot_pattern(void)
{
    if (s_panel == NULL) {
        return ESP_ERR_INVALID_STATE;
    }

    ESP_LOGI(TAG, "drawing local LCD boot test pattern");
    for (uint16_t y = 0; y < LCD_V_RES; y += 32) {
        const uint16_t h = (y + 32 > LCD_V_RES) ? (LCD_V_RES - y) : 32;
        for (uint16_t x = 0; x < LCD_H_RES; x += 32) {
            const uint16_t w = (x + 32 > LCD_H_RES) ? (LCD_H_RES - x) : 32;
            uint16_t out = 0;
            for (uint16_t row = 0; row < h; ++row) {
                for (uint16_t col = 0; col < w; ++col) {
                    write_rgb565_be(&s_dma_buffers[0][out], boot_pattern_color(x + col, y + row));
                    out += 2;
                }
            }
            drain_color_done();
            ESP_RETURN_ON_ERROR(draw_dma_buffer(s_dma_buffers[0], x, y, w, h), TAG, "draw boot pattern");
        }
    }
    return ESP_OK;
}

esp_err_t lcd_display_draw_status_marker(uint16_t rgb565_color)
{
    if (s_panel == NULL) {
        return ESP_ERR_INVALID_STATE;
    }

    uint16_t out = 0;
    for (uint16_t i = 0; i < LCD_STATUS_MARKER_SIZE * LCD_STATUS_MARKER_SIZE; ++i) {
        write_rgb565_be(&s_dma_buffers[0][out], rgb565_color);
        out += 2;
    }

    drain_color_done();
    return draw_dma_buffer(s_dma_buffers[0], LCD_H_RES - LCD_STATUS_MARKER_SIZE, 0, LCD_STATUS_MARKER_SIZE, LCD_STATUS_MARKER_SIZE);
}

esp_err_t lcd_display_init(void)
{
    if (s_panel != NULL) {
        return ESP_OK;
    }

    ESP_RETURN_ON_ERROR(display_gpio_config_load(&s_gpio_config), TAG, "load display GPIO config");
    ESP_LOGI(TAG, "configure LCD backlight pwm gpio=%d", s_gpio_config.bl);
    ledc_timer_config_t backlight_timer = {
        .speed_mode = LCD_BACKLIGHT_LEDC_MODE,
        .duty_resolution = LCD_BACKLIGHT_LEDC_RESOLUTION,
        .timer_num = LCD_BACKLIGHT_LEDC_TIMER,
        .freq_hz = LCD_BACKLIGHT_LEDC_FREQ_HZ,
        .clk_cfg = LEDC_AUTO_CLK,
    };
    ESP_RETURN_ON_ERROR(ledc_timer_config(&backlight_timer), TAG, "configure backlight timer");
    ledc_channel_config_t backlight_channel = {
        .gpio_num = s_gpio_config.bl,
        .speed_mode = LCD_BACKLIGHT_LEDC_MODE,
        .channel = LCD_BACKLIGHT_LEDC_CHANNEL,
        .intr_type = LEDC_INTR_DISABLE,
        .timer_sel = LCD_BACKLIGHT_LEDC_TIMER,
        .duty = 0,
        .hpoint = 0,
        .flags.output_invert = LCD_BACKLIGHT_ON_LEVEL ? 0 : 1,
    };
    ESP_RETURN_ON_ERROR(ledc_channel_config(&backlight_channel), TAG, "configure backlight channel");

    ESP_LOGI(TAG, "initialize SPI LCD bus scl=%d sda=%d cs=%d dc=%d rst=%d", s_gpio_config.scl, s_gpio_config.sda, s_gpio_config.cs, s_gpio_config.dc, s_gpio_config.rst);
    spi_bus_config_t bus_config = {
        .sclk_io_num = s_gpio_config.scl,
        .mosi_io_num = s_gpio_config.sda,
        .miso_io_num = -1,
        .quadwp_io_num = -1,
        .quadhd_io_num = -1,
        .max_transfer_sz = LCD_DMA_BUFFER_BYTES,
    };
    ESP_RETURN_ON_ERROR(spi_bus_initialize(LCD_HOST, &bus_config, SPI_DMA_CH_AUTO), TAG, "initialize SPI bus");

    esp_lcd_panel_io_spi_config_t io_config = {
        .dc_gpio_num = s_gpio_config.dc,
        .cs_gpio_num = s_gpio_config.cs,
        .pclk_hz = LCD_PIXEL_CLOCK_HZ,
        .lcd_cmd_bits = LCD_CMD_BITS,
        .lcd_param_bits = LCD_PARAM_BITS,
        .spi_mode = 0,
        .trans_queue_depth = 10,
    };
    ESP_RETURN_ON_ERROR(esp_lcd_new_panel_io_spi((esp_lcd_spi_bus_handle_t)LCD_HOST, &io_config, &s_panel_io), TAG, "create panel io");

    s_color_done = xSemaphoreCreateCounting(LCD_DMA_BUFFER_COUNT, 0);
    ESP_RETURN_ON_FALSE(s_color_done != NULL, ESP_ERR_NO_MEM, TAG, "create color done semaphore");
    for (size_t i = 0; i < LCD_DMA_BUFFER_COUNT; ++i) {
        s_dma_buffers[i] = heap_caps_malloc(LCD_DMA_BUFFER_BYTES, MALLOC_CAP_DMA);
        ESP_RETURN_ON_FALSE(s_dma_buffers[i] != NULL, ESP_ERR_NO_MEM, TAG, "allocate local DMA buffer");
    }
    s_framebuffer = heap_caps_malloc(LCD_FRAMEBUFFER_BYTES, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
    if (s_framebuffer == NULL) {
        s_framebuffer = heap_caps_malloc(LCD_FRAMEBUFFER_BYTES, MALLOC_CAP_8BIT);
    }
    ESP_RETURN_ON_FALSE(s_framebuffer != NULL, ESP_ERR_NO_MEM, TAG, "allocate framebuffer");
    const esp_lcd_panel_io_callbacks_t callbacks = {
        .on_color_trans_done = on_color_trans_done,
    };
    ESP_RETURN_ON_ERROR(esp_lcd_panel_io_register_event_callbacks(s_panel_io, &callbacks, NULL), TAG, "register panel callbacks");

    esp_lcd_panel_dev_config_t panel_config = {
        .reset_gpio_num = s_gpio_config.rst,
        .rgb_ele_order = LCD_RGB_ELEMENT_ORDER_RGB,
        .data_endian = LCD_RGB_DATA_ENDIAN_BIG,
        .bits_per_pixel = 16,
    };
    ESP_RETURN_ON_ERROR(esp_lcd_new_panel_st7789(s_panel_io, &panel_config, &s_panel), TAG, "create ST7789 panel");
    ESP_RETURN_ON_ERROR(esp_lcd_panel_reset(s_panel), TAG, "reset panel");
    ESP_RETURN_ON_ERROR(esp_lcd_panel_init(s_panel), TAG, "init panel");
    ESP_RETURN_ON_ERROR(lcd_display_set_gamma_curve(LCD_GAMMA_18), TAG, "set panel gamma");
    ESP_RETURN_ON_ERROR(esp_lcd_panel_invert_color(s_panel, true), TAG, "invert panel colors");
    ESP_RETURN_ON_ERROR(esp_lcd_panel_disp_on_off(s_panel, true), TAG, "turn display on");
    ESP_RETURN_ON_ERROR(clear_panel(), TAG, "clear panel");

    ESP_RETURN_ON_ERROR(lcd_display_set_backlight_percent(s_backlight_percent), TAG, "set backlight");
    ESP_LOGI(TAG, "LCD initialized");
    return ESP_OK;
}

esp_err_t lcd_display_set_orientation(bool landscape)
{
    if (s_panel == NULL) {
        return ESP_ERR_INVALID_STATE;
    }
    const uint16_t next_width = landscape ? LCD_LANDSCAPE_H_RES : LCD_PORTRAIT_H_RES;
    const uint16_t next_height = landscape ? LCD_LANDSCAPE_V_RES : LCD_PORTRAIT_V_RES;
    if (s_width == next_width && s_height == next_height) {
        return ESP_OK;
    }

    ESP_RETURN_ON_ERROR(esp_lcd_panel_swap_xy(s_panel, landscape), TAG, "swap panel axes");
    ESP_RETURN_ON_ERROR(esp_lcd_panel_mirror(s_panel, landscape, false), TAG, "mirror rotated panel");
    s_width = next_width;
    s_height = next_height;
    ESP_RETURN_ON_ERROR(clear_panel(), TAG, "clear rotated panel");
    ESP_LOGI(TAG, "LCD orientation=%s size=%ux%u", landscape ? "landscape" : "portrait", s_width, s_height);
    return ESP_OK;
}

uint16_t lcd_display_get_width(void)
{
    return s_width;
}

uint16_t lcd_display_get_height(void)
{
    return s_height;
}

esp_err_t lcd_display_set_backlight_percent(uint8_t percent)
{
    if (percent > 100) {
        percent = 100;
    }
    const uint32_t duty = ((uint32_t)percent * LCD_BACKLIGHT_LEDC_MAX_DUTY) / 100;
    ESP_RETURN_ON_ERROR(ledc_set_duty(LCD_BACKLIGHT_LEDC_MODE, LCD_BACKLIGHT_LEDC_CHANNEL, duty), TAG, "set backlight duty");
    ESP_RETURN_ON_ERROR(ledc_update_duty(LCD_BACKLIGHT_LEDC_MODE, LCD_BACKLIGHT_LEDC_CHANNEL), TAG, "update backlight duty");
    s_backlight_percent = percent;
    return ESP_OK;
}

esp_err_t lcd_display_set_gamma_curve(uint8_t gamma_curve)
{
    if (s_panel_io == NULL) {
        return ESP_ERR_INVALID_STATE;
    }
    if (gamma_curve != LCD_GAMMA_22 && gamma_curve != LCD_GAMMA_18 &&
        gamma_curve != LCD_GAMMA_25 && gamma_curve != LCD_GAMMA_10) {
        return ESP_ERR_INVALID_ARG;
    }
    if (gamma_curve == s_gamma_curve) {
        return ESP_OK;
    }
    esp_err_t ret = esp_lcd_panel_io_tx_param(s_panel_io, LCD_CMD_GAMSET, &gamma_curve, sizeof(gamma_curve));
    if (ret == ESP_OK) {
        s_gamma_curve = gamma_curve;
    }
    return ret;
}

uint8_t lcd_display_get_gamma_curve(void)
{
    return s_gamma_curve;
}

esp_err_t lcd_display_draw_rgb565(uint16_t x, uint16_t y, uint16_t w, uint16_t h, const uint8_t *rgb565_le, uint16_t len)
{
    if (s_panel == NULL) {
        return ESP_ERR_INVALID_STATE;
    }
    if (rgb565_le == NULL) {
        return ESP_ERR_INVALID_ARG;
    }
    if (w == 0 || h == 0 || x >= LCD_H_RES || y >= LCD_V_RES || x + w > LCD_H_RES || y + h > LCD_V_RES) {
        return ESP_ERR_INVALID_ARG;
    }
    if (len != (uint16_t)(w * h * 2)) {
        return ESP_ERR_INVALID_SIZE;
    }
    if (len > LCD_DMA_BUFFER_BYTES) {
        return ESP_ERR_INVALID_SIZE;
    }

    drain_color_done();
    for (uint16_t i = 0; i < len; i += 2) {
        s_dma_buffers[0][i] = rgb565_le[i + 1];
        s_dma_buffers[0][i + 1] = rgb565_le[i];
    }

    return draw_dma_buffer(s_dma_buffers[0], x, y, w, h);
}

esp_err_t lcd_display_draw_rgb565_frame(uint16_t x, uint16_t y, uint16_t w, uint16_t h, const uint8_t *rgb565_le, uint32_t len)
{
    if (s_panel == NULL) {
        return ESP_ERR_INVALID_STATE;
    }
    if (rgb565_le == NULL) {
        return ESP_ERR_INVALID_ARG;
    }
    if (w == 0 || h == 0 || x >= LCD_H_RES || y >= LCD_V_RES || x + w > LCD_H_RES || y + h > LCD_V_RES) {
        return ESP_ERR_INVALID_ARG;
    }
    if (len != (uint32_t)w * h * 2) {
        return ESP_ERR_INVALID_SIZE;
    }
    if ((size_t)w * LCD_TRANSFER_LINES * sizeof(uint16_t) > LCD_DMA_BUFFER_BYTES) {
        return ESP_ERR_INVALID_SIZE;
    }

    drain_color_done();
    size_t in_flight = 0;
    size_t buffer_index = 0;
    for (uint16_t row_start = 0; row_start < h; row_start += LCD_TRANSFER_LINES) {
        if (in_flight == LCD_DMA_BUFFER_COUNT) {
            ESP_RETURN_ON_ERROR(wait_color_done(), TAG, "wait reusable RGB565 DMA buffer");
            --in_flight;
        }
        const uint16_t rows = (row_start + LCD_TRANSFER_LINES > h) ? (h - row_start) : LCD_TRANSFER_LINES;
        uint8_t *dma_buffer = s_dma_buffers[buffer_index];
        uint32_t out = 0;
        for (uint16_t row = 0; row < rows; ++row) {
            const uint8_t *source = &rgb565_le[((row_start + row) * w) * 2];
            for (uint16_t col = 0; col < w; ++col) {
                dma_buffer[out++] = source[col * 2 + 1];
                dma_buffer[out++] = source[col * 2];
            }
        }
        esp_err_t ret = submit_dma_buffer(dma_buffer, x, y + row_start, w, rows);
        if (ret != ESP_OK) {
            (void)wait_dma_pipeline(&in_flight);
            return ret;
        }
        ++in_flight;
        buffer_index = (buffer_index + 1) % LCD_DMA_BUFFER_COUNT;
    }
    return wait_dma_pipeline(&in_flight);
}

void lcd_display_begin_direct_pipeline(void)
{
    drain_color_done();
}

esp_err_t lcd_display_submit_rgb565_be(uint16_t x, uint16_t y, uint16_t w, uint16_t h, const uint8_t *rgb565_be, uint32_t len)
{
    if (s_panel == NULL) {
        return ESP_ERR_INVALID_STATE;
    }
    if (rgb565_be == NULL) {
        return ESP_ERR_INVALID_ARG;
    }
    if (w == 0 || h == 0 || x >= LCD_H_RES || y >= LCD_V_RES || x + w > LCD_H_RES || y + h > LCD_V_RES) {
        return ESP_ERR_INVALID_ARG;
    }
    if (len != (uint32_t)w * h * sizeof(uint16_t)) {
        return ESP_ERR_INVALID_SIZE;
    }
    return submit_dma_buffer((uint8_t *)rgb565_be, x, y, w, h);
}

esp_err_t lcd_display_wait_direct_transfer(void)
{
    return wait_color_done();
}

esp_err_t lcd_display_blit_rgb565_to_framebuffer(uint16_t x, uint16_t y, uint16_t w, uint16_t h, const uint8_t *rgb565_le, uint16_t len)
{
    return lcd_display_blit_rgb565_to_framebuffer_u32(x, y, w, h, rgb565_le, len);
}

esp_err_t lcd_display_blit_rgb565_to_framebuffer_u32(uint16_t x, uint16_t y, uint16_t w, uint16_t h, const uint8_t *rgb565_le, uint32_t len)
{
    if (s_framebuffer == NULL) {
        return ESP_ERR_INVALID_STATE;
    }
    if (rgb565_le == NULL) {
        return ESP_ERR_INVALID_ARG;
    }
    if (w == 0 || h == 0 || x >= LCD_H_RES || y >= LCD_V_RES || x + w > LCD_H_RES || y + h > LCD_V_RES) {
        return ESP_ERR_INVALID_ARG;
    }
    if (len != (uint32_t)w * h * 2) {
        return ESP_ERR_INVALID_SIZE;
    }

    for (uint16_t row = 0; row < h; ++row) {
        uint8_t *target = &s_framebuffer[((y + row) * LCD_H_RES + x) * 2];
        const uint8_t *source = &rgb565_le[row * w * 2];
        for (uint16_t col = 0; col < w; ++col) {
            target[col * 2] = source[col * 2 + 1];
            target[col * 2 + 1] = source[col * 2];
        }
    }
    return ESP_OK;
}

esp_err_t lcd_display_flush_framebuffer(uint16_t x, uint16_t y, uint16_t w, uint16_t h)
{
    if (s_panel == NULL || s_framebuffer == NULL) {
        return ESP_ERR_INVALID_STATE;
    }
    if (w == 0 || h == 0 || x >= LCD_H_RES || y >= LCD_V_RES || x + w > LCD_H_RES || y + h > LCD_V_RES) {
        return ESP_ERR_INVALID_ARG;
    }
    if ((size_t)w * LCD_TRANSFER_LINES * sizeof(uint16_t) > LCD_DMA_BUFFER_BYTES) {
        return ESP_ERR_INVALID_SIZE;
    }

    drain_color_done();
    size_t in_flight = 0;
    size_t buffer_index = 0;
    for (uint16_t row_start = 0; row_start < h; row_start += LCD_TRANSFER_LINES) {
        if (in_flight == LCD_DMA_BUFFER_COUNT) {
            ESP_RETURN_ON_ERROR(wait_color_done(), TAG, "wait reusable framebuffer DMA buffer");
            --in_flight;
        }
        const uint16_t rows = (row_start + LCD_TRANSFER_LINES > h) ? (h - row_start) : LCD_TRANSFER_LINES;
        uint8_t *dma_buffer = s_dma_buffers[buffer_index];
        uint16_t out = 0;
        for (uint16_t row = 0; row < rows; ++row) {
            const uint8_t *source = &s_framebuffer[((y + row_start + row) * LCD_H_RES + x) * 2];
            memcpy(&dma_buffer[out], source, w * sizeof(uint16_t));
            out += w * sizeof(uint16_t);
        }
        esp_err_t ret = submit_dma_buffer(dma_buffer, x, y + row_start, w, rows);
        if (ret != ESP_OK) {
            (void)wait_dma_pipeline(&in_flight);
            return ret;
        }
        ++in_flight;
        buffer_index = (buffer_index + 1) % LCD_DMA_BUFFER_COUNT;
    }
    return wait_dma_pipeline(&in_flight);
}
