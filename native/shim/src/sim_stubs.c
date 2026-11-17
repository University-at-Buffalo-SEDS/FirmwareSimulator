/* Replaced middleware: USBX device stack (USB CDC debug console is not simulated; writes report "not ready"),
 * the HAL TIM6 timebase (the HAL tick is the virtual clock), Rust unwinding personality (SDK built panic=abort). */
#include "sim.h"
#include "main.h"
#include <stdlib.h>

__attribute__((weak)) void *cdc_acm;
__attribute__((weak)) unsigned int _ux_device_class_cdc_acm_write(void *cdc, unsigned char *buf, unsigned long len, unsigned long *actual)
{
    (void)cdc; (void)buf; (void)len; if (actual) *actual = 0; return 0xFF; /* UX_ERROR */
}
__attribute__((weak)) unsigned int _ux_device_class_cdc_acm_read(void *cdc, unsigned char *buf, unsigned long len, unsigned long *actual)
{
    (void)cdc; (void)buf; (void)len; if (actual) *actual = 0; return 0xFF;
}
#ifdef HAL_TIM_MODULE_ENABLED
__attribute__((weak)) TIM_HandleTypeDef htim6;
#endif
void rust_eh_personality(void) { abort(); }
void _Unwind_Resume(void *e) { (void)e; abort(); }

/* Linker-script symbols some boards read (startup/sysmem are replaced): the newlib heap (_sbrk) and stack top come
 * from one arena sized like the board's RAM heap, the image range (g_pfnVectors..__data_source_end) is the
 * application slot in the mapped flash. Retained-RAM sections (survive watchdog/software resets, not power cycles)
 * are gathered by the native link script into __sim_retained_start..end and persisted by sim_mmio.c. */
#define SIM_ARENA (512u * 1024u)
uint8_t sim_ram_arena[SIM_ARENA + 65536u] __attribute__((aligned(16)));
__asm__(".globl _estack\n.set _estack, sim_ram_arena + 589824\n");
static uint8_t *sim_brk = sim_ram_arena;
__attribute__((weak)) void *_sbrk(ptrdiff_t incr)
{
    if (sim_brk + incr > sim_ram_arena + SIM_ARENA || sim_brk + incr < sim_ram_arena) return (void *)-1;
    void *p = sim_brk; sim_brk += incr; return p;
}
