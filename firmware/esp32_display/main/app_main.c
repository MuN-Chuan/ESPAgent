#include "esp_log.h"
#include "esp_err.h"
#include "nvs_flash.h"
#include "lcd_display.h"
#include "protocol.h"
#include "udp_display.h"
#include "usb_wifi_provisioning.h"
#include "wifi_connect.h"

static const char *TAG = "espagent";
extern void esp_log(uint32_t config, const char *tag, const char *format, ...);

void app_main(void)
{
    esp_log(0, TAG, "");
    ESP_LOGI(TAG, "ESPAgent display firmware boot");
    ESP_LOGI(TAG, "protocol magic=0x%04x header=%u", ESPAGENT_MAGIC_DISPLAY, (unsigned)sizeof(tile_packet_header_t));

    esp_err_t nvs_ret = nvs_flash_init();
    if (nvs_ret == ESP_ERR_NVS_NO_FREE_PAGES || nvs_ret == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        ESP_ERROR_CHECK(nvs_flash_erase());
        nvs_ret = nvs_flash_init();
    }
    ESP_ERROR_CHECK(nvs_ret);

    ESP_ERROR_CHECK(lcd_display_init());
    ESP_ERROR_CHECK(lcd_display_draw_boot_pattern());

    esp_err_t ret = wifi_connect_start();
    if (ret != ESP_OK) {
        ESP_LOGE(TAG, "Wi-Fi initialization failed: %s", esp_err_to_name(ret));
        return;
    }
    ESP_ERROR_CHECK(usb_wifi_provisioning_start());

    ret = wifi_wait_connected(portMAX_DELAY);
    if (ret != ESP_OK) {
        ESP_LOGE(TAG, "Wi-Fi connection failed: %s", esp_err_to_name(ret));
        return;
    }

    ESP_ERROR_CHECK(udp_display_start());
}
