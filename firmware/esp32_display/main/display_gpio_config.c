#include "display_gpio_config.h"

#include <stddef.h>
#include <stdint.h>

#include "esp_log.h"
#include "nvs.h"

#define DISPLAY_GPIO_NAMESPACE "display_gpio"

static const char *TAG = "display_gpio";

typedef struct {
    const char *key;
    size_t offset;
} gpio_field_t;

static const gpio_field_t GPIO_FIELDS[] = {
    {"scl", offsetof(display_gpio_config_t, scl)},
    {"sda", offsetof(display_gpio_config_t, sda)},
    {"cs", offsetof(display_gpio_config_t, cs)},
    {"dc", offsetof(display_gpio_config_t, dc)},
    {"rst", offsetof(display_gpio_config_t, rst)},
    {"bl", offsetof(display_gpio_config_t, bl)},
};

static gpio_num_t *field_value(display_gpio_config_t *config, size_t offset)
{
    return (gpio_num_t *)((uint8_t *)config + offset);
}

static const gpio_num_t *const_field_value(const display_gpio_config_t *config, size_t offset)
{
    return (const gpio_num_t *)((const uint8_t *)config + offset);
}

static bool is_reserved_gpio(gpio_num_t gpio)
{
    // The product firmware uses octal PSRAM/Flash and UART0 as its USB recovery path.
    return (gpio >= GPIO_NUM_26 && gpio <= GPIO_NUM_37) || gpio == GPIO_NUM_43 || gpio == GPIO_NUM_44;
}

void display_gpio_config_defaults(display_gpio_config_t *config)
{
    if (config == NULL) {
        return;
    }
    *config = (display_gpio_config_t){
        .scl = DISPLAY_GPIO_DEFAULT_SCL,
        .sda = DISPLAY_GPIO_DEFAULT_SDA,
        .cs = DISPLAY_GPIO_DEFAULT_CS,
        .dc = DISPLAY_GPIO_DEFAULT_DC,
        .rst = DISPLAY_GPIO_DEFAULT_RST,
        .bl = DISPLAY_GPIO_DEFAULT_BL,
    };
}

bool display_gpio_config_is_valid(const display_gpio_config_t *config)
{
    if (config == NULL) {
        return false;
    }
    const gpio_num_t values[] = {config->scl, config->sda, config->cs, config->dc, config->rst, config->bl};
    for (size_t i = 0; i < sizeof(values) / sizeof(values[0]); ++i) {
        if (!GPIO_IS_VALID_OUTPUT_GPIO(values[i]) || is_reserved_gpio(values[i])) {
            return false;
        }
        for (size_t j = i + 1; j < sizeof(values) / sizeof(values[0]); ++j) {
            if (values[i] == values[j]) {
                return false;
            }
        }
    }
    return true;
}

esp_err_t display_gpio_config_load(display_gpio_config_t *config)
{
    if (config == NULL) {
        return ESP_ERR_INVALID_ARG;
    }
    display_gpio_config_defaults(config);

    nvs_handle_t handle;
    esp_err_t ret = nvs_open(DISPLAY_GPIO_NAMESPACE, NVS_READONLY, &handle);
    if (ret == ESP_ERR_NVS_NOT_FOUND) {
        return ESP_OK;
    }
    if (ret != ESP_OK) {
        return ret;
    }

    for (size_t i = 0; i < sizeof(GPIO_FIELDS) / sizeof(GPIO_FIELDS[0]); ++i) {
        int8_t stored = 0;
        ret = nvs_get_i8(handle, GPIO_FIELDS[i].key, &stored);
        if (ret != ESP_OK) {
            nvs_close(handle);
            display_gpio_config_defaults(config);
            return ret == ESP_ERR_NVS_NOT_FOUND ? ESP_OK : ret;
        }
        *field_value(config, GPIO_FIELDS[i].offset) = (gpio_num_t)stored;
    }
    nvs_close(handle);

    if (!display_gpio_config_is_valid(config)) {
        ESP_LOGW(TAG, "stored display GPIO map is invalid; using defaults");
        display_gpio_config_defaults(config);
    }
    return ESP_OK;
}

esp_err_t display_gpio_config_save(const display_gpio_config_t *config)
{
    if (!display_gpio_config_is_valid(config)) {
        return ESP_ERR_INVALID_ARG;
    }

    nvs_handle_t handle;
    esp_err_t ret = nvs_open(DISPLAY_GPIO_NAMESPACE, NVS_READWRITE, &handle);
    if (ret != ESP_OK) {
        return ret;
    }
    for (size_t i = 0; i < sizeof(GPIO_FIELDS) / sizeof(GPIO_FIELDS[0]); ++i) {
        ret = nvs_set_i8(handle, GPIO_FIELDS[i].key, (int8_t)*const_field_value(config, GPIO_FIELDS[i].offset));
        if (ret != ESP_OK) {
            nvs_close(handle);
            return ret;
        }
    }
    ret = nvs_commit(handle);
    nvs_close(handle);
    return ret;
}
