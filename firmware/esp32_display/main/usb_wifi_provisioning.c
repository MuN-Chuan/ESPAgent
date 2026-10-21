#include <ctype.h>
#include <stdio.h>
#include <string.h>

#include "driver/usb_serial_jtag.h"
#include "driver/usb_serial_jtag_vfs.h"
#include "driver/uart.h"
#include "display_gpio_config.h"
#include "esp_err.h"
#include "esp_log.h"
#include "esp_system.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "usb_wifi_provisioning.h"
#include "wifi_connect.h"

#define PROVISION_UART UART_NUM_0
#define PROVISION_BAUD_RATE 115200
#define PROVISION_RX_BUFFER_SIZE 1024
#define PROVISION_USB_BUFFER_SIZE 1024
#define PROVISION_LINE_SIZE 256
#define PROVISION_TASK_STACK_SIZE 4096
#define PROVISION_CONNECT_TIMEOUT pdMS_TO_TICKS(20000)
#define PROVISION_POLL_DELAY pdMS_TO_TICKS(10)

static const char *TAG = "usb_wifi";
static bool s_started;
static bool s_usb_serial_jtag_ready;

static void send_response(const char *response)
{
    uart_write_bytes(PROVISION_UART, response, strlen(response));
    uart_write_bytes(PROVISION_UART, "\r\n", 2);
    if (s_usb_serial_jtag_ready) {
        usb_serial_jtag_write_bytes(response, strlen(response), pdMS_TO_TICKS(50));
        usb_serial_jtag_write_bytes("\r\n", 2, pdMS_TO_TICKS(50));
    }
}

static int hex_value(char value)
{
    if (value >= '0' && value <= '9') {
        return value - '0';
    }
    value = (char)tolower((unsigned char)value);
    if (value >= 'a' && value <= 'f') {
        return value - 'a' + 10;
    }
    return -1;
}

static bool decode_hex(const char *encoded, uint8_t *output, size_t capacity, size_t *output_length)
{
    if (strcmp(encoded, "-") == 0) {
        *output_length = 0;
        return true;
    }
    size_t length = strlen(encoded);
    if ((length & 1U) != 0 || length / 2 > capacity) {
        return false;
    }
    for (size_t index = 0; index < length; index += 2) {
        int high = hex_value(encoded[index]);
        int low = hex_value(encoded[index + 1]);
        if (high < 0 || low < 0) {
            return false;
        }
        output[index / 2] = (uint8_t)((high << 4) | low);
    }
    *output_length = length / 2;
    return true;
}

static void encode_hex(const uint8_t *input, size_t length, char *output, size_t capacity)
{
    static const char digits[] = "0123456789abcdef";
    if (length == 0 || capacity < length * 2 + 1) {
        snprintf(output, capacity, "-");
        return;
    }
    for (size_t index = 0; index < length; ++index) {
        output[index * 2] = digits[input[index] >> 4];
        output[index * 2 + 1] = digits[input[index] & 0x0f];
    }
    output[length * 2] = '\0';
}

static void send_ready(void)
{
    uint8_t ssid[32] = {0};
    size_t ssid_length = 0;
    char ssid_hex[65] = "-";
    char ip[16] = "-";
    if (wifi_get_current_ssid(ssid, sizeof(ssid), &ssid_length) == ESP_OK) {
        encode_hex(ssid, ssid_length, ssid_hex, sizeof(ssid_hex));
    }
    if (wifi_get_ip(ip, sizeof(ip)) != ESP_OK) {
        snprintf(ip, sizeof(ip), "-");
    }
    char response[128];
    snprintf(
        response,
        sizeof(response),
        "ESPAGENT_WIFI READY 1 %u %s %s",
        wifi_is_connected() ? 1U : 0U,
        ssid_hex,
        ip
    );
    send_response(response);
}

static void apply_credentials(char *ssid_hex, char *password_hex)
{
    uint8_t ssid[32] = {0};
    uint8_t password[64] = {0};
    size_t ssid_length = 0;
    size_t password_length = 0;
    if (!decode_hex(ssid_hex, ssid, sizeof(ssid), &ssid_length) || ssid_length == 0) {
        send_response("ESPAGENT_WIFI ERR INVALID_SSID");
        return;
    }
    if (!decode_hex(password_hex, password, sizeof(password) - 1, &password_length)) {
        send_response("ESPAGENT_WIFI ERR INVALID_PASSWORD");
        return;
    }

    esp_err_t ret = wifi_set_credentials(ssid, ssid_length, password, password_length);
    if (ret != ESP_OK) {
        char response[80];
        snprintf(response, sizeof(response), "ESPAGENT_WIFI ERR APPLY_%s", esp_err_to_name(ret));
        send_response(response);
        return;
    }
    send_response("ESPAGENT_WIFI ACK APPLYING");

    ret = wifi_wait_connected(PROVISION_CONNECT_TIMEOUT);
    if (ret != ESP_OK) {
        send_response("ESPAGENT_WIFI ERR CONNECT_TIMEOUT");
        return;
    }
    char ip[16] = "-";
    if (wifi_get_ip(ip, sizeof(ip)) != ESP_OK) {
        send_response("ESPAGENT_WIFI ERR IP_UNAVAILABLE");
        return;
    }
    char response[64];
    snprintf(response, sizeof(response), "ESPAGENT_WIFI OK %s", ip);
    send_response(response);
}

static void send_display_gpio_ready(void)
{
    display_gpio_config_t config;
    esp_err_t ret = display_gpio_config_load(&config);
    if (ret != ESP_OK) {
        char response[96];
        snprintf(response, sizeof(response), "ESPAGENT_DISPLAY_GPIO ERR LOAD_%s", esp_err_to_name(ret));
        send_response(response);
        return;
    }
    char response[128];
    snprintf(
        response,
        sizeof(response),
        "ESPAGENT_DISPLAY_GPIO READY 1 %d %d %d %d %d %d",
        config.scl,
        config.sda,
        config.cs,
        config.dc,
        config.rst,
        config.bl
    );
    send_response(response);
}

static void apply_display_gpio(const char *arguments)
{
    int scl;
    int sda;
    int cs;
    int dc;
    int rst;
    int bl;
    char trailing;
    if (sscanf(arguments, "%d %d %d %d %d %d %c", &scl, &sda, &cs, &dc, &rst, &bl, &trailing) != 6) {
        send_response("ESPAGENT_DISPLAY_GPIO ERR INVALID_COMMAND");
        return;
    }
    display_gpio_config_t config = {
        .scl = (gpio_num_t)scl,
        .sda = (gpio_num_t)sda,
        .cs = (gpio_num_t)cs,
        .dc = (gpio_num_t)dc,
        .rst = (gpio_num_t)rst,
        .bl = (gpio_num_t)bl,
    };
    if (!display_gpio_config_is_valid(&config)) {
        send_response("ESPAGENT_DISPLAY_GPIO ERR INVALID_GPIO");
        return;
    }
    esp_err_t ret = display_gpio_config_save(&config);
    if (ret != ESP_OK) {
        char response[96];
        snprintf(response, sizeof(response), "ESPAGENT_DISPLAY_GPIO ERR SAVE_%s", esp_err_to_name(ret));
        send_response(response);
        return;
    }
    send_response("ESPAGENT_DISPLAY_GPIO OK RESTARTING");
    vTaskDelay(pdMS_TO_TICKS(250));
    esp_restart();
}

static void handle_line(char *line)
{
    while (*line == ' ' || *line == '\t') {
        ++line;
    }
    if (strcmp(line, "ESPAGENT_WIFI_HELLO 1") == 0 || strcmp(line, "ESPAGENT_WIFI_STATUS 1") == 0) {
        send_ready();
        return;
    }

    if (strcmp(line, "ESPAGENT_DISPLAY_GPIO_GET 1") == 0) {
        send_display_gpio_ready();
        return;
    }

    const char display_prefix[] = "ESPAGENT_DISPLAY_GPIO_SET 1 ";
    if (strncmp(line, display_prefix, sizeof(display_prefix) - 1) == 0) {
        apply_display_gpio(line + sizeof(display_prefix) - 1);
        return;
    }

    const char prefix[] = "ESPAGENT_WIFI_SET 1 ";
    if (strncmp(line, prefix, sizeof(prefix) - 1) != 0) {
        return;
    }
    char *ssid_hex = line + sizeof(prefix) - 1;
    char *separator = strchr(ssid_hex, ' ');
    if (separator == NULL) {
        send_response("ESPAGENT_WIFI ERR INVALID_COMMAND");
        return;
    }
    *separator = '\0';
    char *password_hex = separator + 1;
    if (*password_hex == '\0' || strchr(password_hex, ' ') != NULL) {
        send_response("ESPAGENT_WIFI ERR INVALID_COMMAND");
        return;
    }
    apply_credentials(ssid_hex, password_hex);
}

static void consume_byte(uint8_t byte, char *line, size_t *line_length)
{
    if (byte == '\r' || byte == '\n') {
        if (*line_length > 0) {
            line[*line_length] = '\0';
            handle_line(line);
            *line_length = 0;
        }
        return;
    }
    if (*line_length + 1 < PROVISION_LINE_SIZE) {
        line[(*line_length)++] = (char)byte;
    } else {
        *line_length = 0;
    }
}

static void provisioning_task(void *arg)
{
    char uart_line[PROVISION_LINE_SIZE];
    char usb_line[PROVISION_LINE_SIZE];
    size_t uart_line_length = 0;
    size_t usb_line_length = 0;
    uint8_t byte;
    while (true) {
        bool received_any = false;
        if (uart_read_bytes(PROVISION_UART, &byte, 1, 0) > 0) {
            consume_byte(byte, uart_line, &uart_line_length);
            received_any = true;
        }
        if (s_usb_serial_jtag_ready) {
            uint8_t usb_bytes[64];
            const int received = usb_serial_jtag_read_bytes(
                usb_bytes,
                sizeof(usb_bytes),
                0
            );
            for (int index = 0; index < received; ++index) {
                consume_byte(usb_bytes[index], usb_line, &usb_line_length);
                received_any = true;
            }
        }
        if (!received_any) {
            vTaskDelay(PROVISION_POLL_DELAY);
        }
    }
}

esp_err_t usb_wifi_provisioning_start(void)
{
    if (s_started) {
        return ESP_OK;
    }
    if (!uart_is_driver_installed(PROVISION_UART)) {
        uart_config_t config = {
            .baud_rate = PROVISION_BAUD_RATE,
            .data_bits = UART_DATA_8_BITS,
            .parity = UART_PARITY_DISABLE,
            .stop_bits = UART_STOP_BITS_1,
            .flow_ctrl = UART_HW_FLOWCTRL_DISABLE,
            .source_clk = UART_SCLK_DEFAULT,
        };
        ESP_ERROR_CHECK(uart_param_config(PROVISION_UART, &config));
        ESP_ERROR_CHECK(uart_set_pin(
            PROVISION_UART,
            UART_PIN_NO_CHANGE,
            UART_PIN_NO_CHANGE,
            UART_PIN_NO_CHANGE,
            UART_PIN_NO_CHANGE
        ));
        ESP_ERROR_CHECK(uart_driver_install(PROVISION_UART, PROVISION_RX_BUFFER_SIZE, 0, 0, NULL, 0));
    }
    if (!usb_serial_jtag_is_driver_installed()) {
        usb_serial_jtag_driver_config_t usb_config = {
            .tx_buffer_size = PROVISION_USB_BUFFER_SIZE,
            .rx_buffer_size = PROVISION_USB_BUFFER_SIZE,
        };
        esp_err_t usb_ret = usb_serial_jtag_driver_install(&usb_config);
        if (usb_ret == ESP_OK) {
            usb_serial_jtag_vfs_use_driver();
            s_usb_serial_jtag_ready = true;
        } else {
            ESP_LOGW(
                TAG,
                "USB Serial/JTAG provisioning unavailable: %s",
                esp_err_to_name(usb_ret)
            );
        }
    } else {
        usb_serial_jtag_vfs_use_driver();
        s_usb_serial_jtag_ready = true;
    }
    BaseType_t created = xTaskCreate(
        provisioning_task,
        "usb_wifi",
        PROVISION_TASK_STACK_SIZE,
        NULL,
        5,
        NULL
    );
    if (created != pdPASS) {
        return ESP_ERR_NO_MEM;
    }
    s_started = true;
    ESP_LOGI(
        TAG,
        "Wi-Fi provisioning ready on UART0 at %d baud%s",
        PROVISION_BAUD_RATE,
        s_usb_serial_jtag_ready ? " and USB Serial/JTAG" : ""
    );
    return ESP_OK;
}
