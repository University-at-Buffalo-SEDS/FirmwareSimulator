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
