/* TIM2 at register level, for firmware that drives it directly (DAQ-Board 99db2e6+ MCP3564R frame engine: a free-running
 * microsecond counter read from TIM2->CNT, CC1/CC2 compare interrupts programmed through CCRx/DIER/EGR, flags cleared by
 * rc_w0 writes to SR). The counter runs from virtual time; compare matches set SR.CCxIF and, when enabled in DIER, call
 * the firmware's TIM2 handler body (the board's stm32u5xx_it.c is replaced by the shim, so its TIM2_IRQHandler is not
 * linked: `mcp3564r_timer_irq` is what it calls). Active only when the firmware defines that handler. */
#include "sim.h"
#include "main.h"

#if defined(STM32U5) && defined(HAL_TIM_MODULE_ENABLED)
__attribute__((weak)) void mcp3564r_timer_irq(void);

#define TIM_CC_FLAGS (TIM_SR_CC1IF | TIM_SR_CC2IF | TIM_SR_CC3IF | TIM_SR_CC4IF)
static struct {
    int on; uint64_t base_ns; uint32_t base_cnt, psc, cnt_written, flags, prev;
    int dispatching;
} t;

static void tim2_event(void *a, uintptr_t b);
extern volatile int sim_mmio_in_signal;
static uint64_t tick_ns(void)
{
    uint32_t f = HAL_RCC_GetPCLK1Freq();
    if (!f) f = SystemCoreClock ? SystemCoreClock : 160000000u;
    uint64_t ns = (uint64_t)(TIM2->PSC + 1u) * 1000000000ull / f;
    return ns ? ns : 1;
}
static volatile uint32_t *ccr(int ch) { return ch == 0 ? &TIM2->CCR1 : ch == 1 ? &TIM2->CCR2 : ch == 2 ? &TIM2->CCR3 : &TIM2->CCR4; }

/* Bring the counter, the flags and the next compare event up to now; returns the flags pending service. */
static uint32_t sync(void)
{
    if (!(TIM2->CR1 & TIM_CR1_CEN)) { t.on = 0; return 0; }
    uint64_t now = sim_now_ns(), tick = tick_ns();
    if (!t.on || TIM2->PSC != t.psc) { t.on = 1; t.psc = TIM2->PSC; t.base_ns = now; t.base_cnt = TIM2->CNT; t.prev = t.base_cnt; t.flags = 0; }
    else if (TIM2->CNT != t.cnt_written) { t.base_ns = now; t.base_cnt = TIM2->CNT; t.prev = t.base_cnt; }  /* firmware wrote CNT */
    uint32_t cnt = t.base_cnt + (uint32_t)((now - t.base_ns) / tick);
    t.flags &= TIM2->SR | ~TIM_CC_FLAGS;                       /* rc_w0: written zeros clear, written ones do nothing */
    for (int ch = 0; ch < 4; ch++) {
        uint32_t c = *ccr(ch);
        if ((uint32_t)(c - t.prev - 1u) < (uint32_t)(cnt - t.prev)) t.flags |= TIM_SR_CC1IF << ch;
        if (TIM2->EGR & (TIM_EGR_CC1G << ch)) t.flags |= TIM_SR_CC1IF << ch;
    }
    TIM2->EGR = 0;
    t.prev = cnt;
    TIM2->CNT = cnt; t.cnt_written = cnt;
    TIM2->SR = (TIM2->SR & ~TIM_CC_FLAGS) | t.flags;
    /* the next enabled compare match */
    uint32_t best = 0xFFFFFFFFu; int any = 0;
    for (int ch = 0; ch < 4; ch++) {
        if (!(TIM2->DIER & (TIM_DIER_CC1IE << ch))) continue;
        uint32_t d = *ccr(ch) - cnt; if (!d) d = 0xFFFFFFFFu;
        if (d < best) { best = d; any = 1; }
    }
    sim_cancel(tim2_event, &t);
    if (any) sim_schedule(now + (uint64_t)best * tick, -1, tim2_event, &t, 0);
    return t.flags & (TIM2->DIER & (TIM_DIER_CC1IE | TIM_DIER_CC2IE | TIM_DIER_CC3IE | TIM_DIER_CC4IE));
}
static void tim2_event(void *a, uintptr_t b)
{
    (void)a; (void)b;
    if (t.dispatching) return;
    t.dispatching = 1;
    for (int i = 0; i < 4 && sync(); i++) mcp3564r_timer_irq();  /* the handler clears what it serviced */
    sync();
    t.dispatching = 0;
}
/* Called whenever virtual time moves (sim_mmio_live). */
void sim_tim2_live(void)
{
    if (!mcp3564r_timer_irq || t.dispatching) return;
    if (sim_mmio_in_signal) {
        /* counter only (no scheduling from a signal handler) */
        if (t.on && (TIM2->CR1 & TIM_CR1_CEN) && TIM2->PSC == t.psc && TIM2->CNT == t.cnt_written) {
            uint32_t cnt = t.base_cnt + (uint32_t)((sim_now_ns() - t.base_ns) / tick_ns());
            TIM2->CNT = cnt; t.cnt_written = cnt;
        }
        return;
    }
    if (sync()) sim_schedule(sim_now_ns(), -1, tim2_event, &t, 0);
}
#else
void sim_tim2_live(void) {}
#endif
