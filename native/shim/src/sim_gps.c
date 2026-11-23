/* u-blox NEO-M9N on SPI (RF board, NMEA over SPI as RFBoard26 neom9n.c reads it): idle 0xFF, a GGA + RMC pair once
 * per second of virtual time with UTC from the host clock (the station-managed bay runs at real time), and an
 * UBX ACK-ACK for every UBX frame written (CFG-VALSET from neom9n_config.c). Configured by "gps <bus>" in
 * SIM_DEVICES; the position comes from the plant (SIM_MSG_ADC device "gps": "lat,lon,alt_msl,speed_mps,course_deg,
 * fix,sats"); before that the module reports no fix. */
#include "sim.h"
#include "main.h"
#include <math.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>

static struct {
    int present; char bus[8];
    double lat, lon, alt, speed, course; int fix, sats;
    char out[512]; int out_len, out_pos;
    uint64_t next_ns;
    uint8_t ubx[8]; int ubx_pos; unsigned ubx_len;  /* sync, sync, class, id, len lo, len hi */
    unsigned ubx_left;
} g;

void sim_gps_set(const char *csv)
{
    double v[7] = {0}; int n = 0; const char *p = csv;
    while (n < 7 && p && *p) { char *end; v[n] = strtod(p, &end); if (end == p) break; n++; p = *end == ',' ? end + 1 : NULL; }
    if (n < 6) return;
    g.lat = v[0]; g.lon = v[1]; g.alt = v[2]; g.speed = v[3]; g.course = v[4]; g.fix = v[5] != 0; g.sats = n > 6 ? (int)v[6] : 12;
}

static void push(const char *s) { int l = (int)strlen(s); if (g.out_len + l < (int)sizeof g.out) { memcpy(g.out + g.out_len, s, (size_t)l); g.out_len += l; } }
static void nmea(const char *body)
{
    unsigned char cs = 0; for (const char *c = body; *c; c++) cs ^= (unsigned char)*c;
    char line[160]; snprintf(line, sizeof line, "$%s*%02X\r\n", body, cs); push(line);
}
static void coord(char *buf, size_t n, double deg, int lat)
{
    double a = fabs(deg); int d = (int)a; double m = (a - d) * 60.0;
    if (lat) snprintf(buf, n, "%02d%08.5f,%c", d, m, deg < 0 ? 'S' : 'N');
    else snprintf(buf, n, "%03d%08.5f,%c", d, m, deg < 0 ? 'W' : 'E');
}
static void sentences(void)
{
    struct timespec ts; clock_gettime(CLOCK_REALTIME, &ts);
    time_t secs = ts.tv_sec; struct tm u; gmtime_r(&secs, &u);
    char hms[16], dmy[8], la[24], lo[24], body[140];
    snprintf(hms, sizeof hms, "%02d%02d%02d.%02ld", u.tm_hour, u.tm_min, u.tm_sec, ts.tv_nsec / 10000000L);
    snprintf(dmy, sizeof dmy, "%02d%02d%02d", u.tm_mday, u.tm_mon + 1, u.tm_year % 100);
    if (g.fix) {
        coord(la, sizeof la, g.lat, 1); coord(lo, sizeof lo, g.lon, 0);
        snprintf(body, sizeof body, "GPGGA,%s,%s,%s,1,%02d,0.8,%.1f,M,-25.0,M,,", hms, la, lo, g.sats, g.alt); nmea(body);
        snprintf(body, sizeof body, "GPRMC,%s,A,%s,%s,%.2f,%.1f,%s,,,A", hms, la, lo, g.speed * 1.943844, g.course, dmy); nmea(body);
    } else {
        snprintf(body, sizeof body, "GPGGA,%s,,,,,0,00,99.99,,,,,,", hms); nmea(body);
        snprintf(body, sizeof body, "GPRMC,%s,V,,,,,,,%s,,,N", hms, dmy); nmea(body);
    }
}
static void ubx_ack(uint8_t cls, uint8_t id)
{
    uint8_t f[10] = {0xB5, 0x62, 0x05, 0x01, 0x02, 0x00, cls, id, 0, 0};
    uint8_t a = 0, b = 0; for (int i = 2; i < 8; i++) { a = (uint8_t)(a + f[i]); b = (uint8_t)(b + a); }
    f[8] = a; f[9] = b;
    if (g.out_len + 10 < (int)sizeof g.out) { memcpy(g.out + g.out_len, f, 10); g.out_len += 10; }
}
static void mosi(uint8_t v)
{
    if (g.ubx_left) { if (--g.ubx_left == 0) ubx_ack(g.ubx[2], g.ubx[3]); return; }
    if (g.ubx_pos == 0 && v != 0xB5) return;
    if (g.ubx_pos == 1 && v != 0x62) { g.ubx_pos = 0; return; }
    g.ubx[g.ubx_pos++] = v;
    if (g.ubx_pos == 6) { g.ubx_left = (unsigned)(g.ubx[4] | (g.ubx[5] << 8)) + 2u; g.ubx_pos = 0; }  /* payload + checksum */
}
int sim_gps_xfer(const char *bus, const uint8_t *tx, uint8_t *rx, uint16_t n)
{
    if (!g.present || strcmp(bus, g.bus)) return 0;
    uint64_t now = sim_now_ns();
    if (g.out_pos >= g.out_len) { g.out_pos = g.out_len = 0; }
    if (now >= g.next_ns && g.out_len == 0) { sentences(); g.next_ns = now + 1000000000ull; }
    for (uint16_t i = 0; i < n; i++) {
        mosi(tx ? tx[i] : 0xFF);
        uint8_t r = 0xFF;
        if (g.out_pos < g.out_len) r = (uint8_t)g.out[g.out_pos++];
        if (rx) rx[i] = r;
    }
    return 1;
}
__attribute__((constructor(201))) static void gps_load(void)
{
    const char *path = getenv("SIM_DEVICES");
    FILE *fp = path ? fopen(path, "r") : NULL;
    if (!fp) return;
    char line[256], kind[32], bus[16];
    while (fgets(line, sizeof line, fp))
        if (sscanf(line, "%31s %15s", kind, bus) == 2 && !strcmp(kind, "gps")) { g.present = 1; snprintf(g.bus, sizeof g.bus, "%s", bus); }
    fclose(fp);
}
