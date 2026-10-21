#pragma once

#include <stdbool.h>
#include <stdint.h>

#include "esp_err.h"

#define LCD_PORTRAIT_H_RES 240
#define LCD_PORTRAIT_V_RES 320
#define LCD_LANDSCAPE_H_RES 320
#define LCD_LANDSCAPE_V_RES 240
#define LCD_PIXEL_COUNT (LCD_PORTRAIT_H_RES * LCD_PORTRAIT_V_RES)
#define LCD_MAX_LINE_PIXELS LCD_LANDSCAPE_H_RES

#define LCD_GAMMA_22 0x01
#define LCD_GAMMA_18 0x02
#define LCD_GAMMA_25 0x04
#define LCD_GAMMA_10 0x08

esp_err_t lcd_display_init(void);
esp_err_t lcd_display_set_orientation(bool landscape);
uint16_t lcd_display_get_width(void);
uint16_t lcd_display_get_height(void);
esp_err_t lcd_display_set_backlight_percent(uint8_t percent);
esp_err_t lcd_display_set_gamma_curve(uint8_t gamma_curve);
uint8_t lcd_display_get_gamma_curve(void);
esp_err_t lcd_display_draw_boot_pattern(void);
esp_err_t lcd_display_draw_status_marker(uint16_t rgb565_color);
esp_err_t lcd_display_draw_rgb565(uint16_t x, uint16_t y, uint16_t w, uint16_t h, const uint8_t *rgb565_le, uint16_t len);
esp_err_t lcd_display_draw_rgb565_frame(uint16_t x, uint16_t y, uint16_t w, uint16_t h, const uint8_t *rgb565_le, uint32_t len);
void lcd_display_begin_direct_pipeline(void);
esp_err_t lcd_display_submit_rgb565_be(uint16_t x, uint16_t y, uint16_t w, uint16_t h, const uint8_t *rgb565_be, uint32_t len);
esp_err_t lcd_display_wait_direct_transfer(void);
esp_err_t lcd_display_blit_rgb565_to_framebuffer(uint16_t x, uint16_t y, uint16_t w, uint16_t h, const uint8_t *rgb565_le, uint16_t len);
esp_err_t lcd_display_blit_rgb565_to_framebuffer_u32(uint16_t x, uint16_t y, uint16_t w, uint16_t h, const uint8_t *rgb565_le, uint32_t len);
esp_err_t lcd_display_flush_framebuffer(uint16_t x, uint16_t y, uint16_t w, uint16_t h);
