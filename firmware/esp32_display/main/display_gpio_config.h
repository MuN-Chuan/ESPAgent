#pragma once

#include <stdbool.h>

#include "driver/gpio.h"
#include "esp_err.h"

#define DISPLAY_GPIO_DEFAULT_SCL GPIO_NUM_12
#define DISPLAY_GPIO_DEFAULT_SDA GPIO_NUM_11
#define DISPLAY_GPIO_DEFAULT_CS GPIO_NUM_10
#define DISPLAY_GPIO_DEFAULT_DC GPIO_NUM_8
#define DISPLAY_GPIO_DEFAULT_RST GPIO_NUM_7
#define DISPLAY_GPIO_DEFAULT_BL GPIO_NUM_6

typedef struct {
    gpio_num_t scl;
    gpio_num_t sda;
    gpio_num_t cs;
    gpio_num_t dc;
    gpio_num_t rst;
    gpio_num_t bl;
} display_gpio_config_t;

void display_gpio_config_defaults(display_gpio_config_t *config);
bool display_gpio_config_is_valid(const display_gpio_config_t *config);
esp_err_t display_gpio_config_load(display_gpio_config_t *config);
esp_err_t display_gpio_config_save(const display_gpio_config_t *config);
