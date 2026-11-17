/* Native SITL memory map: the MCU's fixed address ranges exist in the host process at their real addresses,
 * so the board's own CMSIS/HAL headers and direct register/flash accesses work unchanged.
 *   flash        0x08000000 (+secure alias 0x0C000000)  file-backed (persists across simulated reboots)
 *   system/OTP   UID, flash size, calibration values      seeded per node
 *   peripherals  0x40000000-0x5FFFFFFF                    plain memory; behaviour lives in the HAL shim
 *   TAMP/RTC     backup-domain page                       file-backed (persists across reboots)
 *   core (PPB)   0xE0000000-0xE00FFFFF                    SCB/NVIC/DWT/SysTick
 * Polled hardware handshakes (ADC calibration/ready, RCC ready flags, ...) complete in sim_mmio_settle(). */
#define _GNU_SOURCE
#include "sim.h"
#include "main.h"
#include <errno.h>
#include <fcntl.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/stat.h>
#include <unistd.h>
#include <signal.h>
#include <ucontext.h>
#include <sys/syscall.h>
#include <sys/time.h>

#ifndef FLASH_BASE
#define FLASH_BASE 0x08000000UL
#endif

static void map_fixed(uintptr_t addr, size_t size, int fd, const char *what)
{
    int flags = MAP_FIXED | (fd >= 0 ? MAP_SHARED : (MAP_PRIVATE | MAP_ANONYMOUS | MAP_NORESERVE));
    void *p = mmap((void *)addr, size, PROT_READ | PROT_WRITE, flags, fd, 0);
    if (p == MAP_FAILED || (uintptr_t)p != addr) {
        fprintf(stderr, "[sim] cannot map %s at 0x%08lx (+0x%zx): %s\n", what, (unsigned long)addr, size, strerror(errno));
        _exit(10);
    }
}
static int reserve_ok(uintptr_t addr, size_t size)
{
    void *p = mmap((void *)addr, size, PROT_NONE, MAP_PRIVATE | MAP_ANONYMOUS | MAP_NORESERVE | MAP_FIXED_NOREPLACE, -1, 0);
    if (p == MAP_FAILED) return 0;
    munmap(p, size);
    return 1;
}

static int state_file(const char *name, size_t size, int *created)
{
    char path[1024];
    snprintf(path, sizeof path, "%s/%s", sim_state_dir(), name);
    int fd = open(path, O_RDWR | O_CREAT, 0644);
    if (fd < 0) { perror(path); _exit(11); }
    struct stat st; fstat(fd, &st);
    *created = st.st_size == 0;
    if ((size_t)st.st_size < size && ftruncate(fd, (off_t)size) != 0) { perror(path); _exit(11); }
    return fd;
}

static size_t flash_size;
volatile uint8_t *sim_ppb_alias;
uint64_t sim_next_tick_ns(void);
void sim_advance_raw_ns(uint64_t ns);
extern uint32_t sim_cycles_per_us;

/* ---------------------------------------------------------------- live core registers
 * DWT->CYCCNT, SysTick->VAL/LOAD and SCB->ICSR.PENDSTSET are live hardware the firmware samples directly (sub-ms
 * time-sync stamps, cycle budgets, busy-wait delays). Virtual time only moves inside the simulated core, which
 * refreshes these words in the mapped core page every time it moves (sim_mmio_live). A firmware busy-wait that
 * spins on CYCCNT without calling into the core is broken by a CPU-time interval timer: when virtual time has not
 * moved for a slice of host CPU time, a few microseconds pass (the only wall-clock-dependent path; such spins are
 * rare and short, e.g. stepper driver wake-up delays). */
#define PPB 0xE0000000u
static uint32_t cyc_written, cyc_offset;
#define ALIAS32(off) (*(volatile uint32_t *)(sim_ppb_alias + (off)))
void sim_mmio_live(void)
{
    if (!sim_ppb_alias) return;
    uint32_t cur = ALIAS32(0x1004);
    if (cur != cyc_written) cyc_offset += cur - cyc_written;  /* firmware wrote DWT->CYCCNT */
    uint64_t now = sim_now_ns();
    uint32_t cyc = (uint32_t)(now * sim_cycles_per_us / 1000u) + cyc_offset;
    ALIAS32(0x1004) = cyc; cyc_written = cyc;
    uint32_t load = sim_cycles_per_us * 1000u - 1u;
    ALIAS32(0xE014) = load;
    uint64_t start = sim_next_tick_ns() - SIM_NS_PER_MS, el = now > start ? now - start : 0;
    uint32_t pend = el >= SIM_NS_PER_MS;
    if (pend) el %= SIM_NS_PER_MS;
    uint32_t done = (uint32_t)(el * sim_cycles_per_us / 1000u);
    ALIAS32(0xE018) = done > load ? 0 : load - done;
    ALIAS32(0xE010) |= 7u;
    ALIAS32(0xED04) = (ALIAS32(0xED04) & ~(1u << 26)) | (pend << 26);
}
extern volatile int sim_core_busy;
static uint64_t stall_last;
static void on_stall_timer(int sig)
{
    (void)sig;
    uint64_t now = sim_now_ns();
    if (!sim_core_busy && now == stall_last) { sim_advance_raw_ns(20000); sim_mmio_live(); }
    stall_last = sim_now_ns();
}
static void trap_core_pages(void)
{
    struct sigaction sa; memset(&sa, 0, sizeof sa);
    sa.sa_flags = SA_RESTART; sa.sa_handler = on_stall_timer;
    sigaction(SIGVTALRM, &sa, NULL);
    struct itimerval it = {{0, 2000}, {0, 2000}};
    setitimer(ITIMER_VIRTUAL, &it, NULL);
    sim_mmio_live();
}
static uint8_t *flash_mem = (uint8_t *)FLASH_BASE;

__attribute__((constructor(101))) static void sim_mmio_reserve(void)
{
    /* Reserve the fixed ranges before the C library places anything there. */
    static const struct { uintptr_t a; size_t s; } ranges[] = {
        {0x08000000u, 0x00400000u}, {0x0BF00000u, 0x00100000u}, {0x0C000000u, 0x00400000u}, {0x1FFF0000u, 0x00010000u},
        {0x20000000u, 0x00100000u}, {0x40000000u, 0x20000000u}, {0xE0000000u, 0x00100000u}};
    for (size_t i = 0; i < sizeof ranges / sizeof ranges[0]; i++) {
        if (!reserve_ok(ranges[i].a, ranges[i].s)) {
            fprintf(stderr, "[sim] address range 0x%08lx busy\n", (unsigned long)ranges[i].a);
            _exit(10);
        }
        map_fixed(ranges[i].a, ranges[i].s, -1, "range");
    }
}

static void put32(uintptr_t a, uint32_t v) { *(volatile uint32_t *)a = v; }
static void put16(uintptr_t a, uint16_t v) { *(volatile uint16_t *)a = v; }

void sim_mmio_init(void)
{
    int created;
    {
        int pfd = (int)syscall(SYS_memfd_create, "ppb", 0);
        if (pfd < 0 || ftruncate(pfd, 0x100000) != 0) { perror("memfd"); _exit(10); }
        map_fixed(PPB, 0x100000, pfd, "core peripherals");
        sim_ppb_alias = mmap(NULL, 0x100000, PROT_READ | PROT_WRITE, MAP_SHARED, pfd, 0);
        close(pfd);
    }
    flash_size = (size_t)sim_env_long("SIM_FLASH_SIZE", 512 * 1024);
    int fd = state_file("flash.bin", flash_size, &created);
    map_fixed(0x08000000u, flash_size, fd, "flash");
    map_fixed(0x0C000000u, flash_size, fd, "flash (secure alias)");
    if (created) {
        memset(flash_mem, 0xFF, flash_size);
        const char *init = sim_env("SIM_FLASH_INIT", NULL);
        if (init) {
            FILE *f = fopen(init, "rb");
            if (f) { size_t n = fread(flash_mem, 1, flash_size, f); fclose(f); sim_log("flash initialised from %s (%zu bytes)", init, n); }
            else sim_log("flash image %s not readable; blank flash", init);
        }
        msync(flash_mem, flash_size, MS_SYNC);
    }
    close(fd);
    /* Backup domain (TAMP backup registers + RTC) survives resets. */
#ifdef TAMP_BASE
    {
        int bfd = state_file("backup-domain.bin", 4096, &created);
        uintptr_t page = (uintptr_t)TAMP_BASE & ~(uintptr_t)0xFFF;
        map_fixed(page, 4096, bfd, "TAMP page");
#if defined(TAMP_BASE_NS) && defined(TAMP_BASE_S)
        uintptr_t other = ((uintptr_t)TAMP_BASE == (uintptr_t)TAMP_BASE_NS ? (uintptr_t)TAMP_BASE_S : (uintptr_t)TAMP_BASE_NS) & ~(uintptr_t)0xFFF;
        map_fixed(other, 4096, bfd, "TAMP alias page");
#endif
        close(bfd);
    }
#endif
    /* Device identity and factory calibration. */
    uint32_t h = 2166136261u;
    for (const char *p = sim_env("SIM_UID_SEED", sim_node_name()); *p; p++) h = (h ^ (uint8_t)*p) * 16777619u;
#ifdef UID_BASE
    put32(UID_BASE, 0x00470031u ^ (h & 0xFFFF)); put32(UID_BASE + 4, 0x31345110u ^ h); put32(UID_BASE + 8, 0x20363050u + (h >> 8));
#endif
#ifdef FLASHSIZE_BASE
    put16(FLASHSIZE_BASE, (uint16_t)(flash_size / 1024u));
#endif
#ifdef TEMPSENSOR_CAL1_ADDR
    *(volatile uint16_t *)TEMPSENSOR_CAL1_ADDR = 1034; *(volatile uint16_t *)TEMPSENSOR_CAL2_ADDR = 1384;
#endif
#ifdef VREFINT_CAL_ADDR
    *(volatile uint16_t *)VREFINT_CAL_ADDR = 1655;
#endif
    /* Reset cause flags. */
    long cause = sim_env_long("SIM_RESET_CAUSE", SIM_RESET_POR);
    uint32_t csr = 0;
#ifdef RCC_CSR_PINRSTF
    csr = RCC_CSR_PINRSTF;
    if (cause == SIM_RESET_POR) csr |= RCC_CSR_BORRSTF;
    if (cause == SIM_RESET_SOFT) csr |= RCC_CSR_SFTRSTF;
    if (cause == SIM_RESET_IWDG) csr |= RCC_CSR_IWDGRSTF;
    RCC->CSR = csr;
#endif
    (void)csr;
    if (!sim_env_long("SIM_NO_CORE_TRAP", 0)) trap_core_pages();
}

void sim_mmio_persist(void)
{
    if (flash_size) msync(flash_mem, flash_size, MS_SYNC);
}

/* ---------------------------------------------------------------- IWDG and software reset */
static int iwdg_running; static uint64_t iwdg_deadline;
static uint64_t iwdg_period_ns(void)
{
#ifdef IWDG
    uint32_t pr = IWDG->PR & 7u, rlr = IWDG->RLR & 0xFFFu;
    uint64_t div = 4ull << (pr > 6 ? 6 : pr);
    return (uint64_t)(rlr + 1u) * div * 1000000000ull / 32000ull;
#else
    return 0;
#endif
}
void sim_watchdog_check(void)
{
#ifdef IWDG
    uint32_t kr = IWDG->KR;
    if (kr) {
        IWDG->KR = 0;
        if (kr == 0xCCCCu) { iwdg_running = 1; iwdg_deadline = sim_now_ns() + iwdg_period_ns(); sim_log("IWDG started (%.0f ms)", iwdg_period_ns() / 1e6); }
        else if (kr == 0xAAAAu && iwdg_running) iwdg_deadline = sim_now_ns() + iwdg_period_ns();
    }
    if (iwdg_running && !sim_env_long("SIM_NO_WATCHDOG", 0) && sim_now_ns() > iwdg_deadline) sim_reset(SIM_RESET_IWDG);
#endif
    uint32_t aircr = sim_ppb_alias ? ALIAS32(0xED0C) : 0;
    if ((aircr & SCB_AIRCR_SYSRESETREQ_Msk) && ((aircr >> 16) == 0x05FAu)) sim_reset(SIM_RESET_SOFT);
}

/* ---------------------------------------------------------------- polled handshakes */
static void settle_adc(ADC_TypeDef *adc, const char *name)
{
    uint32_t cr = adc->CR;
    if (cr & ADC_CR_ADCAL) adc->CR = cr = cr & ~ADC_CR_ADCAL;
    if (cr & ADC_CR_ADDIS) adc->CR = cr = cr & ~(ADC_CR_ADDIS | ADC_CR_ADEN);
    if (cr & ADC_CR_ADEN) adc->ISR |= ADC_ISR_ADRDY;
    if (cr & ADC_CR_ADSTART) {
        uint32_t ch = (adc->SQR1 >> ADC_SQR1_SQ1_Pos) & 0x1Fu;
        int32_t v = sim_adc_sample(name, ch, -1);
#if defined(LL_ADC_CHANNEL_VREFINT) && defined(VREFINT_CAL_ADDR)
        if (v < 0 && ch == __LL_ADC_CHANNEL_TO_DECIMAL_NB(LL_ADC_CHANNEL_VREFINT)) v = *(volatile uint16_t *)VREFINT_CAL_ADDR;
#endif
#if defined(TEMPSENSOR_CAL1_ADDR)
        if (v < 0) v = *(volatile uint16_t *)TEMPSENSOR_CAL1_ADDR - 8;
#endif
        adc->DR = (uint32_t)(v < 0 ? 2048 : v);
        adc->CR = cr & ~ADC_CR_ADSTART;
        adc->ISR |= ADC_ISR_EOC | ADC_ISR_EOS;
    }
#ifdef ADC_CR_JADSTART
    if (cr & ADC_CR_JADSTART) {
        adc->JDR1 = (uint32_t)(sim_adc_sample(name, 99, 1000)); adc->CR &= ~ADC_CR_JADSTART; adc->ISR |= ADC_ISR_JEOC | ADC_ISR_JEOS;
    }
#endif
}

void sim_mmio_settle(void)
{
#ifdef ADC1
    settle_adc(ADC1, "adc1");
#endif
#ifdef ADC2
    settle_adc(ADC2, "adc2");
#endif
#ifdef ADC3
    settle_adc(ADC3, "adc3");
#endif
#ifdef RCC_CR_HSERDY
    if (RCC->CR & RCC_CR_HSEON) RCC->CR |= RCC_CR_HSERDY;
#endif
#ifdef RCC_CR_PLLRDY
    if (RCC->CR & RCC_CR_PLLON) RCC->CR |= RCC_CR_PLLRDY;
#endif
#ifdef RCC_CR_HSIRDY
    RCC->CR |= RCC_CR_HSIRDY;
#endif
    sim_watchdog_check();
}
