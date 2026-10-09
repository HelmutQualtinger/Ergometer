# The C version of ergometer.py. macOS only: Bluetooth goes through CoreBluetooth (ble_macos.m).
CC ?= cc
CFLAGS ?= -O2 -Wall -Wextra

ergometer: ergometer.c ble_macos.m ble.h
	$(CC) $(CFLAGS) -std=gnu11 -fobjc-arc ergometer.c ble_macos.m -o $@ -framework CoreBluetooth -framework Foundation -lm

clean:
	rm -f ergometer

.PHONY: clean
