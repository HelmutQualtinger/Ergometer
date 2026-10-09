/* ble.h on macOS. CoreBluetooth has no C interface, so this one file is Objective-C.
 * The central manager lives on its own dispatch queue; the calls of ble.h wait on a
 * semaphore for the delegate method that answers them. */
#import <CoreBluetooth/CoreBluetooth.h>
#include <pthread.h>
#include "ble.h"

static const int64_t OP_TIMEOUT = 10; /* s, for a single read, write or subscription */
static const int64_t CONNECT_TIMEOUT = 20;

@interface Central : NSObject <CBCentralManagerDelegate, CBPeripheralDelegate>
@property CBCentralManager *manager;
@property dispatch_queue_t queue;
@property dispatch_semaphore_t ready; /* the manager's state is known */
@property dispatch_semaphore_t done;  /* the pending operation was answered */
@property NSMutableDictionary<NSString *, NSMutableDictionary *> *found; /* address -> peripheral, name, rssi, ftms */
@property CBPeripheral *peripheral;
@property NSMutableArray<CBCharacteristic *> *characteristics;
@property NSInteger servicesPending;
@property NSString *reading; /* UUID of the characteristic a read waits for */
@property NSData *value;
@property NSError *error;
@end

static Central *central;
static pthread_mutex_t op_lock = PTHREAD_MUTEX_INITIALIZER; /* one operation at a time */
static ble_notify_fn notify;
static ble_disconnect_fn disconnected;

static NSString *full_uuid(CBUUID *uuid) {
    NSString *text = uuid.UUIDString.lowercaseString;
    if (text.length == 4) return [NSString stringWithFormat:@"0000%@-0000-1000-8000-00805f9b34fb", text];
    if (text.length == 8) return [NSString stringWithFormat:@"%@-0000-1000-8000-00805f9b34fb", text];
    return text;
}

@implementation Central

- (void)centralManagerDidUpdateState:(CBCentralManager *)manager {
    if (manager.state != CBManagerStateUnknown && manager.state != CBManagerStateResetting)
        dispatch_semaphore_signal(self.ready);
}

- (void)centralManager:(CBCentralManager *)manager didDiscoverPeripheral:(CBPeripheral *)peripheral
     advertisementData:(NSDictionary<NSString *, id> *)advertisement RSSI:(NSNumber *)rssi {
    NSString *address = peripheral.identifier.UUIDString;
    NSMutableDictionary *entry = self.found[address];
    if (!entry) entry = self.found[address] = [NSMutableDictionary dictionaryWithObject:peripheral forKey:@"peripheral"];
    entry[@"rssi"] = rssi;
    NSString *name = advertisement[CBAdvertisementDataLocalNameKey] ?: peripheral.name;
    if (name) entry[@"name"] = name;
    for (CBUUID *service in advertisement[CBAdvertisementDataServiceUUIDsKey])
        if ([service isEqual:[CBUUID UUIDWithString:@"1826"]]) entry[@"ftms"] = @YES;
}

- (void)centralManager:(CBCentralManager *)manager didConnectPeripheral:(CBPeripheral *)peripheral {
    peripheral.delegate = self;
    [peripheral discoverServices:nil];
}

- (void)centralManager:(CBCentralManager *)manager didFailToConnectPeripheral:(CBPeripheral *)peripheral error:(NSError *)error {
    self.error = error ?: [NSError errorWithDomain:@"ble" code:1 userInfo:@{NSLocalizedDescriptionKey: @"could not connect"}];
    dispatch_semaphore_signal(self.done);
}

- (void)centralManager:(CBCentralManager *)manager didDisconnectPeripheral:(CBPeripheral *)peripheral error:(NSError *)error {
    if (peripheral == self.peripheral && disconnected) disconnected();
}

- (void)peripheral:(CBPeripheral *)peripheral didDiscoverServices:(NSError *)error {
    self.servicesPending = peripheral.services.count;
    if (error || !self.servicesPending) {
        self.error = error;
        dispatch_semaphore_signal(self.done);
        return;
    }
    for (CBService *service in peripheral.services) [peripheral discoverCharacteristics:nil forService:service];
}

- (void)peripheral:(CBPeripheral *)peripheral didDiscoverCharacteristicsForService:(CBService *)service error:(NSError *)error {
    [self.characteristics addObjectsFromArray:service.characteristics ?: @[]];
    if (--self.servicesPending == 0) dispatch_semaphore_signal(self.done);
}

- (void)peripheral:(CBPeripheral *)peripheral didUpdateValueForCharacteristic:(CBCharacteristic *)characteristic error:(NSError *)error {
    NSString *uuid = full_uuid(characteristic.UUID);
    if ([uuid isEqualToString:self.reading]) {
        self.reading = nil;
        self.value = characteristic.value;
        self.error = error;
        dispatch_semaphore_signal(self.done);
    } else if (!error && notify && characteristic.value) {
        notify(uuid.UTF8String, characteristic.value.bytes, characteristic.value.length);
    }
}

- (void)peripheral:(CBPeripheral *)peripheral didWriteValueForCharacteristic:(CBCharacteristic *)characteristic error:(NSError *)error {
    self.error = error;
    dispatch_semaphore_signal(self.done);
}

- (void)peripheral:(CBPeripheral *)peripheral didUpdateNotificationStateForCharacteristic:(CBCharacteristic *)characteristic error:(NSError *)error {
    self.error = error;
    dispatch_semaphore_signal(self.done);
}

@end

static bool wait_for(dispatch_semaphore_t semaphore, int64_t seconds) {
    return dispatch_semaphore_wait(semaphore, dispatch_time(DISPATCH_TIME_NOW, seconds * NSEC_PER_SEC)) == 0;
}

/* The manager, once Bluetooth is switched on and this program may use it. */
static bool start(void) {
    static dispatch_once_t once;
    dispatch_once(&once, ^{
        central = [Central new];
        central.queue = dispatch_queue_create("ergometer.ble", DISPATCH_QUEUE_SERIAL);
        central.ready = dispatch_semaphore_create(0);
        central.done = dispatch_semaphore_create(0);
        central.found = [NSMutableDictionary dictionary];
        central.characteristics = [NSMutableArray array];
        central.manager = [[CBCentralManager alloc] initWithDelegate:central queue:central.queue];
        wait_for(central.ready, 5);
    });
    if (central.manager.state == CBManagerStatePoweredOn) return true;
    const char *reason = central.manager.state == CBManagerStateUnauthorized ? "this program is not allowed to use it "
                         "(System Settings > Privacy & Security > Bluetooth)"
                         : central.manager.state == CBManagerStatePoweredOff ? "it is switched off" : "it is not available";
    fprintf(stderr, "Bluetooth cannot be used: %s\n", reason);
    return false;
}

/* Run a request on the manager's queue and wait for the delegate to answer it. */
static bool operation(int64_t timeout, void (^request)(void)) {
    pthread_mutex_lock(&op_lock);
    while (dispatch_semaphore_wait(central.done, DISPATCH_TIME_NOW) == 0) {} /* a late answer to an operation that timed out */
    dispatch_sync(central.queue, ^{
        central.error = nil;
        request();
    });
    bool ok = wait_for(central.done, timeout);
    if (!ok) dispatch_sync(central.queue, ^{ central.reading = nil; });
    __block bool failed;
    dispatch_sync(central.queue, ^{ failed = central.error != nil; });
    pthread_mutex_unlock(&op_lock);
    return ok && !failed;
}

static CBCharacteristic *find(const char *uuid) {
    __block CBCharacteristic *result = nil;
    dispatch_sync(central.queue, ^{
        for (CBCharacteristic *characteristic in central.characteristics)
            if ([full_uuid(characteristic.UUID) isEqualToString:@(uuid)]) {
                result = characteristic;
                break;
            }
    });
    return result;
}

int ble_scan(double timeout, ble_device *devices, int max) {
    @autoreleasepool {
        if (!start()) return -1;
        dispatch_sync(central.queue, ^{
            [central.found removeAllObjects];
            [central.manager scanForPeripheralsWithServices:nil options:nil];
        });
        [NSThread sleepForTimeInterval:timeout];
        __block int count = 0;
        dispatch_sync(central.queue, ^{
            [central.manager stopScan];
            NSArray *addresses = [central.found keysSortedByValueUsingComparator:^(NSDictionary *a, NSDictionary *b) {
                return [b[@"rssi"] compare:a[@"rssi"]];
            }];
            for (NSString *address in addresses) {
                if (count == max) break;
                NSDictionary *entry = central.found[address];
                ble_device *device = &devices[count++];
                memset(device, 0, sizeof *device);
                strlcpy(device->address, address.UTF8String, sizeof device->address);
                strlcpy(device->name, [entry[@"name"] UTF8String] ?: "", sizeof device->name);
                device->rssi = [entry[@"rssi"] intValue];
                device->ftms = [entry[@"ftms"] boolValue];
            }
        });
        return count;
    }
}

bool ble_connect(const char *address, ble_disconnect_fn on_disconnect, char *error, size_t error_size) {
    @autoreleasepool {
        if (!start()) {
            snprintf(error, error_size, "Bluetooth is not usable");
            return false;
        }
        __block CBPeripheral *peripheral = nil;
        dispatch_sync(central.queue, ^{
            for (NSString *known in central.found)
                if ([known caseInsensitiveCompare:@(address)] == NSOrderedSame) peripheral = central.found[known][@"peripheral"];
        });
        if (!peripheral) {
            snprintf(error, error_size, "no device with this address was seen in the scan");
            return false;
        }
        /* Answered by the end of the service discovery, or by a failure to connect. */
        bool ok = operation(CONNECT_TIMEOUT, ^{
            [central.characteristics removeAllObjects];
            central.peripheral = peripheral;
            [central.manager connectPeripheral:peripheral options:nil];
        });
        if (!ok) {
            __block NSString *reason;
            dispatch_sync(central.queue, ^{
                reason = central.error.localizedDescription ?: @"timed out";
                central.peripheral = nil; /* so the cancelled connection is not reported as a disconnect */
                [central.manager cancelPeripheralConnection:peripheral];
            });
            snprintf(error, error_size, "%s", reason.UTF8String);
            return false;
        }
        disconnected = on_disconnect;
        return true;
    }
}

int ble_characteristics(ble_characteristic *characteristics, int max) {
    @autoreleasepool {
        __block int count = 0;
        dispatch_sync(central.queue, ^{
            for (CBCharacteristic *found in central.characteristics) {
                if (count == max) break;
                ble_characteristic *out = &characteristics[count++];
                CBCharacteristicProperties p = found.properties;
                strlcpy(out->service, full_uuid(found.service.UUID).UTF8String, sizeof out->service);
                strlcpy(out->uuid, full_uuid(found.UUID).UTF8String, sizeof out->uuid);
                out->properties = (p & CBCharacteristicPropertyRead ? BLE_READ : 0) | (p & CBCharacteristicPropertyWrite ? BLE_WRITE : 0) |
                                  (p & CBCharacteristicPropertyNotify ? BLE_NOTIFY : 0) | (p & CBCharacteristicPropertyIndicate ? BLE_INDICATE : 0) |
                                  (p & CBCharacteristicPropertyWriteWithoutResponse ? BLE_WRITE_NO_RESPONSE : 0);
            }
        });
        return count;
    }
}

bool ble_subscribe(const char *uuid, ble_notify_fn on_data) {
    @autoreleasepool {
        CBCharacteristic *characteristic = find(uuid);
        if (!characteristic) return false;
        notify = on_data;
        return operation(OP_TIMEOUT, ^{ [central.peripheral setNotifyValue:YES forCharacteristic:characteristic]; });
    }
}

int ble_read(const char *uuid, uint8_t *data, size_t max) {
    @autoreleasepool {
        CBCharacteristic *characteristic = find(uuid);
        if (!characteristic) return -1;
        bool ok = operation(OP_TIMEOUT, ^{
            central.reading = @(uuid);
            [central.peripheral readValueForCharacteristic:characteristic];
        });
        if (!ok) return -1;
        __block NSData *value;
        dispatch_sync(central.queue, ^{ value = central.value; });
        size_t length = MIN(value.length, max);
        memcpy(data, value.bytes, length);
        return (int)length;
    }
}

bool ble_write(const char *uuid, const uint8_t *data, size_t length) {
    @autoreleasepool {
        CBCharacteristic *characteristic = find(uuid);
        if (!characteristic) return false;
        NSData *value = [NSData dataWithBytes:data length:length];
        return operation(OP_TIMEOUT, ^{
            [central.peripheral writeValue:value forCharacteristic:characteristic type:CBCharacteristicWriteWithResponse];
        });
    }
}

void ble_disconnect(void) {
    @autoreleasepool {
        if (!central) return;
        dispatch_sync(central.queue, ^{
            CBPeripheral *peripheral = central.peripheral;
            central.peripheral = nil; /* our own doing: no callback */
            if (peripheral) [central.manager cancelPeripheralConnection:peripheral];
        });
    }
}
