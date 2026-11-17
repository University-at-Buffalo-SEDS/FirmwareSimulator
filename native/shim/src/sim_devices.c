/* Board-level device models shared by the HAL shims: ADC channel samples (with injectable signals/faults),
 * LTC2990 power monitors on I2C, static input pins. Configured from SIM_DEVICES (one device per line,
 * generated from the board's sim/board.json by the native engine):
 *   adc <bus> <bits> <noise_lsb> <v0,v1,...>
 *   ltc2990 <bus> <addr> <c0,c1,c2,c3> <failure_every>
 *   pin <global-index> <0|1>
 *   mcp3564r <bus> <v0,...>
 * Live updates: SIM_MSG_ADC {device[16], "v0,v1,..."} (stimulus files, fault scripts). */
#include "sim.h"
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#define MAX_DEV 16
typedef struct { char name[16]; int bits, noise, n; int32_t v[32]; int fault; } adc_t;
static adc_t adcs[MAX_DEV]; static int nadc;
typedef struct { char bus[8]; int addr; uint16_t code[4]; unsigned failure_every; uint8_t regs[256]; uint8_t ptr; int expect_reg; unsigned long accesses; } ltc_t;
static ltc_t ltcs[4]; static int nltc;
static int8_t pins[256];

static adc_t *adc_find(const char *name, int create)
{
    for (int i = 0; i < nadc; i++) if (!strcmp(adcs[i].name, name)) return &adcs[i];
    if (!create || nadc >= MAX_DEV) return NULL;
    adc_t *a = &adcs[nadc++]; memset(a, 0, sizeof *a); snprintf(a->name, sizeof a->name, "%s", name); a->bits = 12; return a;
}
static int parse_csv(const char *csv, int32_t *out, int max)
{
    int n = 0;
    while (csv && *csv && n < max) {
        char *end; long v = strtol(csv, &end, 0);
        if (end == csv) break;
        out[n++] = (int32_t)v;
        csv = *end == ',' ? end + 1 : end;
        if (*end != ',') break;
    }
    return n;
}
bool sim_adc_has(const char *device) { return adc_find(device, 0) != NULL; }
void sim_adc_set_samples(const char *device, const char *csv)
{
    adc_t *a = adc_find(device, 1);
    if (!a) return;
    if (!strncmp(csv, "fault=", 6)) { a->fault = atoi(csv + 6); return; }
    int32_t v[32]; int n = parse_csv(csv, v, 32);
    if (n <= 0) return;
    if (a->n == 0 || n == a->n || n > a->n) a->n = n;
    for (int i = 0; i < n; i++) a->v[i] = v[i];
}
int32_t sim_adc_sample(const char *device, unsigned ch, int32_t fallback)
{
    adc_t *a = adc_find(device, 0);
    if (!a || a->n == 0) return fallback;
    if (a->fault == 1) return 0;                      /* open/shorted input */
    if (a->fault == 2) return (1 << a->bits) - 1;     /* rail */
    int32_t base = a->v[ch < (unsigned)a->n ? ch : 0];
    if (a->noise) {
        int span = a->noise * 2 + 1;
        base += (int)(sim_rand() % (unsigned)span) - a->noise;
        if (base < 0) base = 0;
        if (a->bits < 31 && base > (1 << a->bits) - 1) base = (1 << a->bits) - 1;
    }
    return base;
}
int sim_adc_count(const char *device) { adc_t *a = adc_find(device, 0); return a ? a->n : 0; }

int sim_pin_input(unsigned global_index) { return global_index < 256 ? pins[global_index] : -1; }
void sim_pin_set_input(unsigned global_index, int value) { if (global_index < 256) pins[global_index] = (int8_t)value; }

/* ---------------------------------------------------------------- LTC2990 (Rev. F register map) */
static ltc_t *ltc_find(const char *bus, int addr)
{
    for (int i = 0; i < nltc; i++) if (!strcmp(ltcs[i].bus, bus) && ltcs[i].addr == addr) return &ltcs[i];
    return NULL;
}
static void ltc_convert(ltc_t *l)
{
    int ready = (l->regs[1] & 7) == 6 ? 0x14 : 0x3c;
    l->regs[0] = (uint8_t)(ready | ((l->regs[1] & 0x40) == 0 ? 1 : 0));
    for (int ch = 0; ch < 4; ch++) {
        int reg = 6 + 2 * ch, valid = (ready & (1 << (2 + ch))) != 0;
        uint16_t v = (uint16_t)((valid ? 0x8000 : 0) | l->code[ch]);
        l->regs[reg] = (uint8_t)(v >> 8); l->regs[reg + 1] = (uint8_t)v;
    }
}
/* returns 0 ok, -1 no device (NACK) */
int sim_i2c_write(const char *bus, int addr, const uint8_t *d, unsigned n)
{
    ltc_t *l = ltc_find(bus, addr);
    if (!l) return -1;
    for (unsigned i = 0; i < n; i++) {
        if (l->expect_reg) { l->ptr = d[i]; l->expect_reg = 0; continue; }
        uint8_t reg = l->ptr++;
        l->regs[reg] = d[i];
        if (reg == 0x02) ltc_convert(l);
    }
    l->expect_reg = 1;
    return 0;
}
int sim_i2c_read(const char *bus, int addr, uint8_t *d, unsigned n)
{
    ltc_t *l = ltc_find(bus, addr);
    if (!l) return -1;
    l->expect_reg = 1; l->accesses++;
    if (l->failure_every && l->accesses % l->failure_every == 0) { memset(d, 0, n); return 0; }
    for (unsigned i = 0; i < n; i++) {
        uint8_t reg = l->ptr++;
        d[i] = l->regs[reg];
        if (reg >= 0x06 && reg <= 0x0c && (reg & 1) == 0) { l->regs[0] &= (uint8_t)~(1 << (2 + (reg - 6) / 2)); l->regs[reg] &= 0x7f; }
    }
    return 0;
}
int sim_i2c_present(const char *bus, int addr) { return ltc_find(bus, addr) != NULL; }

static void on_adc(uint16_t type, const uint8_t *b, uint32_t len)
{
    (void)type;
    if (len < 16) return;
    char dev[17]; memcpy(dev, b, 16); dev[16] = 0;
    char csv[1024]; uint32_t n = len - 16 < sizeof csv - 1 ? len - 16 : sizeof csv - 1;
    memcpy(csv, b + 16, n); csv[n] = 0;
    if (!strncmp(dev, "ltc2990:", 8)) {
        for (int i = 0; i < nltc; i++) {
            char key[32]; snprintf(key, sizeof key, "ltc2990:%s@%d", ltcs[i].bus, ltcs[i].addr);
            if (!strcmp(key, dev)) { int32_t v[4]; int k = parse_csv(csv, v, 4); for (int j = 0; j < k; j++) ltcs[i].code[j] = (uint16_t)v[j]; }
        }
        return;
    }
    if (!strncmp(dev, "pin:", 4)) { int idx = atoi(dev + 4); if (idx >= 0 && idx < 256) pins[idx] = (int8_t)atoi(csv); return; }
    sim_adc_set_samples(dev, csv);
}

__attribute__((constructor(150))) static void devices_load(void)
{
    memset(pins, -1, sizeof pins);
    sim_register_rx(SIM_MSG_ADC, on_adc);
    const char *path = getenv("SIM_DEVICES");
    if (!path) return;
    FILE *f = fopen(path, "r");
    if (!f) return;
    char line[2048];
    while (fgets(line, sizeof line, f)) {
        char kind[32] = {0}, a1[64] = {0}, a2[1600] = {0}, a3[1600] = {0}, a4[1600] = {0};
        int k = sscanf(line, "%31s %63s %1599s %1599s %1599s", kind, a1, a2, a3, a4);
        if (k < 2) continue;
        if (!strcmp(kind, "adc") && k >= 5) {
            adc_t *a = adc_find(a1, 1); a->bits = atoi(a2); a->noise = atoi(a3);
            sim_adc_set_samples(a1, a4[0] ? a4 : "");
            /* values may be longer than a4: re-read the rest of the line */
            char *p = strstr(line, a3); p = p ? strchr(p, ' ') : NULL;
            if (p) { while (*p == ' ') p++; char *nl = strchr(p, '\n'); if (nl) *nl = 0; a->n = 0; sim_adc_set_samples(a1, p); }
        } else if (!strcmp(kind, "mcp3564r") && k >= 5) {
            /* mcp3564r <bus> <cs-pin> <irq-pin> <16 codes>: codes are the device's channel samples */
            adc_t *a = adc_find("mcp3564r", 1); a->bits = 24; sim_adc_set_samples("mcp3564r", a4);
        } else if (!strcmp(kind, "ltc2990") && k >= 3 && nltc < 4) {
            ltc_t *l = &ltcs[nltc++]; memset(l, 0, sizeof *l);
            snprintf(l->bus, sizeof l->bus, "%s", a1); l->addr = (int)strtol(a2, NULL, 0); l->expect_reg = 1;
            for (int i = 0; i < 4; i++) l->code[i] = 4096;
            if (k >= 4 && strcmp(a3, "-")) { int32_t v[4]; int n = parse_csv(a3, v, 4); for (int i = 0; i < n; i++) l->code[i] = (uint16_t)v[i]; }
            if (k >= 5) l->failure_every = (unsigned)atoi(a4);
        } else if (!strcmp(kind, "pin") && k >= 3) {
            int idx = atoi(a1); if (idx >= 0 && idx < 256) pins[idx] = (int8_t)atoi(a2);
        }
    }
    fclose(f);
}
