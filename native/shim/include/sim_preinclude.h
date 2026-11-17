/* Native SITL: force-included ahead of every firmware translation unit (gcc -include).
 * Supplies the CMSIS-Core compiler layer (cmsis_gcc.h) for an x86 host so the board's real CMSIS/device/HAL
 * headers compile unchanged; Cortex-M intrinsics become calls into the simulated core (sim_core.c).
 * Peripheral base addresses keep their real values: sim_mmio.c maps those ranges in the host process. */
#ifndef SIM_PREINCLUDE_H
#define SIM_PREINCLUDE_H
#ifndef __ASSEMBLER__
#include <stdint.h>
#include <stddef.h>

#ifdef __cplusplus
extern "C" {
#endif
/* Simulated core state (sim_core.c). */
extern volatile uint32_t sim_primask, sim_basepri, sim_faultmask, sim_ipsr, sim_control;
void sim_irq_enable_point(void);   /* interrupts were re-enabled: pending interrupts may run */
void sim_nop(void);                /* one CPU cycle; spin loops progress */
void sim_wfi(void);
void sim_barrier(void);
#ifdef __cplusplus
}
#endif

/* Firmware layout assertions describe the ARM EABI (8-byte aligned 64-bit members); the i386 host aligns them to 4.
 * Objects are only ever produced and consumed by the same build, so the assertions are not evaluated here. */
#if !defined(__cplusplus) && !defined(SIM_KEEP_STATIC_ASSERTS)
#define _Static_assert(...) _Static_assert(1, "ARM layout assertion (not evaluated in native SITL)")
#endif

/* Firmware file-scope names that the host C library also declares (newlib does not): keep them the board's. */
#ifndef SIM_SHIM_TU
#include <sys/select.h>   /* the C library's declaration keeps its name; later firmware uses get the alias */
#define select sim_fw_select
#endif

/* Skip the real ARM cmsis_gcc.h (its intrinsics are inline ARM assembly). */
#define __CMSIS_GCC_H

#ifndef __ASM
#define __ASM __asm
#endif
#ifndef __INLINE
#define __INLINE inline
#endif
#ifndef __STATIC_INLINE
#define __STATIC_INLINE static inline
#endif
#ifndef __STATIC_FORCEINLINE
#define __STATIC_FORCEINLINE __attribute__((always_inline)) static inline
#endif
#ifndef __NO_RETURN
#define __NO_RETURN __attribute__((__noreturn__))
#endif
#ifndef __USED
#define __USED __attribute__((used))
#endif
#ifndef __WEAK
#define __WEAK __attribute__((weak))
#endif
#ifndef __PACKED
#define __PACKED __attribute__((packed, aligned(1)))
#endif
#ifndef __PACKED_STRUCT
#define __PACKED_STRUCT struct __attribute__((packed, aligned(1)))
#endif
#ifndef __PACKED_UNION
#define __PACKED_UNION union __attribute__((packed, aligned(1)))
#endif
#ifndef __ALIGNED
#define __ALIGNED(x) __attribute__((aligned(x)))
#endif
#ifndef __RESTRICT
#define __RESTRICT __restrict
#endif
#ifndef __COMPILER_BARRIER
#define __COMPILER_BARRIER() __asm volatile("" ::: "memory")
#endif
struct __sim_u32w { uint32_t v; } __attribute__((packed));
struct __sim_u16w { uint16_t v; } __attribute__((packed));
#define __UNALIGNED_UINT32(x) (((struct __sim_u32w *)(void *)(x))->v)
#define __UNALIGNED_UINT16_WRITE(addr, val) (void)((((struct __sim_u16w *)(void *)(addr))->v) = (val))
#define __UNALIGNED_UINT16_READ(addr) (((const struct __sim_u16w *)(const void *)(addr))->v)
#define __UNALIGNED_UINT32_WRITE(addr, val) (void)((((struct __sim_u32w *)(void *)(addr))->v) = (val))
#define __UNALIGNED_UINT32_READ(addr) (((const struct __sim_u32w *)(const void *)(addr))->v)
#ifndef __PROGRAM_START
#define __PROGRAM_START main
#endif
#ifndef __INITIAL_SP
#define __INITIAL_SP __sim_initial_sp
#endif
#ifndef __STACK_LIMIT
#define __STACK_LIMIT __sim_stack_limit
#endif
#ifndef __VECTOR_TABLE
#define __VECTOR_TABLE __sim_vector_table
#endif
#ifndef __VECTOR_TABLE_ATTRIBUTE
#define __VECTOR_TABLE_ATTRIBUTE __attribute__((used))
#endif

__STATIC_FORCEINLINE void __enable_irq(void) { sim_primask = 0; sim_irq_enable_point(); }
__STATIC_FORCEINLINE void __disable_irq(void) { sim_primask = 1; }
__STATIC_FORCEINLINE uint32_t __get_CONTROL(void) { return sim_control; }
__STATIC_FORCEINLINE void __set_CONTROL(uint32_t v) { sim_control = v; }
__STATIC_FORCEINLINE uint32_t __get_IPSR(void) { return sim_ipsr; }
__STATIC_FORCEINLINE uint32_t __get_APSR(void) { return 0; }
__STATIC_FORCEINLINE uint32_t __get_xPSR(void) { return sim_ipsr; }
__STATIC_FORCEINLINE uint32_t __get_PSP(void) { return 0x20000400u; }
__STATIC_FORCEINLINE void __set_PSP(uint32_t v) { (void)v; }
__STATIC_FORCEINLINE uint32_t __get_MSP(void) { return 0x20000800u; }
__STATIC_FORCEINLINE void __set_MSP(uint32_t v) { (void)v; }
__STATIC_FORCEINLINE uint32_t __get_PRIMASK(void) { return sim_primask; }
__STATIC_FORCEINLINE void __set_PRIMASK(uint32_t v) { sim_primask = v & 1u; if (!sim_primask) sim_irq_enable_point(); }
__STATIC_FORCEINLINE void __enable_fault_irq(void) { sim_faultmask = 0; }
__STATIC_FORCEINLINE void __disable_fault_irq(void) { sim_faultmask = 1; }
__STATIC_FORCEINLINE uint32_t __get_BASEPRI(void) { return sim_basepri; }
__STATIC_FORCEINLINE void __set_BASEPRI(uint32_t v) { sim_basepri = v; if (!v) sim_irq_enable_point(); }
__STATIC_FORCEINLINE void __set_BASEPRI_MAX(uint32_t v) { if (v && (!sim_basepri || v < sim_basepri)) sim_basepri = v; }
__STATIC_FORCEINLINE uint32_t __get_FAULTMASK(void) { return sim_faultmask; }
__STATIC_FORCEINLINE void __set_FAULTMASK(uint32_t v) { sim_faultmask = v; }
__STATIC_FORCEINLINE uint32_t __get_PSPLIM(void) { return 0; }
__STATIC_FORCEINLINE void __set_PSPLIM(uint32_t v) { (void)v; }
__STATIC_FORCEINLINE uint32_t __get_MSPLIM(void) { return 0; }
__STATIC_FORCEINLINE void __set_MSPLIM(uint32_t v) { (void)v; }
__STATIC_FORCEINLINE uint32_t __get_FPSCR(void) { return 0; }
__STATIC_FORCEINLINE void __set_FPSCR(uint32_t v) { (void)v; }
#define __NOP() sim_nop()
#define __WFI() sim_wfi()
#define __WFE() sim_wfi()
#define __SEV() ((void)0)
#define __ISB() sim_barrier()
#define __DSB() sim_barrier()
#define __DMB() sim_barrier()
#define __BKPT(v) ((void)0)
__STATIC_FORCEINLINE uint32_t __REV(uint32_t v) { return __builtin_bswap32(v); }
__STATIC_FORCEINLINE uint32_t __REV16(uint32_t v) { return ((v & 0xff00ff00u) >> 8) | ((v & 0x00ff00ffu) << 8); }
__STATIC_FORCEINLINE int16_t __REVSH(int16_t v) { return (int16_t)__builtin_bswap16((uint16_t)v); }
__STATIC_FORCEINLINE uint32_t __ROR(uint32_t a, uint32_t b) { b %= 32u; return b ? (a >> b) | (a << (32u - b)) : a; }
__STATIC_FORCEINLINE uint32_t __RBIT(uint32_t v) {
  uint32_t r = 0; for (int i = 0; i < 32; i++) { r = (r << 1) | (v & 1u); v >>= 1; } return r;
}
__STATIC_FORCEINLINE uint8_t __CLZ(uint32_t v) { return v ? (uint8_t)__builtin_clz(v) : 32u; }
__STATIC_FORCEINLINE uint8_t __LDREXB(volatile uint8_t *p) { return *p; }
__STATIC_FORCEINLINE uint16_t __LDREXH(volatile uint16_t *p) { return *p; }
__STATIC_FORCEINLINE uint32_t __LDREXW(volatile uint32_t *p) { return *p; }
__STATIC_FORCEINLINE uint32_t __STREXB(uint8_t v, volatile uint8_t *p) { *p = v; return 0; }
__STATIC_FORCEINLINE uint32_t __STREXH(uint16_t v, volatile uint16_t *p) { *p = v; return 0; }
__STATIC_FORCEINLINE uint32_t __STREXW(uint32_t v, volatile uint32_t *p) { *p = v; return 0; }
__STATIC_FORCEINLINE void __CLREX(void) {}
__STATIC_FORCEINLINE uint8_t __LDAB(volatile uint8_t *p) { return *p; }
__STATIC_FORCEINLINE uint16_t __LDAH(volatile uint16_t *p) { return *p; }
__STATIC_FORCEINLINE uint32_t __LDA(volatile uint32_t *p) { return *p; }
__STATIC_FORCEINLINE void __STLB(uint8_t v, volatile uint8_t *p) { *p = v; }
__STATIC_FORCEINLINE void __STLH(uint16_t v, volatile uint16_t *p) { *p = v; }
__STATIC_FORCEINLINE void __STL(uint32_t v, volatile uint32_t *p) { *p = v; }
#define __LDAEXB __LDREXB
#define __LDAEXH __LDREXH
#define __LDAEX __LDREXW
#define __STLEXB __STREXB
#define __STLEXH __STREXH
#define __STLEX __STREXW
__STATIC_FORCEINLINE int32_t __SSAT(int32_t v, uint32_t sat) {
  if (sat >= 1u && sat <= 32u) { const int32_t max = (int32_t)((1U << (sat - 1U)) - 1U), min = -1 - max;
    if (v > max) return max; if (v < min) return min; } return v;
}
__STATIC_FORCEINLINE uint32_t __USAT(int32_t v, uint32_t sat) {
  if (sat <= 31u) { const uint32_t max = ((1U << sat) - 1U); if (v > (int32_t)max) return max; if (v < 0) return 0u; }
  return (uint32_t)v;
}
__STATIC_FORCEINLINE uint32_t __RRX(uint32_t v) { return v >> 1; }
__STATIC_FORCEINLINE uint8_t __LDRBT(volatile uint8_t *p) { return *p; }
__STATIC_FORCEINLINE uint16_t __LDRHT(volatile uint16_t *p) { return *p; }
__STATIC_FORCEINLINE uint32_t __LDRT(volatile uint32_t *p) { return *p; }
__STATIC_FORCEINLINE void __STRBT(uint8_t v, volatile uint8_t *p) { *p = v; }
__STATIC_FORCEINLINE void __STRHT(uint16_t v, volatile uint16_t *p) { *p = v; }
__STATIC_FORCEINLINE void __STRT(uint32_t v, volatile uint32_t *p) { *p = v; }
/* TrustZone (Cortex-M33) non-secure accessors. */
__STATIC_FORCEINLINE uint32_t __TZ_get_CONTROL_NS(void) { return 0; }
__STATIC_FORCEINLINE void __TZ_set_CONTROL_NS(uint32_t v) { (void)v; }
__STATIC_FORCEINLINE uint32_t __TZ_get_PRIMASK_NS(void) { return 0; }
__STATIC_FORCEINLINE void __TZ_set_PRIMASK_NS(uint32_t v) { (void)v; }
__STATIC_FORCEINLINE uint32_t __TZ_get_MSP_NS(void) { return 0; }
__STATIC_FORCEINLINE void __TZ_set_MSP_NS(uint32_t v) { (void)v; }
__STATIC_FORCEINLINE uint32_t __TZ_get_PSP_NS(void) { return 0; }
__STATIC_FORCEINLINE void __TZ_set_PSP_NS(uint32_t v) { (void)v; }
__STATIC_FORCEINLINE uint32_t __TZ_get_SP_NS(void) { return 0; }
__STATIC_FORCEINLINE void __TZ_set_SP_NS(uint32_t v) { (void)v; }
__STATIC_FORCEINLINE uint32_t __TZ_get_BASEPRI_NS(void) { return 0; }
__STATIC_FORCEINLINE void __TZ_set_BASEPRI_NS(uint32_t v) { (void)v; }
__STATIC_FORCEINLINE uint32_t __TZ_get_FAULTMASK_NS(void) { return 0; }
__STATIC_FORCEINLINE void __TZ_set_FAULTMASK_NS(uint32_t v) { (void)v; }
__STATIC_FORCEINLINE uint32_t __TZ_get_PSPLIM_NS(void) { return 0; }
__STATIC_FORCEINLINE void __TZ_set_PSPLIM_NS(uint32_t v) { (void)v; }
__STATIC_FORCEINLINE uint32_t __TZ_get_MSPLIM_NS(void) { return 0; }
__STATIC_FORCEINLINE void __TZ_set_MSPLIM_NS(uint32_t v) { (void)v; }
/* DSP/SIMD intrinsics are not used by the boards' C; leave them undefined so a use fails to link loudly. */
#endif /* __ASSEMBLER__ */
#endif
