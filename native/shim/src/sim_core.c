/* Native SITL core: virtual time, the simulated interrupt controller and the lockstep link to the coordinator.
 *
 * Time model: every node runs on node-local virtual nanoseconds. Code consumes time only through charged
 * costs (shim calls, busy waits, interrupt re-enable points); idle time is skipped. The coordinator grants a
 * global horizon; the node runs to it, reports what it sent and when it next needs to run, and waits. All
 * interrupts (ticks, peripheral completions, inbound frames) are timed events delivered in time order at
 * interrupt points with interrupts enabled, so a run is a pure function of its inputs and seed. */
#define _GNU_SOURCE
#include "sim.h"
#include <errno.h>
#include <fcntl.h>
#include <signal.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/prctl.h>
#include <sys/socket.h>
#include <time.h>
#include <unistd.h>

volatile uint32_t sim_primask, sim_basepri, sim_faultmask, sim_ipsr, sim_control;
volatile unsigned int sim_tx_trace_time;
uint32_t sim_cycles_per_us = 170;

/* ThreadX port hooks (sim_threadx_port.c) */
void sim_port_tick(void);
void sim_port_maybe_preempt(void);
uint64_t sim_port_next_timer_ticks(void);
bool sim_port_thread_ready(void);
bool sim_port_in_thread(void);

static uint64_t now_ns, grant_ns = 0, next_tick_ns = SIM_NS_PER_MS, tick_count;
static uint64_t boot_global_ns, stop_at_ns;
static int64_t drift_ppm;
static int sim_fd = -1;
static bool in_isr_flag, kernel_running_flag, servicing;
static const char *node_name = "node", *state_dir = ".";
static uint64_t rng_state = 0x9E3779B97F4A7C15ull;
static uint32_t nvic_enabled[8];
static uint64_t cost_since_service;
volatile uint32_t uwTick;  /* HAL tick variable (HAL itself is replaced) */
uint32_t uwTickPrio = 15;
uint32_t uwTickFreq = 1;

/* ---------------------------------------------------------------- env */
const char *sim_env(const char *key, const char *fallback) { const char *v = getenv(key); return v && *v ? v : fallback; }
long sim_env_long(const char *key, long fallback) { const char *v = getenv(key); return v && *v ? strtol(v, NULL, 0) : fallback; }
const char *sim_node_name(void) { return node_name; }
const char *sim_state_dir(void) { return state_dir; }
bool sim_kernel_running(void) { return kernel_running_flag; }
bool sim_in_isr(void) { return in_isr_flag; }
void sim_set_kernel_running(void) { kernel_running_flag = true; }
uint32_t sim_rand(void)
{
    rng_state ^= rng_state << 13; rng_state ^= rng_state >> 7; rng_state ^= rng_state << 17;
    return (uint32_t)(rng_state >> 16);
}

void sim_log(const char *fmt, ...)
{
    char buf[1024]; va_list ap; va_start(ap, fmt);
    int n = vsnprintf(buf, sizeof buf, fmt, ap); va_end(ap);
    if (n < 0) return;
    if (n > (int)sizeof buf - 1) n = sizeof buf - 1;
    fprintf(stderr, "[sim %s %10.3f ms] %s\n", node_name, now_ns / 1e6, buf);
}

/* ---------------------------------------------------------------- time */
uint64_t sim_now_ns(void) { return now_ns; }
uint64_t sim_next_tick_ns(void) { return next_tick_ns; }
void sim_advance_raw_ns(uint64_t ns) { now_ns += ns; }
volatile int sim_core_busy;
void sim_mmio_live(void);  /* trapped register reads: time passes, no interrupt point */
uint32_t sim_now_ms(void) { return (uint32_t)(now_ns / SIM_NS_PER_MS); }
uint64_t sim_local_to_global(uint64_t local)
{
    /* local = (global - boot) * (1 + ppm/1e6) */
    if (!drift_ppm) return boot_global_ns + local;
    return boot_global_ns + (uint64_t)((long double)local * 1000000.0L / (1000000.0L + drift_ppm) + 0.5L);
}
static uint64_t global_to_local(uint64_t global)
{
    if (global <= boot_global_ns) return 0;
    uint64_t g = global - boot_global_ns;
    if (!drift_ppm) return g;
    return (uint64_t)((long double)g * (1000000.0L + drift_ppm) / 1000000.0L + 0.5L);
}

/* ---------------------------------------------------------------- timed events */
typedef struct ev { uint64_t at; uint64_t seq; int irqn; sim_isr_fn fn; void *arg; uintptr_t a2; struct ev *next; } ev_t;
static ev_t *events, *free_events;
static uint64_t ev_seq;

void sim_schedule(uint64_t at, int irqn, sim_isr_fn fn, void *arg, uintptr_t a2)
{
    ev_t *e = free_events;
    if (e) free_events = e->next; else e = malloc(sizeof *e);
    *e = (ev_t){at, ev_seq++, irqn, fn, arg, a2, NULL};
    ev_t **p = &events;
    while (*p && ((*p)->at < at || ((*p)->at == at && (*p)->seq < e->seq))) p = &(*p)->next;
    e->next = *p; *p = e;
}
void sim_cancel(sim_isr_fn fn, void *arg)
{
    for (ev_t **p = &events; *p;) {
        if ((*p)->fn == fn && (*p)->arg == arg) { ev_t *e = *p; *p = e->next; e->next = free_events; free_events = e; }
        else p = &(*p)->next;
    }
}

/* NVIC: firmware may enable/disable lines through HAL (tracked here) or CMSIS inline register writes
 * (NVIC->ISER/ICER in the mapped system control space, folded in here). */
extern volatile uint8_t *sim_ppb_alias;  /* untrapped view of 0xE0000000.. (sim_mmio.c) */
#define NVIC_ISER ((volatile uint32_t *)(sim_ppb_alias + 0xE100u))
#define NVIC_ICER ((volatile uint32_t *)(sim_ppb_alias + 0xE180u))
static void nvic_sync(void)
{
    if (!sim_ppb_alias) return;
    for (int i = 0; i < 8; i++) {
        uint32_t s = NVIC_ISER[i], c = NVIC_ICER[i];
        if (s | c) {
            nvic_enabled[i] = (nvic_enabled[i] | s) & ~(c & ~s);
            NVIC_ISER[i] = 0; NVIC_ICER[i] = 0;
        }
    }
}
bool sim_nvic_enabled(int irqn)
{
    if (irqn < 0) return true;
    nvic_sync();
    return (nvic_enabled[irqn >> 5] >> (irqn & 31)) & 1u;
}
void sim_nvic_set(int irqn, bool en)
{
    if (irqn < 0) return;
    nvic_sync();
    if (en) nvic_enabled[irqn >> 5] |= 1u << (irqn & 31); else nvic_enabled[irqn >> 5] &= ~(1u << (irqn & 31));
}

static bool irqs_masked(void) { return sim_primask || sim_faultmask || in_isr_flag; }

static void run_isr(int irqn, sim_isr_fn fn, void *arg, uintptr_t a2)
{
    uint32_t saved_ipsr = sim_ipsr;
    in_isr_flag = true; sim_ipsr = (uint32_t)(irqn < 0 ? 15 : irqn + 16);
    fn(arg, a2);
    sim_ipsr = saved_ipsr; in_isr_flag = false;
    sim_primask = 0; /* exception return restores the thread's (enabled) posture */
}

static void tick_isr(void *a, uintptr_t b)
{
    (void)a; (void)b;
    tick_count++;
    if (stop_at_ns && now_ns >= stop_at_ns) { sim_log("standalone stop"); fflush(stdout); fflush(stderr); _exit(0); }
    uwTick = (uint32_t)(now_ns / SIM_NS_PER_MS);
    sim_tx_trace_time = (unsigned)(now_ns / 1000u);
    sim_watchdog_check();
    if (kernel_running_flag) sim_port_tick();
}

/* Run every interrupt that is due and allowed at the current time. */
static void service(void)
{
    if (irqs_masked() || servicing) return;
    sim_mmio_live();
    servicing = true;
    nvic_sync();
    for (;;) {
        if (now_ns >= next_tick_ns) {
            next_tick_ns += SIM_NS_PER_MS;
            run_isr(-1, tick_isr, NULL, 0);
            continue;
        }
        ev_t **p = &events, *e = NULL;
        while (*p && (*p)->at <= now_ns) {
            int n = (*p)->irqn;
            if (n < 0 || ((nvic_enabled[n >> 5] >> (n & 31)) & 1u)) { e = *p; *p = e->next; break; }
            p = &(*p)->next;
        }
        if (!e) break;
        sim_isr_fn fn = e->fn; void *arg = e->arg; uintptr_t a2 = e->a2; int irqn = e->irqn;
        e->next = free_events; free_events = e;
        run_isr(irqn, fn, arg, a2);
    }
    servicing = false;
    cost_since_service = 0;
}

/* ---------------------------------------------------------------- coordinator link */
static uint8_t *out_buf; static size_t out_len, out_cap;
void sim_send2(uint16_t type, const void *a, uint32_t alen, const void *b, uint32_t blen)
{
    if (sim_fd < 0) return;
    uint32_t len = 2 + alen + blen;
    if (out_len + 4 + len > out_cap) { out_cap = (out_len + 4 + len) * 2 + 4096; out_buf = realloc(out_buf, out_cap); }
    memcpy(out_buf + out_len, &len, 4); memcpy(out_buf + out_len + 4, &type, 2);
    if (alen) memcpy(out_buf + out_len + 6, a, alen);
    if (blen) memcpy(out_buf + out_len + 6 + alen, b, blen);
    out_len += 4 + len;
}
void sim_send(uint16_t type, const void *payload, uint32_t len) { sim_send2(type, payload, len, NULL, 0); }
static void flush_out(void)
{
    size_t off = 0;
    while (off < out_len) {
        ssize_t n = write(sim_fd, out_buf + off, out_len - off);
        if (n < 0) { if (errno == EINTR) continue; _exit(3); }
        off += (size_t)n;
    }
    out_len = 0;
}
static uint8_t in_buf[1 << 20]; static size_t in_len;
static sim_rx_fn handlers[256];
void sim_register_rx(uint16_t type, sim_rx_fn fn) { handlers[type & 255] = fn; }

static void read_fully(void *p, size_t n)
{
    uint8_t *d = p;
    while (n) {
        ssize_t r = read(sim_fd, d, n);
        if (r == 0) { fflush(stdout); _exit(0); }  /* coordinator gone */
        if (r < 0) { if (errno == EINTR) continue; _exit(4); }
        d += r; n -= (size_t)r;
    }
}

static uint64_t next_wakeup_local(void)
{
    if (kernel_running_flag && sim_port_thread_ready()) return now_ns;
    uint64_t t = UINT64_MAX;
    if (events) t = events->at;  /* conservative: a masked event still counts */
    if (kernel_running_flag) {
        uint64_t k = sim_port_next_timer_ticks();
        if (k != UINT64_MAX) { uint64_t tt = (tick_count + k) * SIM_NS_PER_MS; if (tt < t) t = tt; }
    } else if (next_tick_ns < t) t = next_tick_ns;
    /* the watchdog and HAL tick variable are refreshed at least every 100 ms */
    uint64_t cap = now_ns + 100 * SIM_NS_PER_MS;
    return t < cap ? (t < now_ns ? now_ns : t) : cap;
}

static void sync_coordinator(void)
{
    if (sim_fd < 0) { grant_ns = UINT64_MAX; return; }
    sim_core_busy++;
    uint64_t done[2] = {sim_local_to_global(now_ns), sim_local_to_global(next_wakeup_local())};
    sim_send(SIM_MSG_DONE, done, sizeof done);
    fflush(stdout);
    flush_out();
    for (;;) {
        uint32_t len; uint16_t type;
        read_fully(&len, 4);
        if (len < 2 || len - 2 > sizeof in_buf) _exit(5);
        read_fully(&type, 2); in_len = len - 2; read_fully(in_buf, in_len);
        if (type == SIM_MSG_GRANT) {
            uint64_t g; memcpy(&g, in_buf, 8);
            grant_ns = global_to_local(g);
            sim_core_busy--;
            return;
        }
        if (type == SIM_MSG_STOP) { fflush(stdout); fflush(stderr); _exit(0); }
        if (handlers[type & 255]) handlers[type & 255](type, in_buf, (uint32_t)in_len);
    }
}

uint64_t sim_global_to_local(uint64_t g) { return global_to_local(g); }

/* Advance local time to t (never past the grant without asking), running due interrupts on the way. */
static void advance_to(uint64_t t)
{
    while (now_ns < t) {
        if (now_ns >= grant_ns) sync_coordinator();
        uint64_t step = t;
        if (grant_ns < step) step = grant_ns;
        if (step <= now_ns) continue;
        now_ns = step;
    }
    sim_mmio_live();
}

void sim_charge_ns(uint64_t ns)
{
    uint64_t t = now_ns + ns;
    sim_core_busy++;
    if (t > grant_ns) advance_to(t); else { now_ns = t; sim_mmio_live(); }
    sim_core_busy--;
    cost_since_service += ns;
    if (!irqs_masked() && (now_ns >= next_tick_ns || (events && events->at <= now_ns))) {
        service();
        if (kernel_running_flag) sim_port_maybe_preempt();
    }
}

void sim_yield_point(void) { sim_charge_ns(200); }
void sim_irq_enable_point(void)
{
    if (!irqs_masked() && (now_ns >= next_tick_ns || (events && events->at <= now_ns))) {
        service();
        if (kernel_running_flag) sim_port_maybe_preempt();
    }
}

static uint32_t nop_count;
void sim_nop(void)
{
    /* One core cycle; settle polled hardware handshakes now and then. */
    if ((++nop_count & 63u) == 0) { sim_mmio_settle(); sim_charge_ns(64000u / sim_cycles_per_us); }
}
void sim_barrier(void) {}
void sim_wfi(void)
{
    /* Sleep until the next interrupt: skip to the next event. */
    if (sim_primask) { sim_charge_ns(1000); return; }
    uint64_t t = next_tick_ns;
    if (events && events->at < t) t = events->at;
    if (t > now_ns) advance_to(t);
    service();
    if (kernel_running_flag) sim_port_maybe_preempt();
}

void sim_busy_wait_ns(uint64_t ns)
{
    uint64_t end = now_ns + ns;
    while (now_ns < end) {
        uint64_t step = end - now_ns;
        if (step > 100000) step = 100000;  /* 100 us slices: interrupts and preemption stay timely */
        sim_charge_ns(step);
    }
}

/* Called by the scheduler when no thread is ready: skip to the next event. */
void sim_core_idle(void)
{
    uint64_t t = next_tick_ns;
    if (events && events->at < t) t = events->at;
    if (t <= now_ns) t = now_ns;
    advance_to(t);
    service();
}

/* ---------------------------------------------------------------- reset */
extern void sim_mmio_persist(void);
__attribute__((noreturn)) void sim_reset(int cause)
{
    static const char *names[] = {"por", "soft", "iwdg", "panic"};
    sim_log("reset (%s)", names[cause & 3]);
    fflush(stdout); fflush(stderr);
    sim_mmio_persist();
    uint8_t c = (uint8_t)cause;
    if (sim_fd >= 0) {
        uint64_t done[2] = {sim_local_to_global(now_ns), sim_local_to_global(now_ns)};
        sim_send(SIM_MSG_RESET, &c, 1);
        sim_send(SIM_MSG_DONE, done, sizeof done);
        flush_out();
    }
    _exit(0);
}

__attribute__((noreturn)) void sim_rust_panic(const uint8_t *msg, size_t len)
{
    sim_log("rust panic: %.*s (core spins until the watchdog)", (int)len, (const char *)msg);
    /* Bare metal: loop {} forever at the panicking context's priority. */
    for (;;) sim_charge_ns(100000);
}

/* ---------------------------------------------------------------- entry */
extern int sim_board_main(void);
static void on_fatal(int sig)
{
    fprintf(stderr, "[sim %s %10.3f ms] fatal signal %d\n", node_name, now_ns / 1e6, sig);
    fflush(stdout); fflush(stderr);
    signal(sig, SIG_DFL); raise(sig);
}

int main(int argc, char **argv)
{
    (void)argc; (void)argv;
    node_name = sim_env("SIM_NODE", "node");
    state_dir = sim_env("SIM_STATE_DIR", ".");
    sim_fd = (int)sim_env_long("SIM_FD", -1);
    boot_global_ns = (uint64_t)strtoull(sim_env("SIM_BOOT_GLOBAL_NS", "0"), NULL, 0);
    drift_ppm = sim_env_long("SIM_DRIFT_PPM", 0);
    rng_state ^= (uint64_t)sim_env_long("SIM_SEED", 1) * 0x2545F4914F6CDD1Dull;
    for (const char *p = node_name; *p; p++) rng_state = rng_state * 131 + (uint8_t)*p;
    rng_state ^= boot_global_ns;
    if (!rng_state) rng_state = 1;
    prctl(PR_SET_PTRACER, (unsigned long)-1, 0, 0, 0);  /* the coordinator reads probe symbols from /proc/pid/mem */
    signal(SIGSEGV, on_fatal); signal(SIGBUS, on_fatal); signal(SIGFPE, on_fatal); signal(SIGILL, on_fatal); signal(SIGABRT, on_fatal);
    setvbuf(stdout, NULL, _IOFBF, 1 << 16);
    setvbuf(stderr, NULL, _IOLBF, 1 << 12);
    if (sim_fd >= 0) {
        sim_send(SIM_MSG_HELLO, node_name, (uint32_t)strlen(node_name));
        grant_ns = 0;
    } else {
        grant_ns = UINT64_MAX;  /* standalone: free running */
        stop_at_ns = (uint64_t)sim_env_long("SIM_STOP_MS", 0) * SIM_NS_PER_MS;
    }
    sim_mmio_init();
    /* Bootloader: image checks before the application starts. */
    sim_busy_wait_ns((uint64_t)sim_env_long("SIM_BOOT_DELAY_US", 30000) * 1000u);
    sim_board_main();
    sim_log("board main returned");
    sim_reset(SIM_RESET_SOFT);
}
