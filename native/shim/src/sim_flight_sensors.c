/* FC sensor bus (BMP390 barometer, BMI088 gyroscope and accelerometer on one SPI bus with three chip selects and
 * three data-ready lines): port of the Renode bay's SedsFlightSensorBus at rest (no flight profile): identity and
 * calibration registers, conversion timers from the configured output data rates, data-ready pulses (barometer
 * level/latch per INT_CTRL, gyro/accel until the sample is read), the BMP390/BMI088-ACC dummy byte on reads, and a
 * resting 1 g on accelerometer Z. Configured by "flightsensors <bus> <cs-baro> <cs-gyro> <cs-accel> <irq-baro>
 * <irq-gyro> <irq-accel>" (pins: port*16+pin, from the board layout's connections). */
#include "sim.h"
#include "main.h"
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#ifdef HAL_SPI_MODULE_ENABLED
int sim_pin_input(unsigned global_index);
void sim_pin_set_input(unsigned global_index, int value);
void sim_gpio_edge(uint16_t pin, int rising);
enum { BARO, GYRO, ACCEL };
static struct {
    int present; char bus[8]; int cs[3], irq[3];
    uint8_t regs[3][256];
    int dev, selected, position, reading, sample_read; uint8_t address;
    int baro_ready;
} f;

static void set_line(int sensor, int level)
{
    int idx = f.irq[sensor], was = sim_pin_input((unsigned)idx);
    if (was == level) return;
    sim_pin_set_input((unsigned)idx, level);
    sim_gpio_edge((uint16_t)(1u << (idx & 15)), level);
}
static void baro_irq(int asserted) { int high = (f.regs[BARO][0x19] & 2) != 0; set_line(BARO, asserted ? high : !high); }
static void conversion_isr(void *a, uintptr_t sensor);
static void baro_pulse_isr(void *a, uintptr_t b) { (void)a; (void)b; baro_irq(0); }
static void update_timer(int s)
{
    uint64_t ticks = 0; int on = 0;
    if (s == BARO) { int odr = f.regs[BARO][0x1d]; ticks = 16000ull << (odr > 17 ? 17 : odr); on = (f.regs[BARO][0x1b] & 0x30) == 0x30 && (f.regs[BARO][0x19] & 0x40); }
    else if (s == GYRO) { static const unsigned rates[8] = {2000, 2000, 1000, 400, 200, 100, 200, 100}; ticks = 3200000ull / rates[f.regs[GYRO][0x10] & 7]; on = (f.regs[GYRO][0x15] & 0x80) != 0; }
    else { int odr = f.regs[ACCEL][0x40] & 15, sh = odr - 5; sh = sh < 0 ? 0 : sh > 7 ? 7 : sh; ticks = 256000ull >> sh; on = (f.regs[ACCEL][0x58] & 4) && (f.regs[ACCEL][0x7d] & 4); }
    sim_cancel(conversion_isr, &f.regs[s]);
    if (on) sim_schedule(sim_now_ns() + ticks * 3125 / 10, -1, conversion_isr, &f.regs[s], (uintptr_t)s);
}
static void conversion_isr(void *a, uintptr_t sensor)
{
    (void)a;
    int s = (int)sensor;
    if (s == BARO) {
        f.baro_ready = 1; baro_irq(1);
        sim_cancel(baro_pulse_isr, &f);
        if ((f.regs[BARO][0x19] & 4) == 0) sim_schedule(sim_now_ns() + 2500000, -1, baro_pulse_isr, &f, 0);
    } else set_line(s, 1);
    update_timer(s);
}
static double accel_lsb(void) { int range = f.regs[ACCEL][0x41] & 3; if (!f.regs[ACCEL][0x41]) range = 1; return 1.5 * (1 << (range + 1)) / 32768.0 * 9.80665; }
static uint8_t read_reg(int d, uint8_t r)
{
    if (r == 0) return d == BARO ? 0x60 : d == GYRO ? 0x0f : 0x1e;
    if (d == BARO && r == 0x03) return 0x60;
    if (d == BARO && r == 0x11) { uint8_t st = f.baro_ready ? 8 : 0; f.baro_ready = 0; sim_cancel(baro_pulse_isr, &f); baro_irq(0); return st; }
    if (d == BARO && r >= 0x04 && r <= 0x09) { static const uint8_t v[6] = {0x00, 0x80, 0x65, 0x00, 0x6a, 0x31}; return v[r - 4]; }
    if (d == ACCEL && r >= 0x12 && r <= 0x17) { int16_t z = (int16_t)(9.80665 / accel_lsb() + 0.5); uint8_t v[6] = {0, 0, 0, 0, (uint8_t)z, (uint8_t)(z >> 8)}; return v[r - 0x12]; }
    if (d == GYRO && r >= 0x02 && r <= 0x07) return 0;
    return f.regs[d][r];
}
static uint8_t transmit(uint8_t v)
{
    if (!f.selected) return 0xFF;
    if (f.position == 0) {
        f.address = v & 0x7f; f.reading = (v & 0x80) != 0;
        f.sample_read = f.reading && ((f.dev == BARO && f.address == 4) || (f.dev == GYRO && f.address == 2) || (f.dev == ACCEL && f.address == 0x12));
        f.position++; return 0;
    }
    if (!f.reading) { f.regs[f.dev][f.address] = v; update_timer(f.dev); f.address++; f.position++; return 0; }
    int dummy = f.dev == GYRO ? 0 : 1;
    if (f.position++ <= dummy) return 0;
    return read_reg(f.dev, f.address++);
}
int sim_flight_sensors_xfer(const char *bus, const uint8_t *tx, uint8_t *rx, uint16_t n)
{
    if (!f.present || strcmp(bus, f.bus)) return 0;
    for (uint16_t i = 0; i < n; i++) { uint8_t r = transmit(tx ? tx[i] : 0); if (rx) rx[i] = r; }
    return 1;
}
void sim_flight_sensors_cs(unsigned idx, int value)
{
    if (!f.present) return;
    for (int s = 0; s < 3; s++) {
        if ((int)idx != f.cs[s]) continue;
        if (!value) { f.dev = s; f.selected = 1; f.position = 0; f.sample_read = 0; }
        else if (f.selected && f.dev == s) {
            if (f.sample_read && s != BARO) set_line(s, 0);
            f.selected = 0; f.position = 0;
        }
    }
}
__attribute__((constructor(200))) static void fs_load(void)
{
    const char *path = getenv("SIM_DEVICES");
    FILE *fp = path ? fopen(path, "r") : NULL;
    if (!fp) return;
    char line[512];
    while (fgets(line, sizeof line, fp)) {
        char kind[32], bus[16]; int c0, c1, c2, i0, i1, i2;
        if (sscanf(line, "%31s %15s %d %d %d %d %d %d", kind, bus, &c0, &c1, &c2, &i0, &i1, &i2) == 8 && !strcmp(kind, "flightsensors")) {
            f.present = 1; snprintf(f.bus, sizeof f.bus, "%s", bus);
            f.cs[0] = c0; f.cs[1] = c1; f.cs[2] = c2; f.irq[0] = i0; f.irq[1] = i1; f.irq[2] = i2;
        }
    }
    fclose(fp);
    if (!f.present) return;
    f.regs[BARO][0x19] = 0x02;
    f.regs[BARO][0x31] = 0xa8; f.regs[BARO][0x32] = 0x61; f.regs[BARO][0x34] = 0x40; f.regs[BARO][0x37] = 0x40;
    f.regs[BARO][0x39] = 0x40; f.regs[BARO][0x3c] = 0xd4; f.regs[BARO][0x3d] = 0x30;
    for (int s = 0; s < 3; s++) sim_pin_set_input((unsigned)f.irq[s], 0);
}
#else
int sim_flight_sensors_xfer(const char *bus, const unsigned char *tx, unsigned char *rx, unsigned short n) { (void)bus; (void)tx; (void)rx; (void)n; return 0; }
void sim_flight_sensors_cs(unsigned idx, int value) { (void)idx; (void)value; }
#endif
