/* The Bluetooth LE calls ergometer.c needs. They block until the answer is there and may be
 * called from any thread except from inside a callback. One connection at a time.
 * UUIDs are the full lower-case form, e.g. "00002ad2-0000-1000-8000-00805f9b34fb". */
#ifndef BLE_H
#define BLE_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

enum { BLE_READ = 1, BLE_WRITE = 2, BLE_NOTIFY = 4, BLE_INDICATE = 8, BLE_WRITE_NO_RESPONSE = 16 };

typedef struct {
    char address[40]; /* a UUID on macOS */
    char name[64];    /* advertised name, or "" */
    int rssi;         /* dBm */
    bool ftms;        /* advertises the Fitness Machine service */
} ble_device;

typedef struct {
    char service[40];
    char uuid[40];
    unsigned properties; /* BLE_READ | ... */
} ble_characteristic;

typedef void (*ble_notify_fn)(const char *uuid, const uint8_t *data, size_t length);
typedef void (*ble_disconnect_fn)(void);

/* Scan for `timeout` seconds; the devices found, strongest signal first. -1 if Bluetooth is not usable. */
int ble_scan(double timeout, ble_device *devices, int max);
/* Connect to a device from the last scan and discover its services. */
bool ble_connect(const char *address, ble_disconnect_fn on_disconnect, char *error, size_t error_size);
int ble_characteristics(ble_characteristic *characteristics, int max);
/* Notifications and indications of all subscribed characteristics go to the same function. */
bool ble_subscribe(const char *uuid, ble_notify_fn on_data);
int ble_read(const char *uuid, uint8_t *data, size_t max); /* bytes read, or -1 */
bool ble_write(const char *uuid, const uint8_t *data, size_t length); /* with response */
void ble_disconnect(void);

#endif
