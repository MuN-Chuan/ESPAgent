#include <stdarg.h>
#include <stdint.h>

#include "esp_log.h"

void esp_log(uint32_t config, const char *tag, const char *format, ...)
{
    (void)config;
    va_list args;
    va_start(args, format);
    esp_log_writev(ESP_LOG_INFO, tag, format, args);
    va_end(args);
}
