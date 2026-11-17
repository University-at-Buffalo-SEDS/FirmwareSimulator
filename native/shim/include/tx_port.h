/* Native SITL ThreadX port: the real ThreadX kernel (common/src) on a deterministic virtual-time core.
 * Threads are host coroutines (sim_threadx_port.c); interrupts run only at simulated interrupt points
 * (interrupt re-enable, HAL/shim calls, CPU-cost accounting), so scheduling is reproducible.
 * Replaces the Cortex-M port tx_port.h (found first on the include path). */
#ifndef TX_PORT_H
#define TX_PORT_H

#ifdef TX_INCLUDE_USER_DEFINE_FILE
#include "tx_user.h"
#endif
#include <stdlib.h>
#include <string.h>

#define VOID void
typedef char CHAR;
typedef unsigned char UCHAR;
typedef int INT;
typedef unsigned int UINT;
typedef long LONG;
typedef unsigned long ULONG;
typedef unsigned long long ULONG64;
typedef short SHORT;
typedef unsigned short USHORT;
#define ULONG64_DEFINED

#ifndef TX_MAX_PRIORITIES
#define TX_MAX_PRIORITIES 32
#endif
#ifndef TX_MINIMUM_STACK
#define TX_MINIMUM_STACK 200
#endif
#ifndef TX_TIMER_THREAD_STACK_SIZE
#define TX_TIMER_THREAD_STACK_SIZE 1024
#endif
#ifndef TX_TIMER_THREAD_PRIORITY
#define TX_TIMER_THREAD_PRIORITY 0
#endif
#define TX_INT_DISABLE 1
#define TX_INT_ENABLE 0

extern volatile unsigned int sim_tx_trace_time;
#ifndef TX_TRACE_TIME_SOURCE
#define TX_TRACE_TIME_SOURCE sim_tx_trace_time
#endif
#ifndef TX_TRACE_TIME_MASK
#define TX_TRACE_TIME_MASK 0xFFFFFFFFUL
#endif
#define TX_PORT_SPECIFIC_BUILD_OPTIONS (0)
#define TX_INLINE_INITIALIZATION
#ifdef TX_ENABLE_STACK_CHECKING
#undef TX_DISABLE_STACK_FILLING
#endif

/* Host coroutine handle (sim_threadx_port.c). */
#define TX_THREAD_EXTENSION_0 VOID *tx_thread_sim_port;
#define TX_THREAD_EXTENSION_1
#define TX_THREAD_EXTENSION_2
#define TX_THREAD_EXTENSION_3
#define TX_BLOCK_POOL_EXTENSION
#define TX_BYTE_POOL_EXTENSION
#define TX_EVENT_FLAGS_GROUP_EXTENSION
#define TX_MUTEX_EXTENSION
#define TX_QUEUE_EXTENSION
#define TX_SEMAPHORE_EXTENSION
#define TX_TIMER_EXTENSION
#ifndef TX_THREAD_USER_EXTENSION
#define TX_THREAD_USER_EXTENSION
#endif
#define TX_THREAD_CREATE_EXTENSION(thread_ptr)
#define TX_THREAD_DELETE_EXTENSION(thread_ptr) sim_tx_thread_delete_extension(thread_ptr);
#define TX_THREAD_COMPLETED_EXTENSION(thread_ptr)
#define TX_THREAD_TERMINATED_EXTENSION(thread_ptr)
#define TX_BLOCK_POOL_CREATE_EXTENSION(pool_ptr)
#define TX_BYTE_POOL_CREATE_EXTENSION(pool_ptr)
#define TX_EVENT_FLAGS_GROUP_CREATE_EXTENSION(group_ptr)
#define TX_MUTEX_CREATE_EXTENSION(mutex_ptr)
#define TX_QUEUE_CREATE_EXTENSION(queue_ptr)
#define TX_SEMAPHORE_CREATE_EXTENSION(semaphore_ptr)
#define TX_TIMER_CREATE_EXTENSION(timer_ptr)
#define TX_BLOCK_POOL_DELETE_EXTENSION(pool_ptr)
#define TX_BYTE_POOL_DELETE_EXTENSION(pool_ptr)
#define TX_EVENT_FLAGS_GROUP_DELETE_EXTENSION(group_ptr)
#define TX_MUTEX_DELETE_EXTENSION(mutex_ptr)
#define TX_QUEUE_DELETE_EXTENSION(queue_ptr)
#define TX_SEMAPHORE_DELETE_EXTENSION(semaphore_ptr)
#define TX_TIMER_DELETE_EXTENSION(timer_ptr)

extern volatile unsigned int sim_primask, sim_ipsr;
void sim_irq_enable_point(void);
struct TX_THREAD_STRUCT;
void sim_tx_thread_delete_extension(struct TX_THREAD_STRUCT *thread_ptr);

#define TX_THREAD_GET_SYSTEM_STATE() (_tx_thread_system_state | sim_ipsr)
/* Before the scheduler starts, as on Cortex-M, no thread may be scheduled from tx_application_define. */
#define TX_PORT_SPECIFIC_POST_INITIALIZATION _tx_thread_preempt_disable++;
#define TX_LOWEST_SET_BIT_CALCULATE(m, b) (b) = (UINT)__builtin_ctz((unsigned int)(m));

static inline unsigned int __sim_disable_interrupts(void)
{
    unsigned int posture = sim_primask;
    sim_primask = 1u;
    return posture;
}
static inline void __sim_restore_interrupt(unsigned int posture)
{
    sim_primask = posture & 1u;
    if (!sim_primask)
        sim_irq_enable_point();
}
#define TX_INTERRUPT_SAVE_AREA UINT interrupt_save;
#define TX_DISABLE interrupt_save = __sim_disable_interrupts();
#define TX_RESTORE __sim_restore_interrupt(interrupt_save);

#define _tx_thread_system_return_inline _tx_thread_system_return

#ifdef TX_THREAD_INIT
CHAR _tx_version_id[] = "ThreadX native SITL port (virtual-time coroutine core)";
#else
extern CHAR _tx_version_id[];
#endif
#endif
