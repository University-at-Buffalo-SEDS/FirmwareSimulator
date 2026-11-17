/* ThreadX port for the native SITL core: the real kernel (common/src) schedules; threads are host coroutines.
 * Mirrors the Cortex-M port's behaviour: _tx_thread_system_return pends a context switch (PendSV), interrupts
 * preempt the running thread at interrupt points, and the scheduler idles (WFI) when no thread is ready. */
#define _GNU_SOURCE
#include "sim.h"
#include "tx_api.h"
#include "tx_thread.h"
#include "tx_timer.h"
#include "tx_initialize.h"
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <ucontext.h>

void sim_core_idle(void);
void sim_set_kernel_running(void);

typedef struct {
    ucontext_t ctx;
    void *stack;
    size_t stack_size;
} port_thread_t;

static ucontext_t scheduler_ctx;
static bool in_scheduler = true;
static size_t host_stack_size;

static UCHAR initial_memory[256 * 1024] __attribute__((aligned(16)));

VOID _tx_initialize_low_level(VOID)
{
    _tx_initialize_unused_memory = (VOID *)initial_memory;
}

UINT _tx_thread_interrupt_control(UINT new_posture)
{
    UINT old = sim_primask;
    sim_primask = new_posture & 1u;
    if (!sim_primask) sim_irq_enable_point();
    return old;
}

VOID _tx_thread_context_save(VOID) {}
VOID _tx_thread_context_restore(VOID) {}

static void thread_trampoline(void)
{
    /* first dispatch of a thread: interrupts enabled, as after an exception return */
    sim_primask = 0;
    _tx_thread_shell_entry();
}

void sim_tx_thread_delete_extension(struct TX_THREAD_STRUCT *t)
{
    port_thread_t *p = t->tx_thread_sim_port;
    if (!p || t == _tx_thread_current_ptr) return;
    munmap(p->stack, p->stack_size);
    free(p);
    t->tx_thread_sim_port = NULL;
}

VOID _tx_thread_stack_build(TX_THREAD *thread_ptr, VOID (*function_ptr)(VOID))
{
    (void)function_ptr;
    if (!host_stack_size) host_stack_size = (size_t)sim_env_long("SIM_THREAD_STACK_KB", 1024) * 1024u;
    port_thread_t *p = thread_ptr->tx_thread_sim_port;
    if (!p) {
        p = calloc(1, sizeof *p);
        p->stack_size = host_stack_size;
        p->stack = mmap(NULL, p->stack_size, PROT_READ | PROT_WRITE, MAP_PRIVATE | MAP_ANONYMOUS | MAP_NORESERVE | MAP_STACK, -1, 0);
        if (p->stack == MAP_FAILED) { perror("thread stack"); abort(); }
        mprotect(p->stack, 4096, PROT_NONE);  /* guard page */
        thread_ptr->tx_thread_sim_port = p;
    }
    getcontext(&p->ctx);
    p->ctx.uc_stack.ss_sp = (char *)p->stack + 4096;
    p->ctx.uc_stack.ss_size = p->stack_size - 4096;
    p->ctx.uc_link = &scheduler_ctx;
    makecontext(&p->ctx, thread_trampoline, 0);
    /* The board's stack is not executed on; keep the kernel's view of it consistent (stack checks pass,
     * a usage analysis reports the fill pattern untouched: stack depth is Renode-only evidence). */
    thread_ptr->tx_thread_stack_ptr = (VOID *)((UCHAR *)thread_ptr->tx_thread_stack_end - 64);
#ifdef TX_ENABLE_STACK_CHECKING
    thread_ptr->tx_thread_stack_highest_ptr = thread_ptr->tx_thread_stack_ptr;
#endif
}

/* The running thread gives the core back to the scheduler (blocking, or a higher-priority thread is ready). */
VOID _tx_thread_system_return(VOID)
{
    TX_THREAD *t = _tx_thread_current_ptr;
    if (in_scheduler || !t) return;
    port_thread_t *p = t->tx_thread_sim_port;
    unsigned saved = sim_primask;
    in_scheduler = true;
    swapcontext(&p->ctx, &scheduler_ctx);
    in_scheduler = false;
    sim_primask = saved;
}

bool sim_port_in_thread(void) { return !in_scheduler && _tx_thread_current_ptr; }
bool sim_port_thread_ready(void) { return _tx_thread_execute_ptr != TX_NULL; }

/* An interrupt made another thread the one to run: switch now (PendSV after the ISR). */
void sim_port_maybe_preempt(void)
{
    if (in_scheduler || sim_in_isr() || sim_primask) return;
    if (_tx_thread_preempt_disable) return;
    if (_tx_thread_execute_ptr != _tx_thread_current_ptr) _tx_thread_system_return();
}

VOID _tx_timer_interrupt(VOID)
{
    _tx_timer_system_clock++;
    if (_tx_timer_time_slice) {
        _tx_timer_time_slice--;
        if (_tx_timer_time_slice == 0) _tx_timer_expired_time_slice = TX_TRUE;
    }
    if (*_tx_timer_current_ptr) {
        _tx_timer_expired = TX_TRUE;
    } else {
        _tx_timer_current_ptr++;
        if (_tx_timer_current_ptr == _tx_timer_list_end) _tx_timer_current_ptr = _tx_timer_list_start;
    }
    if (_tx_timer_expired_time_slice || _tx_timer_expired) {
        if (_tx_timer_expired) _tx_timer_expiration_process();
        if (_tx_timer_expired_time_slice) _tx_thread_time_slice();
    }
}
void sim_port_tick(void) { _tx_timer_interrupt(); }

/* Ticks until the next timer-wheel slot that holds a timer (sleep/timeout), or UINT64_MAX. */
uint64_t sim_port_next_timer_ticks(void)
{
    if (!_tx_timer_current_ptr) return UINT64_MAX;
    TX_TIMER_INTERNAL **p = _tx_timer_current_ptr;
    size_t n = (size_t)(_tx_timer_list_end - _tx_timer_list_start);
    for (size_t k = 0; k < n; k++) {
        if (*p) return k ? k : 1;
        if (++p == _tx_timer_list_end) p = _tx_timer_list_start;
    }
    return UINT64_MAX;
}

/* Never returns: the scheduler loop (the Cortex-M PendSV handler and idle WFI loop). */
VOID _tx_thread_schedule(VOID)
{
    _tx_thread_preempt_disable = 0;
    sim_set_kernel_running();
    sim_primask = 0;
    for (;;) {
        in_scheduler = true;
        TX_THREAD *t = _tx_thread_execute_ptr;
        if (!t) {
            _tx_thread_current_ptr = TX_NULL;
            sim_core_idle();
            continue;
        }
        _tx_thread_current_ptr = t;
        t->tx_thread_run_count++;
        _tx_timer_time_slice = t->tx_thread_time_slice;
        port_thread_t *p = t->tx_thread_sim_port;
        in_scheduler = false;
        sim_primask = 0;
        swapcontext(&scheduler_ctx, &p->ctx);
        in_scheduler = true;
        /* back from the thread: it blocked, was preempted, or its entry function returned */
        if (_tx_thread_current_ptr == t) {
            if (t->tx_thread_time_slice && _tx_timer_time_slice) t->tx_thread_time_slice = _tx_timer_time_slice;
        }
        sim_irq_enable_point();
    }
}
