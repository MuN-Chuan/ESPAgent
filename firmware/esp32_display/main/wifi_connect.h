#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "esp_err.h"
#include "freertos/FreeRTOS.h"

esp_err_t wifi_connect_start(void);
esp_err_t wifi_wait_connected(TickType_t timeout);
esp_err_t wifi_set_credentials(
    const uint8_t *ssid,
    size_t ssid_length,
    const uint8_t *password,
    size_t password_length
);
bool wifi_is_connected(void);
esp_err_t wifi_get_current_ssid(uint8_t *ssid, size_t capacity, size_t *ssid_length);
esp_err_t wifi_get_ip(char *buffer, size_t capacity);
esp_err_t wifi_connect(void);
