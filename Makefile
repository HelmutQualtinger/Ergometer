# The C version of ergometer.py. macOS only: Bluetooth goes through CoreBluetooth (ble_macos.m).
# HTTPS for the phone needs OpenSSL 3 (brew install openssl@3).
CC ?= cc
CFLAGS ?= -O2 -Wall -Wextra
OPENSSL ?= $(shell brew --prefix openssl@3 2>/dev/null)

ergometer: ergometer.c ble_macos.m ble.h
	$(CC) $(CFLAGS) -std=gnu11 -fobjc-arc -I$(OPENSSL)/include ergometer.c ble_macos.m -o $@ \
		-framework CoreBluetooth -framework Foundation -L$(OPENSSL)/lib -lssl -lcrypto -lm

clean:
	rm -f ergometer

.PHONY: clean
