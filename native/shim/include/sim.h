/* Native SITL core API shared by the shim modules. */
#ifndef SIM_H
#define SIM_H
#include <stdint.h>
#include <stddef.h>
#include <stdbool.h>

#define SIM_NS_PER_MS 1000000ull

/* ---- wire protocol with the coordinator (native_bay.rs): [u32 len][u16 type][payload] little endian ---- */
enum {
    SIM_MSG_HELLO = 1,    /* node -> coord: {name...} */
    SIM_MSG_DONE = 2,     /* node -> coord: {u64 reached_global, u64 next_wakeup_global} */
    SIM_MSG_CAN_TX = 3,   /* {u64 t, char port[8], u8 flags, u8 len, u8 pad[2], u32 id, data} */
    SIM_MSG_UART_TX = 4,  /* {u64 t, char port[8], u32 baud, bytes} */
    SIM_MSG_GPIO = 5,     /* {u64 t, u8 port, u8 pin, u8 value, u8 pad} */
    SIM_MSG_RESET = 6,    /* {u8 cause} then exit */
    SIM_MSG_LOG = 7,      /* text */
    SIM_MSG_GRANT = 101,  /* coord -> node: {u64 until_global} */
    SIM_MSG_CAN_RX = 102, /* {u64 t, char port[8], u8 flags, u8 len, u8 pad[2], u32 id, data} */
    SIM_MSG_UART_RX = 103,/* {u64 t, char port[8], bytes} */
    SIM_MSG_ADC = 104,    /* {char device[16], text "v0,v1,..."} */
    SIM_MSG_LINK = 105,   /* {char port[8], u8 connected} */
    SIM_MSG_STOP = 106,
    SIM_MSG_GPIO_IN = 107,/* {u8 port, u8 pin, u8 value} */
};
enum { SIM_RESET_POR = 0, SIM_RESET_SOFT = 1, SIM_RESET_IWDG = 2, SIM_RESET_PANIC = 3 };
#define SIM_CAN_EXT 1u
#define SIM_CAN_FD 2u
#define SIM_CAN_BRS 4u
#define SIM_CAN_RTR 8u

/* ---- time ---- */
uint64_t sim_now_ns(void);              /* node-local virtual time since this boot */
uint32_t sim_now_ms(void);
void sim_charge_ns(uint64_t ns);        /* CPU time used by the running context; may run interrupts/preempt */
void sim_busy_wait_ns(uint64_t ns);     /* a busy loop of the given length (HAL_Delay etc.) */
void sim_yield_point(void);             /* interrupt/preemption point */

/* ---- interrupts / timed events (node-local time) ---- */
typedef void (*sim_isr_fn)(void *arg, uintptr_t a2);
/* irqn < 0: always enabled (core exceptions); otherwise gated by the simulated NVIC. */
void sim_schedule(uint64_t at_ns, int irqn, sim_isr_fn fn, void *arg, uintptr_t a2);
void sim_cancel(sim_isr_fn fn, void *arg);
bool sim_nvic_enabled(int irqn);
void sim_nvic_set(int irqn, bool enabled);

/* ---- outbound ---- */
void sim_send(uint16_t type, const void *payload, uint32_t len);
void sim_send2(uint16_t type, const void *a, uint32_t alen, const void *b, uint32_t blen);
uint64_t sim_local_to_global(uint64_t local_ns);
void sim_log(const char *fmt, ...) __attribute__((format(printf, 1, 2)));
__attribute__((noreturn)) void sim_reset(int cause);

/* ---- inbound handler registration ---- */
typedef void (*sim_rx_fn)(uint16_t type, const uint8_t *payload, uint32_t len);
void sim_register_rx(uint16_t type, sim_rx_fn fn);

/* ---- config/env ---- */
const char *sim_env(const char *key, const char *fallback);
long sim_env_long(const char *key, long fallback);
const char *sim_node_name(void);
const char *sim_state_dir(void);
uint32_t sim_rand(void);                /* deterministic per node/boot (seeded) */
bool sim_kernel_running(void);
bool sim_in_isr(void);

/* ---- device helpers (sim_devices.c) ---- */
void sim_mmio_init(void);
void sim_mmio_settle(void);             /* complete hardware handshakes polled by firmware (ADC calibration ...) */
void sim_watchdog_check(void);
void sim_adc_set_samples(const char *device, const char *csv);
int32_t sim_adc_sample(const char *device, unsigned channel, int32_t fallback);
bool sim_adc_has(const char *device);
const char *sim_port_name(const void *instance);
#endif
