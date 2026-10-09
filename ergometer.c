/* Connect to a Christopeit AX 4000 ergometer over Bluetooth LE and print its live values.
 *
 * The C version of ergometer.py: same options, same panel (panel.html), same logs.
 *
 *     make
 *     ./ergometer                 # find the ergometer automatically
 *     ./ergometer --scan          # just list nearby BLE devices
 *     ./ergometer --panel         # also show a live Plotly panel in the browser
 *     ./ergometer --power 150     # hold 150 W by adjusting the resistance (PID)
 *     ./ergometer --panel --profile vorgabe-hit.csv   # follow a power profile from a file
 *     ./ergometer --raw           # also dump the vendor-specific raw data
 *     ./ergometer --name FS-1837  # match on a different name fragment
 *     ./ergometer --address <id>  # connect to a specific device
 *
 * Bluetooth itself is behind ble.h (ble_macos.m on macOS); everything else is here.
 */
#define _DARWIN_C_SOURCE
#define _GNU_SOURCE

#include <arpa/inet.h>
#include <ctype.h>
#include <dirent.h>
#include <errno.h>
#include <fcntl.h>
#include <getopt.h>
#include <math.h>
#include <netdb.h>
#include <netinet/in.h>
#include <poll.h>
#include <pthread.h>
#include <signal.h>
#include <spawn.h>
#include <stdarg.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <strings.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/time.h>
#include <time.h>
#include <unistd.h>
#ifdef __APPLE__
#include <mach-o/dyld.h>
#endif

#include "ble.h"

#define FTMS_SERVICE "00001826-0000-1000-8000-00805f9b34fb"
#define INDOOR_BIKE_DATA "00002ad2-0000-1000-8000-00805f9b34fb"
#define HEART_RATE "00002a37-0000-1000-8000-00805f9b34fb"
#define CONTROL_POINT "00002ad9-0000-1000-8000-00805f9b34fb"
#define RESISTANCE_RANGE "00002ad6-0000-1000-8000-00805f9b34fb"
#define POWER_RANGE "00002ad8-0000-1000-8000-00805f9b34fb"

/* PID gains for the power controller: output is the resistance level, error is in watts.
 * One level is worth roughly 15-20 W at 70 rpm. The gains are deliberately gentle,
 * because a level change takes a second or two to show up in the measured power. */
#define PID_KP 0.01       /* levels per W */
#define PID_KI 0.008      /* levels per W*s */
#define PID_KD 0.005      /* levels per W/s */
#define PID_DEADBAND 0.6  /* only move when the output is this far from the current level */
#define PID_TOLERANCE 8   /* W; smaller errors count as zero, so it doesn't hunt between two levels */
#define PID_SETTLE 3      /* s; after a jump to a new level the controller waits for the measured power to follow */
#define PID_MAX_TRIM 3    /* levels; how far the controller's correction to the table is carried over to a new target */
#define MIN_CADENCE 20    /* rpm; below this the rider has stopped and the controller holds */
#define MAX_TARGET 300    /* W; as far as the slider for the target power goes */

/* The room temperature comes from a sensor that publishes JSON telemetry over MQTT. Where the
 * broker is stays out of the repository: MQTT_HOST, MQTT_PORT and MQTT_TOPIC come from the
 * environment or from a file .env next to the program (KEY=value per line, see .env.example).
 * Without MQTT_HOST and MQTT_PORT there is no temperature. */
#define MQTT_FIELD "roomtemp"
#define MQTT_OFFSET -2.0 /* °C; the sensor reads this much too warm */
static char MQTT_HOST[256], MQTT_PORT[16], MQTT_TOPIC[256];

static double clamp(double value, double low, double high) { return value < low ? low : value > high ? high : value; }

static double now_ms(void) { /* epoch ms */
    struct timespec now;
    clock_gettime(CLOCK_REALTIME, &now);
    return now.tv_sec * 1000.0 + now.tv_nsec / 1e6;
}

static double monotonic(void) {
    struct timespec now;
    clock_gettime(CLOCK_MONOTONIC, &now);
    return now.tv_sec + now.tv_nsec / 1e9;
}

static void sleep_for(double seconds) {
    struct timespec time = {(time_t)seconds, (long)((seconds - (time_t)seconds) * 1e9)};
    while (nanosleep(&time, &time) == -1 && errno == EINTR) {}
}

static const char *stamp(void) {
    static _Thread_local char text[16];
    time_t now = time(NULL);
    struct tm local;
    strftime(text, sizeof text, "%H:%M:%S", localtime_r(&now, &local));
    return text;
}

/* A string that grows; used for the JSON replies. */
struct buf {
    char *text;
    size_t length, capacity;
};

static void buf_add(struct buf *buf, const char *text, size_t length) {
    if (buf->length + length + 1 > buf->capacity) {
        buf->capacity = (buf->length + length + 1) * 2;
        buf->text = realloc(buf->text, buf->capacity);
        if (!buf->text) abort();
    }
    memcpy(buf->text + buf->length, text, length);
    buf->length += length;
    buf->text[buf->length] = 0;
}

static void buf_printf(struct buf *buf, const char *format, ...) {
    char text[512];
    va_list args;
    va_start(args, format);
    int length = vsnprintf(text, sizeof text, format, args);
    va_end(args);
    buf_add(buf, text, length < (int)sizeof text ? (size_t)length : sizeof text - 1);
}

static void buf_json_string(struct buf *buf, const char *text) {
    buf_add(buf, "\"", 1);
    for (; *text; text++) {
        if (*text == '"' || *text == '\\') buf_printf(buf, "\\%c", *text);
        else if ((unsigned char)*text < 0x20) buf_printf(buf, "\\u%04x", *text);
        else buf_add(buf, text, 1);
    }
    buf_add(buf, "\"", 1);
}

/* The value of a key in a flat JSON object, as far as the panel and the sensor send one:
 * a pointer to where the value starts, or NULL. */
static const char *json_value(const char *json, const char *key) {
    size_t length = strlen(key);
    for (const char *at = json; (at = strchr(at, '"')); at++) {
        if (strncmp(at + 1, key, length) || at[length + 1] != '"') continue;
        const char *value = at + length + 2;
        while (isspace((unsigned char)*value)) value++;
        if (*value != ':') continue;
        for (value++; isspace((unsigned char)*value); value++) {}
        return value;
    }
    return NULL;
}

static bool json_number(const char *json, const char *key, double *number) {
    const char *value = json_value(json, key);
    if (!value) return false;
    bool quoted = *value == '"'; /* float("150") works in the Python version too */
    char *end;
    *number = strtod(value + quoted, &end);
    return end != value + quoted && isfinite(*number);
}

static bool json_string(const char *json, const char *key, char *text, size_t size) {
    const char *value = json_value(json, key);
    if (!value || *value != '"') return false;
    size_t length = 0;
    for (value++; *value && *value != '"' && length + 4 < size; value++) {
        if (*value != '\\') {
            text[length++] = *value;
        } else if (*++value == 'u') {
            unsigned code = 0;
            if (sscanf(value + 1, "%4x", &code) != 1) return false;
            value += 4;
            if (code < 0x80) {
                text[length++] = (char)code;
            } else if (code < 0x800) {
                text[length++] = (char)(0xC0 | code >> 6);
                text[length++] = (char)(0x80 | (code & 0x3F));
            } else {
                text[length++] = (char)(0xE0 | code >> 12);
                text[length++] = (char)(0x80 | (code >> 6 & 0x3F));
                text[length++] = (char)(0x80 | (code & 0x3F));
            }
        } else if (*value) {
            text[length++] = *value == 'n' ? '\n' : *value == 't' ? '\t' : *value;
        } else {
            return false;
        }
    }
    text[length] = 0;
    return *value == '"';
}

/* Power in watts by resistance level (rows, 1 to 24) and cadence (columns), from the manual of
 * the AX 4000 (Art.-Nr. 2007, "WATT TABELLE"). The manual gives it up to 80 rpm. */
#define LEVELS 24
#define COLUMNS 7
static const double WATT_TABLE_RPM[COLUMNS] = {20, 30, 40, 50, 60, 70, 80};
static const double WATT_TABLE[LEVELS][COLUMNS] = {
    {4, 8, 14, 20, 28, 35, 42}, {6, 11, 19, 27, 38, 48, 60}, {7, 13, 23, 34, 48, 61, 77}, {8, 16, 28, 41, 58, 74, 93},
    {9, 19, 33, 48, 68, 89, 110}, {10, 21, 37, 54, 78, 100, 125}, {11, 23, 41, 61, 88, 112, 142}, {13, 27, 47, 68, 98, 124, 159},
    {15, 29, 52, 76, 108, 137, 176}, {16, 31, 56, 82, 118, 148, 187}, {17, 35, 62, 90, 128, 164, 203}, {18, 37, 66, 96, 138, 172, 220},
    {19, 40, 70, 103, 148, 185, 236}, {20, 43, 75, 110, 158, 201, 252}, {22, 46, 79, 117, 168, 215, 269}, {23, 50, 85, 125, 178, 228, 286},
    {25, 53, 90, 133, 188, 246, 304}, {26, 56, 95, 141, 198, 251, 318}, {28, 59, 100, 149, 208, 272, 332}, {29, 63, 104, 156, 218, 283, 346},
    {31, 65, 108, 162, 228, 291, 361}, {32, 67, 113, 168, 238, 303, 378}, {34, 71, 120, 175, 248, 320, 397}, {36, 74, 127, 182, 258, 335, 416},
};
#define WATT_TABLE_EXPONENT 1.7 /* above 80 rpm the power is taken to grow with cadence^1.7, as measured on this ergometer */

/* The power the table gives for a level at a cadence, interpolated between its columns. */
static double table_power(int level, double cadence) {
    const double *row = WATT_TABLE[level - 1], *rpm = WATT_TABLE_RPM;
    if (cadence <= rpm[0]) return row[0] * cadence / rpm[0];
    if (cadence >= rpm[COLUMNS - 1]) return row[COLUMNS - 1] * pow(cadence / rpm[COLUMNS - 1], WATT_TABLE_EXPONENT);
    int i = 0;
    while (cadence >= rpm[i + 1]) i++;
    return row[i] + (row[i + 1] - row[i]) * (cadence - rpm[i]) / (rpm[i + 1] - rpm[i]);
}

/* The level, with fractions, at which the table gives this power at this cadence (1 to 24). */
static double table_level(double power, double cadence) {
    double powers[LEVELS];
    for (int level = 1; level <= LEVELS; level++) powers[level - 1] = table_power(level, cadence);
    if (power <= powers[0]) return 1.0;
    if (power >= powers[LEVELS - 1]) return LEVELS;
    int i = 0;
    while (power >= powers[i + 1]) i++;
    return i + 1 + (power - powers[i]) / (powers[i + 1] - powers[i]);
}

/* The AX 4000 advertises under its FitShow module name, e.g. "FS-1837D8". */
static const char *NAME_HINTS[] = {"fs-", "ax4000", "ax 4000", "christopeit"};

/* Indoor Bike Data fields in transmission order. Bit 0 is inverted: instantaneous speed is
 * present when the bit is NOT set. A scale of 1 means the value is a whole number. */
static const struct bike_field {
    int bit;
    const char *label;
    int size; /* bytes, little-endian */
    bool is_signed;
    double scale;
    const char *unit;
} BIKE_FIELDS[] = {
    {0, "speed", 2, false, 0.01, "km/h"},
    {1, "avg speed", 2, false, 0.01, "km/h"},
    {2, "cadence", 2, false, 0.5, "rpm"},
    {3, "avg cadence", 2, false, 0.5, "rpm"},
    {4, "distance", 3, false, 1, "m"}, /* uint24 */
    {5, "resistance", 2, true, 1, ""},
    {6, "power", 2, true, 1, "W"},
    {7, "avg power", 2, true, 1, "W"},
    {8, "energy", 2, false, 1, "kcal"},
    {8, "energy/h", 2, false, 1, "kcal/h"},
    {8, "energy/min", 1, false, 1, "kcal/min"},
    {9, "heart rate", 1, false, 1, "bpm"},
    {10, "MET", 1, false, 0.1, ""},
    {11, "elapsed", 2, false, 1, "s"},
    {12, "remaining", 2, false, 1, "s"},
};
#define FIELDS ((int)(sizeof BIKE_FIELDS / sizeof *BIKE_FIELDS))
enum { CADENCE = 2, RESISTANCE = 5, POWER = 6, HEART = 11 };

/* One Indoor Bike Data notification. */
struct sample {
    double t;       /* epoch ms */
    double target;  /* W, 0 = none */
    unsigned present; /* bit i: BIKE_FIELDS[i] was sent */
    double value[FIELDS];
    bool has_room;
    double room; /* °C */
};

/* The fields of a notification; false if it is too short to hold even the flags. */
static bool parse_indoor_bike_data(const uint8_t *data, size_t length, struct sample *sample) {
    if (length < 2) return false;
    unsigned flags = data[0] | data[1] << 8;
    size_t offset = 2;
    sample->present = 0;
    for (int i = 0; i < FIELDS; i++) {
        const struct bike_field *field = &BIKE_FIELDS[i];
        if (field->bit == 0 ? flags & 1 : !(flags & 1u << field->bit)) continue;
        if (offset + field->size > length) break;
        long raw = 0;
        for (int byte = 0; byte < field->size; byte++) raw |= (long)data[offset + byte] << (8 * byte);
        if (field->is_signed) raw = (int16_t)raw;
        offset += field->size;
        sample->value[i] = raw * field->scale;
        sample->present |= 1u << i;
    }
    return true;
}

static void print_value(const struct bike_field *field, double value) {
    printf(field->scale == 1 ? "  %s: %.0f" : "  %s: %.1f", field->label, value);
    if (*field->unit) printf(" %s", field->unit);
}

/* Shared between the Bluetooth callbacks, the control loop and the panel's HTTP threads.
 * Whoever touches SAMPLES, STATUS, CONTROL or ROOM holds LOCK (it is recursive). */
static pthread_mutex_t LOCK;
static struct sample *SAMPLES; /* append-only, never trimmed */
static size_t SAMPLE_COUNT, SAMPLE_CAPACITY;
static char STATUS[256] = "starting";
static struct {
    double temperature, t; /* the latest reading and when it arrived (epoch s) */
} ROOM;

struct step {
    double seconds, watts;
};
static struct {
    bool connected;        /* the resistance range is known: commands can be sent */
    double range[3];       /* resistance levels: low, high, step */
    double power_range[3];
    double target_power;
    double write_offset;
    /* Power profile from a file. A manual run is a profile without a name and without steps:
     * the slider sets the power. `start` is epoch ms, 0 while it does not run. */
    struct {
        bool loaded;
        char name[256];
        struct step *steps;
        int count;
        double start;
    } profile;
    double log_start; /* epoch ms of the "Start" press while a recording runs, else 0; see save_log() */
} CONTROL = {.power_range = {0, MAX_TARGET, 5}};

static char PROGRAM_DIR[1024] = "."; /* panel.html, .env, the profiles and logs/ live next to the program */
static bool RAW;

static void lock(void) { pthread_mutex_lock(&LOCK); }
static void unlock(void) { pthread_mutex_unlock(&LOCK); }

static const char *program_file(const char *name) {
    static _Thread_local char path[1400];
    snprintf(path, sizeof path, "%s/%s", PROGRAM_DIR, name);
    return path;
}

static char *read_file(const char *path) {
    FILE *file = fopen(path, "rb");
    if (!file) return NULL;
    struct buf text = {0};
    char chunk[4096];
    size_t count;
    buf_add(&text, "", 0);
    while ((count = fread(chunk, 1, sizeof chunk, file)) > 0) buf_add(&text, chunk, count);
    fclose(file);
    return text.text;
}

/* The settings from the .env file; variables that are already set in the environment win. */
static void read_env(void) {
    char *text = read_file(program_file(".env")), *rest = text, *line;
    while ((line = strsep(&rest, "\n"))) {
        char *value = strchr(line, '=');
        if (!value) continue;
        *value++ = 0;
        while (isspace((unsigned char)*line)) line++;
        for (char *end = line + strlen(line); end > line && isspace((unsigned char)end[-1]);) *--end = 0;
        while (isspace((unsigned char)*value)) value++;
        for (char *end = value + strlen(value); end > value && isspace((unsigned char)end[-1]);) *--end = 0;
        while (*value == '"' || *value == '\'') value++;
        for (char *end = value + strlen(value); end > value && (end[-1] == '"' || end[-1] == '\'');) *--end = 0;
        if (*line != '#') setenv(line, value, 0);
    }
    free(text);
    snprintf(MQTT_HOST, sizeof MQTT_HOST, "%s", getenv("MQTT_HOST") ?: "");
    snprintf(MQTT_PORT, sizeof MQTT_PORT, "%s", getenv("MQTT_PORT") ?: "");
    snprintf(MQTT_TOPIC, sizeof MQTT_TOPIC, "%s", getenv("MQTT_TOPIC") ?: "");
    if (atoi(MQTT_PORT) <= 0) MQTT_PORT[0] = 0;
}

/* QR code versions 1 to 6, which is plenty for a short address. Per error correction level:
 * the two bits that name it in the code, then for each version the error correction bytes
 * per block and the number of blocks. A code survives damage to about 7 % (L), 15 % (M),
 * 25 % (Q) or 30 % (H) of its bytes; more redundancy makes it larger. */
#define QR_VERSIONS 6
#define QR_MAX (17 + 4 * QR_VERSIONS)
static const int QR_CODEWORDS[QR_VERSIONS] = {26, 44, 70, 100, 134, 172}; /* all bytes of a version, data and error correction */
static const struct qr_level {
    char name;
    int bits;
    int blocks[QR_VERSIONS][2];
} QR_LEVELS[] = {
    {'L', 1, {{7, 1}, {10, 1}, {15, 1}, {20, 1}, {26, 1}, {18, 2}}},
    {'M', 0, {{10, 1}, {16, 1}, {26, 1}, {18, 2}, {24, 2}, {16, 4}}},
    {'Q', 3, {{13, 1}, {22, 1}, {18, 2}, {26, 2}, {18, 4}, {24, 4}}},
    {'H', 2, {{17, 1}, {28, 1}, {22, 2}, {16, 4}, {22, 4}, {28, 4}}},
};
#define QR_LEVEL 'H'

/* The code being built: its modules (true = dark) and which of them are not data. */
static struct {
    int size, level_bits;
    bool dark[QR_MAX][QR_MAX], fixed[QR_MAX][QR_MAX];
} qr;

/* The panel for a phone in the same network; see start_panel(). */
static struct {
    char url[64];
    int size;
    bool qr[QR_MAX][QR_MAX];
} COMPANION;

static bool qr_mask(int mask, int x, int y) {
    switch (mask) {
    case 0: return (x + y) % 2 == 0;
    case 1: return y % 2 == 0;
    case 2: return x % 3 == 0;
    case 3: return (x + y) % 3 == 0;
    case 4: return (x / 3 + y / 2) % 2 == 0;
    case 5: return x * y % 2 + x * y % 3 == 0;
    case 6: return (x * y % 2 + x * y % 3) % 2 == 0;
    default: return ((x + y) % 2 + x * y % 3) % 2 == 0;
    }
}

static int qr_multiply(int x, int y) { /* in the field GF(2^8) the Reed-Solomon code works in */
    int z = 0;
    for (int i = 7; i >= 0; i--) {
        z = ((z << 1) ^ ((z >> 7) * 0x11D)) & 0xFF;
        z ^= ((y >> i) & 1) * x;
    }
    return z;
}

static void qr_put(int x, int y, bool value) {
    if (x >= 0 && x < qr.size && y >= 0 && y < qr.size) {
        qr.dark[y][x] = value;
        qr.fixed[y][x] = true;
    }
}

static void qr_put_format(int mask) {
    int bits = qr.level_bits << 3 | mask, remainder = bits; /* the level and the mask, protected by a BCH code */
    for (int i = 0; i < 10; i++) remainder = (remainder << 1) ^ ((remainder >> 9) * 0x537);
    bits = (bits << 10 | remainder) ^ 0x5412;
    for (int i = 0; i < 15; i++) {
        bool bit = bits >> i & 1;
        if (i < 6) qr_put(8, i, bit);
        else if (i == 6) qr_put(8, 7, bit);
        else if (i == 7) qr_put(8, 8, bit);
        else if (i == 8) qr_put(7, 8, bit);
        else qr_put(14 - i, 8, bit);
        if (i < 8) qr_put(qr.size - 1 - i, 8, bit);
        else qr_put(8, qr.size - 15 + i, bit);
    }
    qr_put(8, qr.size - 8, true);
}

static int qr_penalty(void) {
    int size = qr.size, score = 0, count = 0;
    static const bool finder[2][11] = {{1, 0, 1, 1, 1, 0, 1, 0, 0, 0, 0}, {0, 0, 0, 0, 1, 0, 1, 1, 1, 0, 1}};
    for (int n = 0; n < 2 * size; n++) { /* the rows, then the columns */
        bool line[QR_MAX];
        for (int i = 0; i < size; i++) line[i] = n < size ? qr.dark[n][i] : qr.dark[i][n - size];
        int run = 1;
        for (int i = 1; i <= size; i++) { /* runs of five or more of one colour */
            if (i < size && line[i] == line[i - 1]) {
                run++;
            } else {
                score += run >= 5 ? run - 2 : 0;
                run = 1;
            }
        }
        for (int i = 0; i < size - 10; i++) /* anything that looks like a finder pattern */
            for (int pattern = 0; pattern < 2; pattern++) score += 40 * !memcmp(line + i, finder[pattern], sizeof finder[pattern]);
    }
    for (int y = 0; y < size - 1; y++)
        for (int x = 0; x < size - 1; x++)
            score += 3 * (qr.dark[y][x] == qr.dark[y][x + 1] && qr.dark[y][x] == qr.dark[y + 1][x] && qr.dark[y][x] == qr.dark[y + 1][x + 1]);
    for (int y = 0; y < size; y++)
        for (int x = 0; x < size; x++) count += qr.dark[y][x]; /* balance of dark and light */
    return score + 10 * ((abs(count * 20 - size * size * 10) + size * size - 1) / (size * size) - 1);
}

static void qr_apply(int mask) {
    for (int y = 0; y < qr.size; y++)
        for (int x = 0; x < qr.size; x++) qr.dark[y][x] ^= qr_mask(mask, x, y) && !qr.fixed[y][x];
    qr_put_format(mask);
}

/* The QR code for a short text (ISO 18004, byte mode, level QR_LEVEL): fills `qr` and
 * returns its size in modules, or 0 if the text is too long. */
static int qr_matrix(const char *text) {
    const struct qr_level *level = QR_LEVELS;
    while (level->name != QR_LEVEL) level++;
    int length = (int)strlen(text), version = 0, capacity = 0;
    for (int v = 1; v <= QR_VERSIONS && !version; v++) {
        capacity = QR_CODEWORDS[v - 1] - level->blocks[v - 1][0] * level->blocks[v - 1][1];
        if (length <= capacity - 2) version = v;
    }
    if (!version) return 0;
    int ec_length = level->blocks[version - 1][0], n_blocks = level->blocks[version - 1][1];
    memset(&qr, 0, sizeof qr);
    qr.size = 17 + 4 * version;
    qr.level_bits = level->bits;
    int size = qr.size;

    /* Mode and length, the bytes, a terminator, then padding up to the capacity. The mode is
     * four bits, so every byte straddles two codewords. */
    uint8_t codewords[172] = {0};
    codewords[0] = 0x40 | length >> 4;
    codewords[1] = (length & 0x0F) << 4;
    for (int i = 0; i < length; i++) {
        codewords[i + 1] |= (uint8_t)text[i] >> 4;
        codewords[i + 2] = ((uint8_t)text[i] & 0x0F) << 4;
    }
    for (int i = length + 2; i < capacity; i++) codewords[i] = (i - length - 2) % 2 ? 0x11 : 0xEC;

    int divisor[30] = {0}, root = 1;
    divisor[ec_length - 1] = 1;
    for (int i = 0; i < ec_length; i++) {
        for (int j = 0; j < ec_length; j++) divisor[j] = qr_multiply(divisor[j], root) ^ (j + 1 < ec_length ? divisor[j + 1] : 0);
        root = qr_multiply(root, 2);
    }

    /* The data is split into blocks that are protected separately; the later ones are a byte
     * longer when it doesn't divide evenly. Their bytes are then dealt out in turn, so damage
     * in one place is spread over all blocks. */
    int shorter = capacity / n_blocks, longer = capacity % n_blocks, at = 0, start[4], lengths[4], checks[4][30];
    for (int b = 0; b < n_blocks; b++) {
        start[b] = at;
        lengths[b] = shorter + (b >= n_blocks - longer);
        at += lengths[b];
        memset(checks[b], 0, sizeof checks[b]);
        for (int i = 0; i < lengths[b]; i++) {
            int factor = codewords[start[b] + i] ^ checks[b][0];
            for (int j = 0; j < ec_length; j++) checks[b][j] = (j + 1 < ec_length ? checks[b][j + 1] : 0) ^ qr_multiply(divisor[j], factor);
        }
    }
    uint8_t stream[172];
    int stream_length = 0;
    for (int i = 0; i <= shorter; i++)
        for (int b = 0; b < n_blocks; b++)
            if (i < lengths[b]) stream[stream_length++] = codewords[start[b] + i];
    for (int i = 0; i < ec_length; i++)
        for (int b = 0; b < n_blocks; b++) stream[stream_length++] = (uint8_t)checks[b][i];

    for (int i = 0; i < size; i++) { /* timing patterns */
        qr_put(6, i, i % 2 == 0);
        qr_put(i, 6, i % 2 == 0);
    }
    const int finders[3][2] = {{3, 3}, {size - 4, 3}, {3, size - 4}};
    for (int f = 0; f < 3; f++) /* finder patterns with their separators */
        for (int dy = -4; dy <= 4; dy++)
            for (int dx = -4; dx <= 4; dx++) {
                int ring = abs(dx) > abs(dy) ? abs(dx) : abs(dy);
                qr_put(finders[f][0] + dx, finders[f][1] + dy, ring != 2 && ring != 4);
            }
    if (version > 1) /* alignment pattern */
        for (int dy = -2; dy <= 2; dy++)
            for (int dx = -2; dx <= 2; dx++) qr_put(size - 7 + dx, size - 7 + dy, (abs(dx) > abs(dy) ? abs(dx) : abs(dy)) != 1);
    qr_put_format(0); /* reserves the modules */

    /* The data runs in a zigzag of two-module columns from the bottom right, skipping the timing column. */
    int bit = 0;
    for (int right = size - 1; right >= 1; right -= 2) {
        if (right == 6) right = 5;
        for (int vertical = 0; vertical < size; vertical++)
            for (int x = right; x >= right - 1; x--) {
                int y = ((right + 1) & 2) == 0 ? size - 1 - vertical : vertical;
                if (!qr.fixed[y][x] && bit < stream_length * 8) {
                    qr.dark[y][x] = stream[bit >> 3] >> (7 - (bit & 7)) & 1;
                    bit++;
                }
            }
    }

    /* The mask that gives the calmest picture wins. */
    int best = 0, best_score = 0;
    for (int mask = 0; mask < 8; mask++) {
        qr_apply(mask);
        int score = qr_penalty();
        if (mask == 0 || score < best_score) best = mask, best_score = score;
        qr_apply(mask); /* masking twice undoes it */
    }
    qr_apply(best);
    return size;
}

/* The QR code for a terminal: two rows of modules per line, black on white whatever the terminal's colours are. */
static void qr_print(void) {
    static const char *blocks[] = {" ", "▄", "▀", "█"};
    int quiet = 2, size = COMPANION.size + 2 * quiet;
    for (int y = 0; y < size; y += 2) {
        printf("\033[30;107m");
        for (int x = 0; x < size; x++) {
            bool inside = x >= quiet && x - quiet < COMPANION.size;
            bool upper = inside && y >= quiet && y - quiet < COMPANION.size && COMPANION.qr[y - quiet][x - quiet];
            bool lower = inside && y + 1 >= quiet && y + 1 - quiet < COMPANION.size && COMPANION.qr[y + 1 - quiet][x - quiet];
            fputs(blocks[2 * upper + lower], stdout);
        }
        printf("\033[0m\n");
    }
}

/* This computer's address in the local network (nothing is sent). */
static void local_address(char *address, size_t size) {
    snprintf(address, size, "127.0.0.1");
    int probe = socket(AF_INET, SOCK_DGRAM, 0);
    struct sockaddr_in remote = {.sin_family = AF_INET, .sin_port = htons(9)}, local;
    socklen_t length = sizeof local;
    inet_pton(AF_INET, "192.0.2.1", &remote.sin_addr);
    if (probe >= 0 && connect(probe, (struct sockaddr *)&remote, sizeof remote) == 0 &&
        getsockname(probe, (struct sockaddr *)&local, &length) == 0)
        inet_ntop(AF_INET, &local.sin_addr, address, (socklen_t)size);
    if (probe >= 0) close(probe);
}

static char *strip(char *text) {
    while (isspace((unsigned char)*text)) text++;
    for (char *end = text + strlen(text); end > text && isspace((unsigned char)end[-1]);) *--end = 0;
    return text;
}

static int compare_steps(const void *a, const void *b) {
    const struct step *x = a, *y = b;
    return x->seconds != y->seconds ? (x->seconds > y->seconds) - (x->seconds < y->seconds) : (x->watts > y->watts) - (x->watts < y->watts);
}

/* Read "mm:ss,watt" rows; each row sets the target power from that time on. The caller frees
 * the steps. Returns 0 when no row has that form. */
static int parse_profile(const char *text, struct step **steps) {
    char *copy = strdup(text), *rest = copy, *line;
    int count = 0;
    *steps = NULL;
    while ((line = strsep(&rest, "\n"))) {
        char *fields[2], *field;
        int found = 0;
        while (found < 2 && (field = strsep(&line, ",;\t")))
            if (*(field = strip(field))) fields[found++] = field;
        if (found < 2) continue; /* header, blank or comment line */
        long seconds = 0;
        bool valid = true;
        for (char *part = fields[0]; valid;) { /* "::" counts as ":" */
            char *end;
            long number = strtol(part, &end, 10);
            valid = end != part && (*strip(end) == 0 || *end == ':');
            seconds = seconds * 60 + number;
            if (*end != ':') break;
            part = end + 1 + (end[1] == ':');
        }
        char *end;
        double watts = strtod(fields[1], &end);
        if (!valid || end == fields[1] || *end) continue;
        *steps = realloc(*steps, (count + 1) * sizeof **steps);
        (*steps)[count++] = (struct step){seconds, watts};
    }
    free(copy);
    if (count) qsort(*steps, count, sizeof **steps, compare_steps);
    return count;
}

/* End the recording and write its samples to logs/log-<yy-mm-dd-hh-mm-ss>.csv, named after the start.
 *
 * Samples only arrive while the ergometer is connected, so the file holds exactly
 * what was measured between "Start" and "Stop" (or the end of the profile). */
static void save_log(void) {
    lock();
    double start = CONTROL.log_start;
    CONTROL.log_start = 0;
    size_t first = SAMPLE_COUNT;
    while (start && first > 0 && SAMPLES[first - 1].t >= start) first--;
    size_t rows = SAMPLE_COUNT - first;
    if (!start || !rows) {
        unlock();
        return;
    }
    unsigned present = 0;
    bool room = false;
    for (size_t i = first; i < SAMPLE_COUNT; i++) present |= SAMPLES[i].present, room |= SAMPLES[i].has_room;

    char name[64], path[1400];
    time_t seconds = (time_t)(start / 1000);
    struct tm local;
    strftime(name, sizeof name, "log-%y-%m-%d-%H-%M-%S.csv", localtime_r(&seconds, &local));
    mkdir(program_file("logs"), 0755);
    snprintf(path, sizeof path, "%s/logs/%s", PROGRAM_DIR, name);
    FILE *file = fopen(path, "w");
    if (!file) {
        printf("%s  could not write %s: %s\n", stamp(), path, strerror(errno));
        unlock();
        return;
    }
    fputs("time,seconds,target", file);
    for (int i = 0; i < FIELDS; i++)
        if (present & 1u << i) fprintf(file, ",%s", BIKE_FIELDS[i].label);
    fputs(room ? ",room temperature\r\n" : "\r\n", file);
    for (size_t i = first; i < SAMPLE_COUNT; i++) {
        const struct sample *row = &SAMPLES[i];
        char time_text[32];
        seconds = (time_t)(row->t / 1000);
        strftime(time_text, sizeof time_text, "%Y-%m-%dT%H:%M:%S", localtime_r(&seconds, &local));
        fprintf(file, "%s.%03d,%.1f,%g", time_text, (int)fmod(row->t, 1000), (row->t - start) / 1000, row->target);
        for (int f = 0; f < FIELDS; f++) {
            if (!(present & 1u << f)) continue;
            if (row->present & 1u << f) fprintf(file, ",%g", row->value[f]);
            else fputc(',', file);
        }
        if (room && row->has_room) fprintf(file, ",%g", row->room);
        else if (room) fputc(',', file);
        fputs("\r\n", file);
    }
    fclose(file);
    printf("%s  log saved: %s (%zu samples)\n", stamp(), path, rows);
    unlock();
}

static void clear_profile(void) {
    free(CONTROL.profile.steps);
    memset(&CONTROL.profile, 0, sizeof CONTROL.profile);
}

static bool load_profile(const char *name, const char *text) {
    save_log(); /* a running profile is replaced */
    struct step *steps;
    int count = parse_profile(text, &steps);
    if (!count) return false;
    clear_profile();
    CONTROL.profile.loaded = true;
    snprintf(CONTROL.profile.name, sizeof CONTROL.profile.name, "%s", name);
    CONTROL.profile.steps = steps;
    CONTROL.profile.count = count;
    return true;
}

/* Start the loaded profile and, with it, the recording of the measured values. */
static void start_profile(void) {
    save_log();
    CONTROL.profile.start = CONTROL.log_start = now_ms();
}

static void stop_profile(void) {
    CONTROL.profile.start = 0;
    save_log();
}

static int compare_names(const void *a, const void *b) { return strcmp(*(char *const *)a, *(char *const *)b); }

/* The profiles offered in the panel: vorgabe-<name>.csv next to this program. The caller frees them. */
static int profile_files(char ***names) {
    int count = 0;
    *names = NULL;
    DIR *directory = opendir(PROGRAM_DIR);
    struct dirent *entry;
    while (directory && (entry = readdir(directory))) {
        size_t length = strlen(entry->d_name);
        if (strncmp(entry->d_name, "vorgabe-", 8) || length < 12 || strcmp(entry->d_name + length - 4, ".csv")) continue;
        *names = realloc(*names, (count + 1) * sizeof **names);
        (*names)[count++] = strdup(entry->d_name);
    }
    if (directory) closedir(directory);
    if (count) qsort(*names, count, sizeof **names, compare_names);
    return count;
}

static void free_names(char **names, int count) {
    for (int i = 0; i < count; i++) free(names[i]);
    free(names);
}

/* Take the target power from the running profile, if there is one. */
static void follow_profile(void) {
    if (!CONTROL.profile.loaded || !CONTROL.profile.start || !CONTROL.profile.count) return; /* in a manual run the slider sets the power */
    const struct step *steps = CONTROL.profile.steps, *last = &steps[CONTROL.profile.count - 1];
    double elapsed = (now_ms() - CONTROL.profile.start) / 1000;
    CONTROL.target_power = 0;
    for (int i = CONTROL.profile.count - 1; i >= 0; i--)
        if (steps[i].seconds <= elapsed) {
            CONTROL.target_power = steps[i].watts;
            break;
        }
    if (elapsed >= last->seconds && last->watts == 0) {
        stop_profile();
        printf("%s  profile %s finished\n", stamp(), CONTROL.profile.name);
    }
}

static void set_status(const char *format, ...) {
    va_list args;
    va_start(args, format);
    lock();
    vsnprintf(STATUS, sizeof STATUS, format, args);
    puts(STATUS);
    unlock();
    va_end(args);
}

/* FTMS "Set Target Resistance Level": opcode 0x04, sint16 in steps of 0.1. */
static bool write_resistance(double target) {
    int16_t tenths = (int16_t)lrint(target * 10);
    uint8_t command[3] = {0x04, tenths & 0xFF, tenths >> 8 & 0xFF};
    return ble_write(CONTROL_POINT, command, sizeof command);
}

static bool set_resistance(double level) {
    lock();
    level = clamp(rint(level), CONTROL.range[0], CONTROL.range[1]);
    double offset = CONTROL.write_offset;
    unlock();
    if (!write_resistance(level + offset)) return false;
    /* The AX 4000 can land one level beside the requested one, so check what it
     * reports, remember the difference for later writes and correct. */
    sleep_for(1.5);
    lock();
    const struct sample *last = SAMPLE_COUNT ? &SAMPLES[SAMPLE_COUNT - 1] : NULL;
    bool beside = last && last->present & 1u << RESISTANCE && last->value[RESISTANCE] != level;
    if (beside) offset = CONTROL.write_offset = clamp(CONTROL.write_offset + level - last->value[RESISTANCE], -2, 2);
    unlock();
    if (beside && !write_resistance(fmax(level + offset, 0))) return false;
    printf("%s  resistance set to %g\n", stamp(), level);
    return true;
}

/* Steer the resistance level so the measured power follows the target.
 *
 * A new target sets the level straight from the manufacturer's table for the current cadence.
 * The PID loop only trims from there: it corrects what the table gets wrong on this
 * ergometer and follows the rider's cadence.
 *
 * The AX 4000 acknowledges FTMS "Set Target Power" (opcode 0x05) but then ignores
 * it and keeps its resistance level, so the control has to happen here. */
static void *power_control(void *unused) {
    (void)unused;
    bool has_integral = false, has_error = false;
    double integral = 0, last_error = 0, last_target = 0;
    double settled = 0; /* from when on the PID may work again after a jump */
    double last_time = monotonic();
    for (;;) {
        sleep_for(1);
        double now = monotonic(), dt = now - last_time;
        last_time = now;

        lock();
        follow_profile();
        double target = CONTROL.target_power, power = 0, cadence = 0, level = 0, t = 0;
        int recent = 0;
        for (size_t i = SAMPLE_COUNT > 3 ? SAMPLE_COUNT - 3 : 0; i < SAMPLE_COUNT; i++) {
            const struct sample *sample = &SAMPLES[i];
            if (!(sample->present & 1u << POWER) || !(sample->present & 1u << RESISTANCE)) continue;
            recent++;
            power += sample->value[POWER];
            cadence = sample->present & 1u << CADENCE ? sample->value[CADENCE] : 0;
            level = sample->value[RESISTANCE];
            t = sample->t;
        }
        double low = CONTROL.range[0], high = CONTROL.range[1];
        bool connected = CONTROL.connected;
        unlock();

        if (!connected || !target || !recent || now_ms() - t > 3000) {
            has_integral = false;
            continue;
        }
        if (cadence < MIN_CADENCE) continue; /* hold instead of winding the resistance up while nobody pedals */

        if (!has_integral || target != last_target) {
            /* A new target: jump to the level the table gives for it at this cadence. What the
             * controller had to add to the table for the old target is carried over. */
            double trim = has_integral && last_target ? integral - table_level(last_target, cadence) : 0.0;
            trim = clamp(trim, -PID_MAX_TRIM, PID_MAX_TRIM);
            integral = clamp(table_level(target, cadence) + trim, low, high);
            has_integral = true;
            last_target = target;
            has_error = false;
            if (rint(integral) != level && !set_resistance(integral)) printf("%s  could not set resistance\n", stamp());
            settled = monotonic() + PID_SETTLE;
            continue;
        }
        if (now < settled) continue; /* the measured power still belongs to the old level */

        double error = target - power / recent;
        if (fabs(error) < PID_TOLERANCE) error = 0.0;
        if (!has_error) last_error = error; /* no kick from the derivative on the first step after a jump */
        has_error = true;
        integral = clamp(integral + PID_KI * error * dt, low, high); /* clamped: anti-windup */
        double output = clamp(integral + PID_KP * error + PID_KD * (error - last_error) / dt, low, high);
        last_error = error;
        if (fabs(output - level) > PID_DEADBAND && !set_resistance(output)) printf("%s  could not set resistance\n", stamp());
    }
    return NULL;
}

/* Send everything; false if the connection broke. */
static bool send_all(int connection, const void *data, size_t length) {
    for (const char *at = data; length;) {
        ssize_t sent = send(connection, at, length, 0);
        if (sent <= 0) return false;
        at += sent, length -= (size_t)sent;
    }
    return true;
}

/* Receive exactly `count` bytes: 1 = done, 0 = nothing came within the timeout, -1 = the connection broke. */
static int receive_all(int connection, uint8_t *data, size_t count) {
    for (size_t have = 0; have < count;) {
        ssize_t got = recv(connection, data + have, count - have, 0);
        if (got < 0 && (errno == EAGAIN || errno == EWOULDBLOCK) && !have) return 0;
        if (got <= 0) return -1;
        have += (size_t)got;
    }
    return 1;
}

/* An MQTT control packet: type and flags, the length in 7-bit groups, then the body. */
static bool mqtt_send(int connection, int kind, const uint8_t *body, size_t length) {
    uint8_t packet[600] = {(uint8_t)kind};
    size_t at = 1, rest = length;
    do {
        packet[at++] = (rest % 128) | (rest >= 128 ? 0x80 : 0);
        rest /= 128;
    } while (rest);
    memcpy(packet + at, body, length);
    return send_all(connection, packet, at + length);
}

static size_t mqtt_string(uint8_t *at, const char *text) {
    size_t length = strlen(text);
    at[0] = length >> 8, at[1] = length & 0xFF;
    memcpy(at + 2, text, length);
    return length + 2;
}

static int connect_to(const char *host, const char *port, int timeout_s) {
    struct addrinfo hints = {.ai_socktype = SOCK_STREAM}, *addresses;
    if (getaddrinfo(host, port, &hints, &addresses)) return -1;
    int connection = -1;
    for (struct addrinfo *address = addresses; address && connection < 0; address = address->ai_next) {
        int candidate = socket(address->ai_family, address->ai_socktype, address->ai_protocol);
        if (candidate < 0) continue;
        fcntl(candidate, F_SETFL, O_NONBLOCK);
        struct pollfd wait = {candidate, POLLOUT, 0};
        int failed = 0;
        socklen_t size = sizeof failed;
        if ((connect(candidate, address->ai_addr, address->ai_addrlen) == 0 || errno == EINPROGRESS) &&
            poll(&wait, 1, timeout_s * 1000) == 1 && getsockopt(candidate, SOL_SOCKET, SO_ERROR, &failed, &size) == 0 && !failed) {
            fcntl(candidate, F_SETFL, 0);
            connection = candidate;
        } else {
            close(candidate);
        }
    }
    freeaddrinfo(addresses);
    return connection;
}

/* Keep ROOM up to date from the MQTT topic; runs in its own thread and reconnects for ever.
 *
 * A small MQTT 3.1.1 client, enough to subscribe to one topic without acknowledgements. */
static void *follow_temperature(void *unused) {
    (void)unused;
    for (;;) {
        int connection = connect_to(MQTT_HOST, MQTT_PORT, 10);
        if (connection >= 0) {
            /* Connect with a clean session and a keep-alive of 60 s, then subscribe. */
            uint8_t body[560], kind;
            char client[32];
            size_t length = mqtt_string(body, "MQTT");
            memcpy(body + length, (uint8_t[]){4, 2, 0, 60}, 4);
            snprintf(client, sizeof client, "ergometer-%08x", (unsigned)(uint64_t)(now_ms() * 1000) ^ (unsigned)getpid() << 16);
            length += 4 + mqtt_string(body + length + 4, client);
            bool ok = mqtt_send(connection, 0x10, body, length);
            body[0] = 0, body[1] = 1;
            length = 2 + mqtt_string(body + 2, MQTT_TOPIC);
            body[length++] = 0;
            ok = ok && mqtt_send(connection, 0x82, body, length);
            setsockopt(connection, SOL_SOCKET, SO_RCVTIMEO, &(struct timeval){30, 0}, sizeof(struct timeval));
            double pinged = monotonic();
            while (ok) {
                int got = receive_all(connection, &kind, 1);
                if (got == 0 || monotonic() - pinged > 30) {
                    /* Ping, so the broker keeps the connection: it wants to hear from us within the
                     * keep-alive, however much it sends itself. */
                    ok = mqtt_send(connection, 0xC0, NULL, 0);
                    pinged = monotonic();
                }
                if (got == 0) continue;
                size_t size = 0;
                uint8_t digit = 0x80;
                for (int shift = 0; got == 1 && digit & 0x80 && shift < 28; shift += 7)
                    if ((got = receive_all(connection, &digit, 1)) == 1) size |= (size_t)(digit & 0x7F) << shift;
                uint8_t *message = got == 1 && size < 1 << 20 ? malloc(size + 1) : NULL;
                if (!message || (size && receive_all(connection, message, size) != 1)) {
                    free(message);
                    break;
                }
                message[size] = 0;
                size_t payload = size >= 2 ? 2 + (message[0] << 8 | message[1]) + (kind & 0x06 ? 2 : 0) : size;
                double reading;
                if (kind >> 4 == 3 && payload < size && json_number((char *)message + payload, MQTT_FIELD, &reading)) { /* only published messages matter */
                    lock();
                    ROOM.temperature = round((reading + MQTT_OFFSET) * 10) / 10;
                    ROOM.t = now_ms() / 1000;
                    unlock();
                }
                free(message);
            }
            close(connection);
        }
        printf("%s  room temperature: %s; trying again in 30 s\n", stamp(),
               connection >= 0 ? "the MQTT broker closed the connection" : "no connection to the MQTT broker");
        sleep_for(30);
    }
    return NULL;
}

static void json_profile(struct buf *json) {
    if (!CONTROL.profile.loaded) {
        buf_printf(json, "null");
        return;
    }
    buf_printf(json, "{\"name\": ");
    buf_json_string(json, CONTROL.profile.name);
    buf_printf(json, ", \"steps\": [");
    for (int i = 0; i < CONTROL.profile.count; i++)
        buf_printf(json, "%s[%.10g, %.10g]", i ? ", " : "", CONTROL.profile.steps[i].seconds, CONTROL.profile.steps[i].watts);
    if (CONTROL.profile.start) buf_printf(json, "], \"start\": %.3f}", CONTROL.profile.start);
    else buf_printf(json, "], \"start\": null}");
}

static void http_reply(int connection, int status, const char *content_type, const char *body, size_t length) {
    char head[256];
    int head_length = snprintf(head, sizeof head, "HTTP/1.1 %d %s\r\nContent-Type: %s\r\nContent-Length: %zu\r\n"
                               "Cache-Control: no-store\r\nConnection: close\r\n\r\n",
                               status, status == 200 ? "OK" : status == 404 ? "Not Found" : "Bad Request", content_type, length);
    if (send_all(connection, head, (size_t)head_length)) send_all(connection, body, length);
}

static void http_get(int connection, const char *path, const char *query) {
    struct buf json = {0};
    if (!strcmp(path, "/")) {
        char *panel = read_file(program_file("panel.html"));
        if (panel) http_reply(connection, 200, "text/html; charset=utf-8", panel, strlen(panel));
        else http_reply(connection, 404, "text/plain", "panel.html is missing\n", 22);
        free(panel);
        return;
    } else if (!strcmp(path, "/qr")) {
        buf_printf(&json, "{\"url\": ");
        buf_json_string(&json, COMPANION.url);
        buf_printf(&json, ", \"modules\": [");
        for (int y = 0; y < COMPANION.size; y++) {
            buf_printf(&json, y ? ", \"" : "\"");
            for (int x = 0; x < COMPANION.size; x++) buf_add(&json, COMPANION.qr[y][x] ? "1" : "0", 1);
            buf_add(&json, "\"", 1);
        }
        buf_printf(&json, "]}");
    } else if (!strcmp(path, "/data")) {
        const char *since_text = query ? strstr(query, "since=") : NULL;
        long since = since_text ? strtol(since_text + 6, NULL, 10) : 0;
        char **names;
        int name_count = profile_files(&names);
        struct stat panel = {0};
        stat(program_file("panel.html"), &panel);
        lock();
        buf_printf(&json, "{\"status\": ");
        buf_json_string(&json, STATUS);
        buf_printf(&json, ", \"next\": %zu, \"samples\": [", SAMPLE_COUNT);
        size_t first = since < 0 ? 0 : (size_t)since;
        for (size_t i = first; i < SAMPLE_COUNT; i++) {
            const struct sample *sample = &SAMPLES[i];
            buf_printf(&json, "%s{\"t\": %.3f, \"target\": ", i > first ? ", " : "", sample->t);
            if (sample->target) buf_printf(&json, "%.10g", sample->target);
            else buf_printf(&json, "null");
            for (int f = 0; f < FIELDS; f++)
                if (sample->present & 1u << f) buf_printf(&json, ", \"%s\": %.10g", BIKE_FIELDS[f].label, sample->value[f]);
            if (sample->has_room) buf_printf(&json, ", \"room temperature\": %.10g", sample->room);
            buf_printf(&json, "}");
        }
        buf_printf(&json, "], \"connected\": %s, \"power_range\": [%.10g, %.10g, %.10g], \"target_power\": %.10g, \"profile\": ",
                   CONTROL.connected ? "true" : "false", CONTROL.power_range[0], CONTROL.power_range[1], CONTROL.power_range[2],
                   CONTROL.target_power);
        json_profile(&json);
        buf_printf(&json, ", \"profiles\": [");
        for (int i = 0; i < name_count; i++) {
            buf_printf(&json, i ? ", " : "");
            buf_json_string(&json, names[i]);
        }
        /* Room temperature in °C, or null while there is no reading from the last two minutes. */
        if (now_ms() / 1000 - ROOM.t < 120) buf_printf(&json, "], \"temperature\": %.10g", ROOM.temperature);
        else buf_printf(&json, "], \"temperature\": null");
        /* Lets an open page notice that panel.html was edited and reload itself. */
        buf_printf(&json, ", \"panel_version\": %lld.%06ld}", (long long)panel.st_mtime,
#ifdef __APPLE__
                   panel.st_mtimespec.tv_nsec / 1000
#else
                   panel.st_mtim.tv_nsec / 1000
#endif
        );
        unlock();
        free_names(names, name_count);
    } else {
        http_reply(connection, 404, "text/plain", "not found\n", 10);
        return;
    }
    http_reply(connection, 200, "application/json", json.text, json.length);
    free(json.text);
}

static void http_post(int connection, const char *path, const char *request) {
    const char *error = NULL;
    char name[256] = "", action[32] = "";
    double power;
    bool has_power = json_number(request, "power", &power);
    bool has_name = json_string(request, "name", name, sizeof name);
    json_string(request, "action", action, sizeof action);
    bool is_profile = !strcmp(path, "/profile"), known = false;
    char **names;
    int name_count = profile_files(&names);
    for (int i = 0; has_name && i < name_count; i++) known |= !strcmp(names[i], name);
    free_names(names, name_count);

    lock();
    bool has_profile = CONTROL.profile.loaded, has_steps = has_profile && CONTROL.profile.count;
    double limit = clamp(has_power ? power : CONTROL.target_power, 0, CONTROL.power_range[1]);
    if (!strcmp(path, "/target")) {
        if (!has_power) {
            error = "power is missing";
        } else {
            /* Setting the power by hand takes over from a running profile; a manual run just carries on. */
            if (has_steps) stop_profile();
            CONTROL.target_power = limit;
            printf("%s  target power: %g W\n", stamp(), CONTROL.target_power);
        }
    } else if (is_profile && known) {
        char *text = read_file(program_file(name));
        if (text && load_profile(name, text)) {
            CONTROL.target_power = 0;
            printf("%s  profile loaded: %s\n", stamp(), name);
        } else {
            error = "no rows of the form mm:ss,watt found";
        }
        free(text);
    } else if (is_profile && has_name && !*name) {
        save_log();
        clear_profile();
        CONTROL.target_power = 0;
    } else if (is_profile && !strcmp(action, "start")) {
        if (!has_profile) {
            /* "Start" without a profile is a manual run at the power the slider shows. */
            clear_profile();
            CONTROL.profile.loaded = true;
            CONTROL.target_power = limit;
        }
        start_profile();
        if (has_steps) printf("%s  profile started: %s\n", stamp(), CONTROL.profile.name);
        else printf("%s  manual run started at %g W\n", stamp(), CONTROL.target_power);
    } else if (is_profile && has_profile && !strcmp(action, "stop")) {
        stop_profile();
        CONTROL.target_power = 0;
        if (!has_steps) clear_profile(); /* a manual run leaves nothing behind */
        printf("%s  profile stopped\n", stamp());
    } else {
        error = "unknown request";
    }
    struct buf json = {0};
    if (error) {
        buf_printf(&json, "{\"error\": ");
        buf_json_string(&json, error);
        buf_printf(&json, "}");
    } else {
        buf_printf(&json, "{\"power\": %.10g, \"profile\": ", CONTROL.target_power);
        json_profile(&json);
        buf_printf(&json, "}");
    }
    unlock();
    http_reply(connection, error ? 400 : 200, "application/json", json.text, json.length);
    free(json.text);
}

/* One request per connection. */
static void *http_serve(void *argument) {
    int connection = (int)(intptr_t)argument;
    char request[65536];
    size_t length = 0;
    char *body = NULL;
    setsockopt(connection, SOL_SOCKET, SO_RCVTIMEO, &(struct timeval){10, 0}, sizeof(struct timeval));
    while (!body && length < sizeof request - 1) {
        ssize_t got = recv(connection, request + length, sizeof request - 1 - length, 0);
        if (got <= 0) break;
        request[length += (size_t)got] = 0;
        body = strstr(request, "\r\n\r\n");
    }
    char method[8], target[1024];
    if (body && sscanf(request, "%7s %1023s", method, target) == 2) {
        body += 4;
        size_t expected = 0;
        for (char *line = request; (line = strstr(line, "\r\n")) && line < body - 2;) {
            line += 2;
            if (!strncasecmp(line, "Content-Length:", 15)) expected = (size_t)strtoul(line + 15, NULL, 10);
        }
        size_t have = length - (size_t)(body - request);
        while (have < expected && length < sizeof request - 1) {
            ssize_t got = recv(connection, request + length, sizeof request - 1 - length, 0);
            if (got <= 0) break;
            request[length += (size_t)got] = 0;
            have += (size_t)got;
        }
        char *query = strchr(target, '?');
        if (query) *query++ = 0;
        if (!strcmp(method, "GET")) http_get(connection, target, query);
        else if (!strcmp(method, "POST")) http_post(connection, target, body);
        else http_reply(connection, 400, "text/plain", "unsupported method\n", 19);
    }
    close(connection);
    return NULL;
}

static void *http_accept(void *argument) {
    int server = (int)(intptr_t)argument;
    pthread_attr_t detached;
    pthread_attr_init(&detached);
    pthread_attr_setdetachstate(&detached, PTHREAD_CREATE_DETACHED);
    for (;;) {
        int connection = accept(server, NULL, NULL);
        pthread_t thread;
        if (connection >= 0 && pthread_create(&thread, &detached, http_serve, (void *)(intptr_t)connection)) close(connection);
    }
    return NULL;
}

static void run_detached(char *const arguments[]) {
    pid_t child;
    extern char **environ;
    posix_spawnp(&child, arguments[0], NULL, NULL, arguments, environ);
}

static void start_panel(int port, bool open_browser) {
    /* The panel listens in the whole local network, so a phone can be the remote control at the
     * ergometer: anyone in that network can open it. The QR code only saves typing the address. */
    char address[INET_ADDRSTRLEN], url[64];
    local_address(address, sizeof address);
    snprintf(COMPANION.url, sizeof COMPANION.url, "http://%s:%d/", address, port);
    COMPANION.size = qr_matrix(COMPANION.url);
    memcpy(COMPANION.qr, qr.dark, sizeof COMPANION.qr);

    int server = socket(AF_INET, SOCK_STREAM, 0);
    struct sockaddr_in everywhere = {.sin_family = AF_INET, .sin_port = htons((uint16_t)port), .sin_addr.s_addr = htonl(INADDR_ANY)};
    setsockopt(server, SOL_SOCKET, SO_REUSEADDR, &(int){1}, sizeof(int));
    if (server < 0 || bind(server, (struct sockaddr *)&everywhere, sizeof everywhere) || listen(server, 16)) {
        fprintf(stderr, "Panel on port %d: %s\n", port, strerror(errno));
        exit(1);
    }
    pthread_t thread;
    pthread_create(&thread, NULL, http_accept, (void *)(intptr_t)server);
    if (*MQTT_HOST && *MQTT_PORT) pthread_create(&thread, NULL, follow_temperature, NULL);
    snprintf(url, sizeof url, "http://127.0.0.1:%d/", port);
    printf("Panel: %s\nPhone: %s\n", url, COMPANION.url);
    qr_print();
    if (open_browser) {
#ifdef __APPLE__
        run_detached((char *[]){"open", url, NULL});
#else
        run_detached((char *[]){"xdg-open", url, NULL});
#endif
    }
}

static struct {
    const char *name, *address, *profile;
    double timeout, power;
    int port;
    bool panel, scan;
} ARGS = {.timeout = 10, .port = 8050};

#define MAX_DEVICES 256
static ble_device DEVICES[MAX_DEVICES];

static void print_devices(int count) {
    for (int i = 0; i < count; i++)
        printf("  %s  %4d dBm  %s%s\n", DEVICES[i].address, DEVICES[i].rssi, *DEVICES[i].name ? DEVICES[i].name : "(no name)",
               DEVICES[i].ftms ? "  [fitness machine]" : "");
}

static bool contains_lower(const char *text, const char *fragment) { /* the fragment anywhere in the text, ignoring case */
    for (size_t length = strlen(fragment); *text; text++)
        if (!strncasecmp(text, fragment, length)) return true;
    return false;
}

static const ble_device *find_ergometer(void) {
    set_status("Scanning for %.0f s ...", ARGS.timeout);
    int count = ble_scan(ARGS.timeout, DEVICES, MAX_DEVICES);
    for (int i = 0; i < count; i++) {
        const ble_device *device = &DEVICES[i];
        if (ARGS.address) {
            if (!strcasecmp(device->address, ARGS.address)) return device;
            continue;
        }
        if (ARGS.name && contains_lower(device->name, ARGS.name)) return device;
        for (size_t hint = 0; !ARGS.name && hint < sizeof NAME_HINTS / sizeof *NAME_HINTS; hint++)
            if (contains_lower(device->name, NAME_HINTS[hint])) return device;
        /* Without an explicit name, any fitness machine is a good candidate. */
        if (!ARGS.name && device->ftms) return device;
    }
    set_status("Ergometer not found");
    if (count < 0) return NULL;
    printf("Nearby devices:\n");
    print_devices(count);
    printf("\nWake the ergometer (pedal a few turns), make sure no phone/app is connected to it,\n"
           "then retry or pick one with --name or --address.\n");
    return NULL;
}

static void print_raw(const char *uuid, const uint8_t *data, size_t length) {
    printf("%s  %.4s raw:", stamp(), uuid + 4);
    for (size_t i = 0; i < length; i++) printf(" %02x", data[i]);
    printf("\n");
}

/* A notification or indication from the ergometer; runs on the Bluetooth thread. */
static void on_data(const char *uuid, const uint8_t *data, size_t length) {
    if (!strcmp(uuid, CONTROL_POINT) && !RAW) {
        /* Response: 0x80, request opcode, result (1 = success). */
        if (length >= 3 && data[2] != 1) printf("%s  ergometer rejected command 0x%02x (result %d)\n", stamp(), data[1], data[2]);
        return;
    }
    struct sample sample = {0};
    if (!strcmp(uuid, INDOOR_BIKE_DATA) && parse_indoor_bike_data(data, length, &sample)) {
        lock();
        sample.t = now_ms();
        sample.target = CONTROL.target_power;
        if (now_ms() / 1000 - ROOM.t < 120) sample.has_room = true, sample.room = ROOM.temperature; /* goes into the log with the ride */
        if (SAMPLE_COUNT == SAMPLE_CAPACITY) {
            SAMPLE_CAPACITY = SAMPLE_CAPACITY ? SAMPLE_CAPACITY * 2 : 4096;
            SAMPLES = realloc(SAMPLES, SAMPLE_CAPACITY * sizeof *SAMPLES);
            if (!SAMPLES) abort();
        }
        SAMPLES[SAMPLE_COUNT++] = sample;
        unlock();
        printf("%s", stamp());
        for (int i = 0; i < FIELDS; i++)
            if (sample.present & 1u << i) print_value(&BIKE_FIELDS[i], sample.value[i]);
        printf("\n");
    } else if (!strcmp(uuid, HEART_RATE) && length >= (data[0] & 1 ? 3u : 2u)) {
        printf("%s", stamp());
        print_value(&BIKE_FIELDS[HEART], data[0] & 1 ? data[1] | data[2] << 8 : data[1]);
        printf("\n");
    } else if (RAW) {
        print_raw(uuid, data, length); /* unknown or unparseable characteristic: show the raw bytes */
    }
}

static pthread_mutex_t END_LOCK = PTHREAD_MUTEX_INITIALIZER;
static pthread_cond_t END = PTHREAD_COND_INITIALIZER;
static bool DISCONNECTED;

static void on_disconnect(void) {
    pthread_mutex_lock(&END_LOCK);
    DISCONNECTED = true;
    pthread_cond_signal(&END);
    pthread_mutex_unlock(&END_LOCK);
}

/* low, high and step of a "Supported ... Range" characteristic (sint16, sint16, uint16). */
static bool read_range(const char *uuid, double range[3]) {
    uint8_t data[6];
    if (ble_read(uuid, data, sizeof data) != 6) return false;
    range[0] = (int16_t)(data[0] | data[1] << 8);
    range[1] = (int16_t)(data[2] | data[3] << 8);
    range[2] = data[4] | data[5] << 8;
    return true;
}

static void run(void) {
    if (ARGS.scan) {
        printf("Scanning for %.0f s ...\n", ARGS.timeout);
        print_devices(ble_scan(ARGS.timeout, DEVICES, MAX_DEVICES));
        return;
    }

    /* macOS: keep the display awake (no screen saver, no sleep) for as long as this program runs. */
    char pid[16];
    snprintf(pid, sizeof pid, "%d", getpid());
    if (access("/usr/bin/caffeinate", X_OK) == 0) run_detached((char *[]){"/usr/bin/caffeinate", "-d", "-i", "-w", pid, NULL});
    CONTROL.target_power = ARGS.power;
    if (ARGS.profile) {
        char *text = read_file(ARGS.profile);
        const char *name = strrchr(ARGS.profile, '/');
        if (!text || !load_profile(name ? name + 1 : ARGS.profile, text)) {
            fprintf(stderr, "%s: %s\n", ARGS.profile, text ? "no rows of the form mm:ss,watt found" : strerror(errno));
            exit(1);
        }
        free(text);
    }
    if (ARGS.panel) start_panel(ARGS.port, true);
    const ble_device *device = find_ergometer();
    if (!device) return;

    char error[256];
    set_status("Connecting to %s [%s] ...", *device->name ? device->name : "(no name)", device->address);
    if (!ble_connect(device->address, on_disconnect, error, sizeof error)) {
        set_status("Could not connect: %s", error);
        return;
    }
    set_status("Connected to %s", *device->name ? device->name : device->address);

    static ble_characteristic characteristics[128];
    int count = ble_characteristics(characteristics, 128);
    bool has_bike_data = false, has_control = false, has_range = false, has_power_range = false;
    printf("Services:\n");
    for (int i = 0; i < count; i++) {
        const ble_characteristic *c = &characteristics[i];
        if (!i || strcmp(c->service, characteristics[i - 1].service)) printf("  %s\n", c->service);
        char properties[64] = "";
        static const char *names[] = {"read", "write", "notify", "indicate", "write-without-response"};
        for (int p = 0; p < 5; p++)
            if (c->properties & 1u << p) snprintf(properties + strlen(properties), sizeof properties - strlen(properties), "%s%s", *properties ? "," : "", names[p]);
        printf("    %s  %s\n", c->uuid, properties);
        has_bike_data |= !strcmp(c->uuid, INDOOR_BIKE_DATA) && c->properties & (BLE_NOTIFY | BLE_INDICATE);
        has_control |= !strcmp(c->uuid, CONTROL_POINT);
        has_range |= !strcmp(c->uuid, RESISTANCE_RANGE);
        has_power_range |= !strcmp(c->uuid, POWER_RANGE);
    }
    if (!has_bike_data) {
        printf("\nNo standard FTMS Indoor Bike Data characteristic - showing raw data only.\n");
        RAW = true;
    }
    for (int i = 0; i < count; i++) {
        const char *uuid = characteristics[i].uuid;
        if (!(characteristics[i].properties & (BLE_NOTIFY | BLE_INDICATE))) continue;
        if (!RAW && strcmp(uuid, INDOOR_BIKE_DATA) && strcmp(uuid, HEART_RATE) && strcmp(uuid, CONTROL_POINT)) continue;
        if (!ble_subscribe(uuid, on_data)) printf("  could not subscribe to %s\n", uuid);
    }

    double range[3];
    if (has_control && has_range && read_range(RESISTANCE_RANGE, range) &&
        ble_write(CONTROL_POINT, (uint8_t[]){0x00}, 1)) { /* request control */
        lock();
        CONTROL.range[0] = range[0] / 10, CONTROL.range[1] = range[1] / 10, CONTROL.range[2] = (range[2] ? range[2] : 10) / 10;
        CONTROL.connected = true;
        /* The ergometer's own power limit is not ours: the PID sets levels. */
        if (has_power_range && read_range(POWER_RANGE, range)) CONTROL.power_range[2] = fmax(range[2], 5);
        if (ARGS.profile) start_profile(); /* a profile from the command line starts on connect */
        unlock();
        pthread_t thread;
        pthread_create(&thread, NULL, power_control, NULL);
    } else if (has_control && has_range) {
        printf("The ergometer did not hand over control; the resistance cannot be set.\n");
    }

    printf("\nReceiving data, start pedalling. Press Ctrl+C to stop.\n\n");
    pthread_mutex_lock(&END_LOCK);
    while (!DISCONNECTED) pthread_cond_wait(&END, &END_LOCK);
    pthread_mutex_unlock(&END_LOCK);
    lock();
    CONTROL.connected = false;
    unlock();
    save_log();
    set_status("Ergometer disconnected");
}

/* Ctrl+C: the handler only wakes this thread, which may do what a handler may not. */
static int INTERRUPT[2];

static void on_interrupt(int signal) {
    (void)signal;
    (void)!write(INTERRUPT[1], "", 1);
}

static void *wait_for_interrupt(void *unused) {
    (void)unused;
    char byte;
    while (read(INTERRUPT[0], &byte, 1) != 1) {}
    printf("\nStopped.\n");
    save_log(); /* a recording that is still running is kept */
    ble_disconnect();
    exit(0);
}

static void usage(const char *program) {
    printf("usage: %s [--name NAME] [--address ADDRESS] [--timeout S] [--panel] [--port PORT]\n"
           "       %*s [--power W] [--profile FILE] [--raw] [--scan]\n\n"
           "Show live values from a Christopeit AX 4000 ergometer.\n\n"
           "  --name NAME        name fragment to look for (default: FS-... / Christopeit / any fitness machine)\n"
           "  --address ADDRESS  connect to this device address (a UUID on macOS)\n"
           "  --timeout S        scan time in seconds (default: 10)\n"
           "  --panel            show a live Plotly panel in the browser\n"
           "  --port PORT        port for the panel (default: 8050)\n"
           "  --power W          target power in W, held by a PID controller on the resistance\n"
           "  --profile FILE     CSV file with rows mm:ss,watt; the target power follows it once connected\n"
           "  --raw              also print raw bytes of all other notifications\n"
           "  --scan             only list nearby BLE devices\n",
           program, (int)strlen(program), "");
}

int main(int argc, char **argv) {
    static const struct option options[] = {
        {"name", required_argument, NULL, 'n'}, {"address", required_argument, NULL, 'a'}, {"timeout", required_argument, NULL, 't'},
        {"panel", no_argument, NULL, 'P'}, {"port", required_argument, NULL, 'p'}, {"power", required_argument, NULL, 'w'},
        {"profile", required_argument, NULL, 'f'}, {"raw", no_argument, NULL, 'r'}, {"scan", no_argument, NULL, 's'},
        {"help", no_argument, NULL, 'h'}, {0}};
    for (int option; (option = getopt_long(argc, argv, "h", options, NULL)) != -1;) {
        switch (option) {
        case 'n': ARGS.name = optarg; break;
        case 'a': ARGS.address = optarg; break;
        case 't': ARGS.timeout = atof(optarg); break;
        case 'P': ARGS.panel = true; break;
        case 'p': ARGS.port = atoi(optarg); break;
        case 'w': ARGS.power = atof(optarg); break;
        case 'f': ARGS.profile = optarg; break;
        case 'r': RAW = true; break;
        case 's': ARGS.scan = true; break;
        case 'h': usage(argv[0]); return 0;
        default: usage(argv[0]); return 2;
        }
    }

    char executable[1024] = "", resolved[1024];
#ifdef __APPLE__
    uint32_t size = sizeof executable;
    if (_NSGetExecutablePath(executable, &size)) executable[0] = 0;
#else
    ssize_t size = readlink("/proc/self/exe", executable, sizeof executable - 1);
    executable[size > 0 ? size : 0] = 0;
#endif
    if (*executable && realpath(executable, resolved) && strrchr(resolved, '/')) {
        *strrchr(resolved, '/') = 0;
        snprintf(PROGRAM_DIR, sizeof PROGRAM_DIR, "%s", *resolved ? resolved : "/");
    }

    pthread_mutexattr_t recursive;
    pthread_mutexattr_init(&recursive);
    pthread_mutexattr_settype(&recursive, PTHREAD_MUTEX_RECURSIVE);
    pthread_mutex_init(&LOCK, &recursive);
    setvbuf(stdout, NULL, _IOLBF, 0);
    signal(SIGPIPE, SIG_IGN); /* a browser that hangs up must not end the program */
    pthread_t thread;
    if (pipe(INTERRUPT) == 0 && pthread_create(&thread, NULL, wait_for_interrupt, NULL) == 0) signal(SIGINT, on_interrupt);
    read_env();

    run();
    save_log(); /* a recording that is still running is kept */
    return 0;
}
