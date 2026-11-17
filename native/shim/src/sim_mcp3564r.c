/* MCP3564R 24-bit delta-sigma ADC on SPI with a data-ready (IRQ) pin: port of the Renode bay's SedsMcp3564r
 * (renode/peripherals/SedsSpiSensors.cs) at the HAL boundary. Fast commands, register map with multi-register
 * auto-increment, SCAN sequencing (MSb first), conversion timing from OSR/prescaler/AZ_MUX at a 4 MHz DMCLK, the
 * DR pin falling at each conversion and released before the next, ADCDATA formats 0..3 and the STATUS byte.
 * Channel codes come from the device's samples (SIM_DEVICES "mcp3564r", live updates through the load-cell
 * stimulus file or scripted signals); without samples the Renode fixture codes and drift apply.
 * Configured by "mcp3564r <bus> <cs-pin> <irq-pin> <codes>" (pins: port*16+pin, as the board layout). */
#include "sim.h"
#include "main.h"
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#ifdef HAL_SPI_MODULE_ENABLED
int sim_pin_input(unsigned global_index);
void sim_pin_set_input(unsigned global_index, int value);
void sim_gpio_edge(uint16_t pin, int rising);
int sim_adc_count(const char *device);

static struct {
    int present; char bus[8]; int cs, irq;
    uint8_t regs[64]; uint32_t samples[16];
    int running, data_ready, channel, latched_channel; int32_t latched_code;
    int position, reading, reg_addr, reg_byte, data_bytes; uint32_t data;
    int selected; uint64_t transactions;
} m;

static uint8_t reg(int a) { return m.regs[(a << 2) & 63]; }
static int reg_width(int a) { return a == 0 ? 4 : (a >= 7 && a <= 10) ? 3 : a == 15 ? 2 : 1; }
static uint16_t scan_mask(void) { return (uint16_t)((m.regs[(7 << 2) + 1] << 8) | m.regs[(7 << 2) + 2]); }

static void set_irq(int level)
{
    int was = sim_pin_input((unsigned)m.irq);
    sim_pin_set_input((unsigned)m.irq, level);
    if (was == 1 && level == 0) sim_gpio_edge((uint16_t)(1u << (m.irq & 15)), 0);
}
static void select_channel(void)
{
    uint16_t mask = scan_mask();
    if (!mask) { m.channel = (reg(6) >> 4) & 7; return; }
    for (int i = 0; i < 16; i++) { if (mask & (1u << m.channel)) return; m.channel = (m.channel + 15) & 15; }
}
/* Channel codes: 24-bit two's complement (0..0xFFFFFF, as the Renode fixtures), or 0x1000000 + c for a positive
 * overrange code c beyond full scale (the 25-bit range the 32-bit formats carry, e.g. 0x1B40000 = +0xB40000). */
static int32_t sample(int ch)
{
    uint32_t v = sim_adc_count("mcp3564r") >= 16 ? (uint32_t)sim_adc_sample("mcp3564r", (unsigned)ch, 0) : m.samples[ch];
    if (v >= 0x1000000u) return (int32_t)(v - 0x1000000u);
    return (v & 0x00800000u) ? (int32_t)(v | 0xFF000000u) : (int32_t)v;
}
static void convert_sample(void) { select_channel(); m.latched_channel = m.channel; m.latched_code = sample(m.channel); }
static void advance_channel(void)
{
    if (sim_adc_count("mcp3564r") < 16 && m.channel < 12) m.samples[m.channel] = (m.samples[m.channel] + 17u) & 0x00FFFFFFu;
    m.channel = (m.channel + 15) & 15;
}

static const uint32_t osr_table[16] = {32, 64, 128, 256, 512, 1024, 2048, 4096, 8192, 16384, 20480, 24576, 40960, 49152, 81920, 98304};
static void conversion_isr(void *a, uintptr_t b);
static void release_isr(void *a, uintptr_t b) { (void)a; (void)b; set_irq(1); }
static void arm(void)
{
    uint8_t c1 = reg(2);
    uint64_t osr = osr_table[(c1 >> 2) & 15], clocks = osr <= 512 ? 3 * osr : osr + 1024;
    uint64_t az = (reg(3) & 4) ? 2 : 1, pre = 1ull << (c1 >> 6);
    uint64_t us = (clocks * az * pre + 3) / 4; if (!us) us = 1;
    uint64_t rel = us - (us - 1 < 4 * pre ? us - 1 : 4 * pre);
    sim_cancel(conversion_isr, &m); sim_cancel(release_isr, &m);
    sim_schedule(sim_now_ns() + us * 1000u, -1, conversion_isr, &m, 0);
    sim_schedule(sim_now_ns() + rel * 1000u, -1, release_isr, &m, 0);
}
static void conversion_isr(void *a, uintptr_t b)
{
    (void)a; (void)b;
    if (!m.running) return;
    convert_sample(); advance_channel();
    m.data_ready = 1;
    set_irq(0);
    arm();
}
static void start(void) { m.channel = 15; m.data_ready = 0; set_irq(1); m.running = 1; arm(); }
static void stop(void) { m.running = 0; m.data_ready = 0; set_irq(1); sim_cancel(conversion_isr, &m); sim_cancel(release_isr, &m); }
static void reset_regs(void)
{
    stop();
    memset(m.regs, 0, sizeof m.regs);
    for (int i = 0; i < 16; i++) m.samples[i] = ((uint32_t)(i + 1) * 0x100000u) & 0x00FFFFFFu;
    for (int i = 8; i < 12; i++) m.samples[i] = (m.samples[2 * (i - 8)] - m.samples[2 * (i - 8) + 1]) & 0x00FFFFFFu;
    m.samples[12] = 305652u;
    m.channel = 15;
}

static uint8_t transmit(uint8_t v)
{
    if (!m.selected) return 0xFF;
    if (m.position == 0) {
        m.transactions++;
        m.reg_addr = (v >> 2) & 0x0F; m.reg_byte = 0; m.reading = (v & 3) == 1;
        int ready_before = m.data_ready;
        if (m.reading && m.reg_addr == 0) {
            m.data_ready = 0; set_irq(1);
            int fmt = (reg(4) >> 4) & 3;
            m.data_bytes = fmt == 0 ? 3 : 4;
            int32_t c = m.latched_code;
            int32_t clipped = c > 0x7FFFFF ? 0x7FFFFF : c < -0x800000 ? -0x800000 : c;  /* 24-bit formats saturate */
            uint32_t code = (uint32_t)clipped & 0x00FFFFFFu, s = (uint32_t)c;
            m.data = fmt == 0 ? code : fmt == 1 ? code << 8 : fmt == 2 ? (uint32_t)clipped : (((scan_mask() ? (uint32_t)m.latched_channel : 0u) << 28) | (s & 0x0FFFFFFFu));
        }
        uint8_t status = (uint8_t)(0x13 | (ready_before ? 0 : 4));
        if (v == 0x78) reset_regs();
        if (v == 0x68) start();
        m.position++;
        return status;
    }
    if (m.reading && m.reg_addr == 0) {
        int idx = m.position - 1 < m.data_bytes - 1 ? m.position - 1 : m.data_bytes - 1;
        uint8_t r = (uint8_t)(m.data >> (8 * (m.data_bytes - 1 - idx)));
        m.position++;
        return r;
    }
    int key = ((m.reg_addr << 2) + m.reg_byte) & 63;
    uint8_t out = 0;
    if (m.reading) out = m.regs[key];
    else {
        m.regs[key] = v;
        if (m.reg_addr == 1 && (v & 3) != 3) stop();
        else if (m.reg_addr == 1 && (v & 3) == 3) start();
    }
    if (++m.reg_byte == reg_width(m.reg_addr)) { m.reg_byte = 0; m.reg_addr = (m.reg_addr + 1) & 0x0F; }
    m.position++;
    return out;
}

void sim_spi_xfer(const char *bus, const uint8_t *tx, uint8_t *rx, uint16_t n)
{
    if (!m.present || strcmp(bus, m.bus)) { if (rx) memset(rx, 0xFF, n); return; }
    for (uint16_t i = 0; i < n; i++) { uint8_t r = transmit(tx ? tx[i] : 0); if (rx) rx[i] = r; }
}

/* chip select from the board's GPIO writes (hal_core.c) */
void sim_gpio_changed(unsigned global_index, int value)
{
    if (!m.present || (int)global_index != m.cs) return;
    m.selected = !value;
    if (value) { m.position = 0; m.reading = 0; }
}

__attribute__((constructor(200))) static void mcp_load(void)
{
    const char *path = getenv("SIM_DEVICES");
    FILE *f = path ? fopen(path, "r") : NULL;
    if (!f) return;
    char line[2048];
    while (fgets(line, sizeof line, f)) {
        char kind[32], bus[16]; int cs, irq;
        if (sscanf(line, "%31s %15s %d %d", kind, bus, &cs, &irq) == 4 && !strcmp(kind, "mcp3564r")) {
            m.present = 1; snprintf(m.bus, sizeof m.bus, "%s", bus); m.cs = cs; m.irq = irq; m.selected = 0;
        }
    }
    fclose(f);
    if (m.present) { reset_regs(); sim_pin_set_input((unsigned)m.irq, 1); }
}
#endif
