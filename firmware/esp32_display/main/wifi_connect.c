#include <stdio.h>
#include <string.h>

#include "esp_event.h"
#include "esp_log.h"
#include "esp_netif.h"
#include "esp_wifi.h"
#include "freertos/FreeRTOS.h"
#include "freertos/event_groups.h"
#include "nvs_flash.h"
#include "sdkconfig.h"
#include "wifi_connect.h"

#define WIFI_CONNECTED_BIT BIT0

static const char *TAG = "wifi";
static EventGroupHandle_t s_wifi_event_group;
static esp_netif_t *s_station_netif;
static bool s_has_credentials;
static bool s_started;

static void wifi_event_handler(void *arg, esp_event_base_t event_base, int32_t event_id, void *event_data)
{
    if (event_base == WIFI_EVENT && event_id == WIFI_EVENT_STA_START) {
        if (s_has_credentials) {
            esp_wifi_connect();
        }
        return;
    }

    if (event_base == WIFI_EVENT && event_id == WIFI_EVENT_STA_DISCONNECTED) {
        xEventGroupClearBits(s_wifi_event_group, WIFI_CONNECTED_BIT);
        if (s_has_credentials) {
            ESP_LOGW(TAG, "Wi-Fi disconnected, retrying");
            esp_wifi_connect();
        }
        return;
    }

    if (event_base == IP_EVENT && event_id == IP_EVENT_STA_GOT_IP) {
        const ip_event_got_ip_t *event = (const ip_event_got_ip_t *)event_data;
        ESP_LOGI(TAG, "Wi-Fi connected, ip=" IPSTR, IP2STR(&event->ip_info.ip));
        xEventGroupSetBits(s_wifi_event_group, WIFI_CONNECTED_BIT);
    }
}

static esp_err_t init_nvs(void)
{
    esp_err_t ret = nvs_flash_init();
    if (ret == ESP_ERR_NVS_NO_FREE_PAGES || ret == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        ESP_ERROR_CHECK(nvs_flash_erase());
        ret = nvs_flash_init();
    }
    return ret;
}

static bool config_has_ssid(const wifi_config_t *config)
{
    return config->sta.ssid[0] != '\0';
}

esp_err_t wifi_connect_start(void)
{
    if (s_started) {
        return ESP_OK;
    }

    ESP_ERROR_CHECK(init_nvs());
    ESP_ERROR_CHECK(esp_netif_init());
    ESP_ERROR_CHECK(esp_event_loop_create_default());
    s_station_netif = esp_netif_create_default_wifi_sta();
    if (s_station_netif == NULL) {
        return ESP_ERR_NO_MEM;
    }

    s_wifi_event_group = xEventGroupCreate();
    if (s_wifi_event_group == NULL) {
        return ESP_ERR_NO_MEM;
    }

    wifi_init_config_t init_config = WIFI_INIT_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_wifi_init(&init_config));
    ESP_ERROR_CHECK(esp_event_handler_register(WIFI_EVENT, ESP_EVENT_ANY_ID, &wifi_event_handler, NULL));
    ESP_ERROR_CHECK(esp_event_handler_register(IP_EVENT, IP_EVENT_STA_GOT_IP, &wifi_event_handler, NULL));
    ESP_ERROR_CHECK(esp_wifi_set_storage(WIFI_STORAGE_FLASH));
    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_STA));

    wifi_config_t wifi_config = {0};
    esp_err_t stored_config_result = esp_wifi_get_config(WIFI_IF_STA, &wifi_config);
    if (stored_config_result != ESP_OK || !config_has_ssid(&wifi_config)) {
        snprintf((char *)wifi_config.sta.ssid, sizeof(wifi_config.sta.ssid), "%s", CONFIG_ESPAGENT_WIFI_SSID);
        snprintf((char *)wifi_config.sta.password, sizeof(wifi_config.sta.password), "%s", CONFIG_ESPAGENT_WIFI_PASSWORD);
        wifi_config.sta.threshold.authmode =
            strlen(CONFIG_ESPAGENT_WIFI_PASSWORD) == 0 ? WIFI_AUTH_OPEN : WIFI_AUTH_WPA2_PSK;
        if (config_has_ssid(&wifi_config)) {
            ESP_ERROR_CHECK(esp_wifi_set_config(WIFI_IF_STA, &wifi_config));
        }
    }

    s_has_credentials = config_has_ssid(&wifi_config);
    ESP_ERROR_CHECK(esp_wifi_start());
    s_started = true;

    if (s_has_credentials) {
        ESP_LOGI(TAG, "connecting to saved Wi-Fi");
    } else {
        ESP_LOGW(TAG, "Wi-Fi is not provisioned; waiting for USB configuration");
    }
    return ESP_OK;
}

esp_err_t wifi_wait_connected(TickType_t timeout)
{
    if (!s_started || s_wifi_event_group == NULL) {
        return ESP_ERR_INVALID_STATE;
    }
    EventBits_t bits = xEventGroupWaitBits(
        s_wifi_event_group,
        WIFI_CONNECTED_BIT,
        pdFALSE,
        pdTRUE,
        timeout
    );
    return (bits & WIFI_CONNECTED_BIT) != 0 ? ESP_OK : ESP_ERR_TIMEOUT;
}

esp_err_t wifi_set_credentials(
    const uint8_t *ssid,
    size_t ssid_length,
    const uint8_t *password,
    size_t password_length
)
{
    if (!s_started) {
        return ESP_ERR_INVALID_STATE;
    }
    if (ssid == NULL || ssid_length == 0 || ssid_length > sizeof(((wifi_config_t *)0)->sta.ssid)) {
        return ESP_ERR_INVALID_ARG;
    }
    if (password_length >= sizeof(((wifi_config_t *)0)->sta.password)) {
        return ESP_ERR_INVALID_ARG;
    }
    if (password_length > 0 && password == NULL) {
        return ESP_ERR_INVALID_ARG;
    }

    wifi_config_t config = {0};
    memcpy(config.sta.ssid, ssid, ssid_length);
    if (password_length > 0) {
        memcpy(config.sta.password, password, password_length);
        config.sta.threshold.authmode = WIFI_AUTH_WPA2_PSK;
    } else {
        config.sta.threshold.authmode = WIFI_AUTH_OPEN;
    }
    config.sta.pmf_cfg.capable = true;
    config.sta.pmf_cfg.required = false;

    s_has_credentials = false;
    xEventGroupClearBits(s_wifi_event_group, WIFI_CONNECTED_BIT);
    esp_wifi_disconnect();
    esp_err_t ret = esp_wifi_set_config(WIFI_IF_STA, &config);
    if (ret != ESP_OK) {
        return ret;
    }
    s_has_credentials = true;
    ESP_LOGI(TAG, "saved USB Wi-Fi configuration, reconnecting");
    return esp_wifi_connect();
}

bool wifi_is_connected(void)
{
    if (s_wifi_event_group == NULL) {
        return false;
    }
    return (xEventGroupGetBits(s_wifi_event_group) & WIFI_CONNECTED_BIT) != 0;
}

esp_err_t wifi_get_current_ssid(uint8_t *ssid, size_t capacity, size_t *ssid_length)
{
    if (!s_started || ssid == NULL || capacity == 0 || ssid_length == NULL) {
        return ESP_ERR_INVALID_ARG;
    }
    wifi_config_t config = {0};
    esp_err_t ret = esp_wifi_get_config(WIFI_IF_STA, &config);
    if (ret != ESP_OK) {
        return ret;
    }
    size_t length = strnlen((const char *)config.sta.ssid, sizeof(config.sta.ssid));
    if (length > capacity) {
        return ESP_ERR_INVALID_SIZE;
    }
    memcpy(ssid, config.sta.ssid, length);
    *ssid_length = length;
    return ESP_OK;
}

esp_err_t wifi_get_ip(char *buffer, size_t capacity)
{
    if (!wifi_is_connected() || s_station_netif == NULL || buffer == NULL || capacity == 0) {
        return ESP_ERR_INVALID_STATE;
    }
    esp_netif_ip_info_t ip_info = {0};
    esp_err_t ret = esp_netif_get_ip_info(s_station_netif, &ip_info);
    if (ret != ESP_OK) {
        return ret;
    }
    int written = snprintf(buffer, capacity, IPSTR, IP2STR(&ip_info.ip));
    return written > 0 && (size_t)written < capacity ? ESP_OK : ESP_ERR_INVALID_SIZE;
}

esp_err_t wifi_connect(void)
{
    esp_err_t ret = wifi_connect_start();
    if (ret != ESP_OK) {
        return ret;
    }
    return wifi_wait_connected(portMAX_DELAY);
}
