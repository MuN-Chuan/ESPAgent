idf_component_register(SRCS "app_main.c" "wifi_connect.c" "usb_wifi_provisioning.c" "display_gpio_config.c" "udp_display.c" "lcd_display.c" "esp_log_compat.c"
                       INCLUDE_DIRS "."
                       REQUIRES esp_driver_gpio esp_driver_spi esp_driver_uart esp_driver_usb_serial_jtag esp_lcd esp_timer esp_wifi esp_netif esp_event nvs_flash lwip esp_psram espressif__esp_new_jpeg)
