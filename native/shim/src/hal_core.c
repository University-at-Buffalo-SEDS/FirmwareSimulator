/* HAL boundary: core/system services, RCC/PWR/NVIC, GPIO/EXTI, timers, flash, ADC/DAC, I2C, SPI, SD, USB, caches.
 * Behaviour is modelled at the HAL API (handles, states, callbacks) so the board's application code, HAL
 * callback implementations and CubeMX init run unchanged. Register-level effects firmware relies on are
 * mirrored into the mapped peripheral memory (ODR/IDR, DMA counters, ...). */
#include "sim.h"
#include "main.h"
#include <stdio.h>
#include <string.h>

/* ---------------------------------------------------------------- system_stm32xxxx.c replacement */
#if defined(STM32U5) || defined(STM32U585xx) || defined(STM32U575xx)
uint32_t SystemCoreClock = 160000000U;
const uint32_t MSIRangeTable[16] = {48000000U, 24000000U, 16000000U, 12000000U, 4000000U, 2000000U, 1330000U, 1000000U, 3072000U, 1536000U, 1024000U, 768000U, 400000U, 200000U, 133000U, 100000U};
#elif defined(STM32H5) || defined(STM32H523xx) || defined(STM32H563xx)
uint32_t SystemCoreClock = 250000000U;
#elif defined(STM32H7) || defined(STM32H743xx) || defined(STM32H7S3xx)
uint32_t SystemCoreClock = 400000000U;
uint32_t SystemD2Clock = 200000000U;
#else
uint32_t SystemCoreClock = 170000000U;
#endif
const uint8_t AHBPrescTable[16] = {0, 0, 0, 0, 0, 0, 0, 0, 1, 2, 3, 4, 6, 7, 8, 9};
const uint8_t APBPrescTable[8] = {0, 0, 0, 0, 1, 2, 3, 4};
void SystemInit(void) {}
void SystemCoreClockUpdate(void) {}
extern volatile uint32_t uwTick;
extern uint32_t uwTickPrio, uwTickFreq;
extern uint32_t sim_cycles_per_us;

/* ---------------------------------------------------------------- HAL core */
__attribute__((weak)) void HAL_MspInit(void) {}
__attribute__((weak)) void HAL_MspDeInit(void) {}
HAL_StatusTypeDef HAL_Init(void) { sim_cycles_per_us = SystemCoreClock / 1000000u; HAL_MspInit(); return HAL_OK; }
HAL_StatusTypeDef HAL_DeInit(void) { HAL_MspDeInit(); return HAL_OK; }
HAL_StatusTypeDef HAL_InitTick(uint32_t prio) { uwTickPrio = prio; return HAL_OK; }
uint32_t HAL_GetTick(void) { sim_charge_ns(40); uwTick = sim_now_ms(); return uwTick; }
void HAL_IncTick(void) { uwTick = sim_now_ms(); }
void HAL_Delay(uint32_t ms) { sim_busy_wait_ns((uint64_t)(ms + (ms < 0xFFFFFFFFu ? 1u : 0u)) * SIM_NS_PER_MS); }
void HAL_SuspendTick(void) {}
void HAL_ResumeTick(void) {}
uint32_t HAL_GetTickPrio(void) { return uwTickPrio; }
HAL_StatusTypeDef HAL_SetTickFreq(uint32_t f) { uwTickFreq = f; return HAL_OK; }
uint32_t HAL_GetTickFreq(void) { return uwTickFreq; }
uint32_t HAL_GetHalVersion(void) { return 0x01020000u; }
uint32_t HAL_GetREVID(void) { return 0x2000u; }
uint32_t HAL_GetDEVID(void) { return 0x468u; }
#ifdef UID_BASE
uint32_t HAL_GetUIDw0(void) { return *(volatile uint32_t *)UID_BASE; }
uint32_t HAL_GetUIDw1(void) { return *(volatile uint32_t *)(UID_BASE + 4); }
uint32_t HAL_GetUIDw2(void) { return *(volatile uint32_t *)(UID_BASE + 8); }
#endif
void HAL_DBGMCU_EnableDBGSleepMode(void) {}
void HAL_DBGMCU_EnableDBGStopMode(void) {}
void HAL_DBGMCU_EnableDBGStandbyMode(void) {}

/* RCC */
HAL_StatusTypeDef HAL_RCC_OscConfig(const RCC_OscInitTypeDef *o) { (void)o; return HAL_OK; }
HAL_StatusTypeDef HAL_RCC_ClockConfig(const RCC_ClkInitTypeDef *c, uint32_t lat) { (void)c; (void)lat; return HAL_OK; }
HAL_StatusTypeDef HAL_RCC_DeInit(void) { return HAL_OK; }
uint32_t HAL_RCC_GetSysClockFreq(void) { return SystemCoreClock; }
uint32_t HAL_RCC_GetHCLKFreq(void) { return SystemCoreClock; }
uint32_t HAL_RCC_GetPCLK1Freq(void) { return SystemCoreClock; }
uint32_t HAL_RCC_GetPCLK2Freq(void) { return SystemCoreClock; }
#if defined(RCC_CFGR2_PPRE3) || defined(STM32U5) || defined(STM32H5)
uint32_t HAL_RCC_GetPCLK3Freq(void) { return SystemCoreClock; }
#endif
void HAL_RCC_GetClockConfig(RCC_ClkInitTypeDef *c, uint32_t *lat)
{
    memset(c, 0, sizeof *c);
    c->ClockType = 0xF; c->SYSCLKSource = RCC_SYSCLKSOURCE_PLLCLK; c->AHBCLKDivider = RCC_SYSCLK_DIV1;
    c->APB1CLKDivider = RCC_HCLK_DIV1; c->APB2CLKDivider = RCC_HCLK_DIV1;
    if (lat) *lat = 4;
}
void HAL_RCC_GetOscConfig(RCC_OscInitTypeDef *o) { memset(o, 0, sizeof *o); }
HAL_StatusTypeDef HAL_RCCEx_PeriphCLKConfig(const RCC_PeriphCLKInitTypeDef *p) { (void)p; return HAL_OK; }
void HAL_RCCEx_GetPeriphCLKConfig(RCC_PeriphCLKInitTypeDef *p) { (void)p; }
void HAL_RCC_MCOConfig(uint32_t a, uint32_t b, uint32_t c) { (void)a; (void)b; (void)c; }
void HAL_RCC_EnableCSS(void) {}

/* PWR (signatures differ by family; the board only calls them for configuration) */
#ifdef HAL_PWR_MODULE_ENABLED
HAL_StatusTypeDef HAL_PWREx_ControlVoltageScaling(uint32_t v) { (void)v; return HAL_OK; }
#if defined(PWR_CR3_UCPD_DBDIS) || defined(STM32G4)
void HAL_PWREx_DisableUCPDDeadBattery(void) {}
#endif
#if defined(STM32H5)
void HAL_PWREx_EnableVddUSB(void) {}
#endif
#if defined(PWR_SVMCR_USV) || defined(STM32U5)
void HAL_PWREx_EnableVddUSB(void) {}
void HAL_PWREx_EnableVddA(void) {}
void HAL_PWREx_EnableVddIO2(void) {}
HAL_StatusTypeDef HAL_PWREx_ConfigSupply(uint32_t s) { (void)s; return HAL_OK; }
#endif
void HAL_PWR_EnableBkUpAccess(void) {}
void HAL_PWR_DisableBkUpAccess(void) {}
#endif

/* NVIC / Cortex */
void HAL_NVIC_SetPriorityGrouping(uint32_t g) { (void)g; }
void HAL_NVIC_SetPriority(IRQn_Type n, uint32_t p, uint32_t s) { (void)n; (void)p; (void)s; }
void HAL_NVIC_EnableIRQ(IRQn_Type n) { sim_nvic_set((int)n, true); sim_yield_point(); }
void HAL_NVIC_DisableIRQ(IRQn_Type n) { sim_nvic_set((int)n, false); }
void HAL_NVIC_ClearPendingIRQ(IRQn_Type n) { (void)n; }
void HAL_NVIC_SetPendingIRQ(IRQn_Type n) { (void)n; }
uint32_t HAL_NVIC_GetPendingIRQ(IRQn_Type n) { (void)n; return 0; }
void HAL_NVIC_SystemReset(void) { sim_reset(SIM_RESET_SOFT); }
uint32_t HAL_SYSTICK_Config(uint32_t t) { (void)t; return 0; }
void HAL_SYSTICK_CLKSourceConfig(uint32_t s) { (void)s; }
void HAL_MPU_Enable(uint32_t c) { (void)c; }
#if defined(STM32H5) || defined(STM32U5)
void HAL_MPU_ConfigRegion(const MPU_Region_InitTypeDef *const r) { (void)r; }
void HAL_MPU_ConfigMemoryAttributes(const MPU_Attributes_InitTypeDef *const a) { (void)a; }
#endif
void HAL_MPU_Disable(void) {}

/* ---------------------------------------------------------------- GPIO / EXTI */
int sim_pin_input(unsigned global_index);
static unsigned gpio_port_index(const GPIO_TypeDef *g) { return (unsigned)(((uintptr_t)g - (uintptr_t)GPIOA) / 0x400u) & 15u; }
static unsigned pin_bit(uint16_t pin) { return pin ? (unsigned)__builtin_ctz(pin) : 0; }
void HAL_GPIO_Init(GPIO_TypeDef *g, const GPIO_InitTypeDef *i) { (void)g; (void)i; }
void HAL_GPIO_DeInit(GPIO_TypeDef *g, uint32_t p) { (void)g; (void)p; }
__attribute__((weak)) void sim_gpio_changed(unsigned global_index, int value) { (void)global_index; (void)value; }
static void gpio_report(GPIO_TypeDef *g, uint16_t pins, uint32_t before)
{
    uint32_t after = g->ODR, changed = (before ^ after) & pins;
    for (unsigned b = 0; changed; b++, changed >>= 1) {
        if (!(changed & 1u)) continue;
        sim_gpio_changed(gpio_port_index(g) * 16 + b, (int)((after >> b) & 1u));
        struct __attribute__((packed)) { uint64_t t; uint8_t port, pin, value, pad; } m = {sim_local_to_global(sim_now_ns()), (uint8_t)gpio_port_index(g), (uint8_t)b, (uint8_t)((after >> b) & 1u), 0};
        sim_send(SIM_MSG_GPIO, &m, sizeof m);
    }
}
static void gpio_idr_refresh(GPIO_TypeDef *g)
{
    uint32_t idr = g->ODR & 0xFFFFu; unsigned port = gpio_port_index(g);
    for (unsigned b = 0; b < 16; b++) {
        int v = sim_pin_input(port * 16 + b);
        if (v >= 0) idr = v ? (idr | (1u << b)) : (idr & ~(1u << b));
    }
    g->IDR = idr;
}
void HAL_GPIO_WritePin(GPIO_TypeDef *g, uint16_t pin, GPIO_PinState s)
{
    sim_charge_ns(50);
    uint32_t before = g->ODR;
    if (s != GPIO_PIN_RESET) g->ODR = before | pin; else g->ODR = before & ~(uint32_t)pin;
    gpio_report(g, pin, before);
    gpio_idr_refresh(g);
}
void HAL_GPIO_TogglePin(GPIO_TypeDef *g, uint16_t pin)
{
    uint32_t before = g->ODR; g->ODR = before ^ pin; gpio_report(g, pin, before); gpio_idr_refresh(g);
}
GPIO_PinState HAL_GPIO_ReadPin(const GPIO_TypeDef *g, uint16_t pin)
{
    sim_charge_ns(50);
    unsigned port = gpio_port_index(g);
    int v = sim_pin_input(port * 16 + pin_bit(pin));
    if (v >= 0) return v ? GPIO_PIN_SET : GPIO_PIN_RESET;
    return (g->ODR & pin) ? GPIO_PIN_SET : GPIO_PIN_RESET;
}
HAL_StatusTypeDef HAL_GPIO_LockPin(GPIO_TypeDef *g, uint16_t p) { (void)g; (void)p; return HAL_OK; }
__attribute__((weak)) void HAL_GPIO_EXTI_Callback(uint16_t p) { (void)p; }
__attribute__((weak)) void HAL_GPIO_EXTI_Rising_Callback(uint16_t p) { (void)p; }
__attribute__((weak)) void HAL_GPIO_EXTI_Falling_Callback(uint16_t p) { (void)p; }
void HAL_GPIO_EXTI_IRQHandler(uint16_t p) { (void)p; }
/* Device models raise an edge on a pin (ISR context). */
void sim_gpio_edge(uint16_t pin, int rising)
{
#if defined(EXTI_RPR1_RPIF0) || defined(STM32U5) || defined(STM32H5)
    if (rising) HAL_GPIO_EXTI_Rising_Callback(pin); else HAL_GPIO_EXTI_Falling_Callback(pin);
#else
    (void)rising; HAL_GPIO_EXTI_Callback(pin);
#endif
}

/* ---------------------------------------------------------------- timers */
#ifdef HAL_TIM_MODULE_ENABLED
__attribute__((weak)) void HAL_TIM_PeriodElapsedCallback(TIM_HandleTypeDef *h) { (void)h; }
__attribute__((weak)) void HAL_TIM_Base_MspInit(TIM_HandleTypeDef *h) { (void)h; }
__attribute__((weak)) void HAL_TIM_Base_MspDeInit(TIM_HandleTypeDef *h) { (void)h; }
__attribute__((weak)) void HAL_TIM_PWM_MspInit(TIM_HandleTypeDef *h) { (void)h; }
__attribute__((weak)) void HAL_TIM_PWM_MspDeInit(TIM_HandleTypeDef *h) { (void)h; }
__attribute__((weak)) void HAL_TIM_MspPostInit(TIM_HandleTypeDef *h) { (void)h; }
static uint64_t tim_period_ns(TIM_HandleTypeDef *h)
{
    uint64_t psc = h->Instance->PSC ? h->Instance->PSC : h->Init.Prescaler, arr = h->Instance->ARR ? h->Instance->ARR : h->Init.Period;
    uint64_t ns = (psc + 1) * (arr + 1) * 1000000000ull / SystemCoreClock;
    return ns ? ns : 1000;
}
static void tim_isr(void *arg, uintptr_t a2)
{
    (void)a2;
    TIM_HandleTypeDef *h = arg;
    if (!(h->Instance->DIER & TIM_DIER_UIE) || !(h->Instance->CR1 & TIM_CR1_CEN)) return;
    sim_schedule(sim_now_ns() + tim_period_ns(h), -1, tim_isr, h, 0);
    HAL_TIM_PeriodElapsedCallback(h);
}
static void tim_load(TIM_HandleTypeDef *h) { h->Instance->PSC = h->Init.Prescaler; h->Instance->ARR = h->Init.Period; }
HAL_StatusTypeDef HAL_TIM_Base_Init(TIM_HandleTypeDef *h) { if (h->State == HAL_TIM_STATE_RESET) { h->Lock = HAL_UNLOCKED; HAL_TIM_Base_MspInit(h); } tim_load(h); h->State = HAL_TIM_STATE_READY; return HAL_OK; }
HAL_StatusTypeDef HAL_TIM_Base_DeInit(TIM_HandleTypeDef *h) { sim_cancel(tim_isr, h); HAL_TIM_Base_MspDeInit(h); h->State = HAL_TIM_STATE_RESET; return HAL_OK; }
HAL_StatusTypeDef HAL_TIM_Base_Start(TIM_HandleTypeDef *h) { h->Instance->CR1 |= TIM_CR1_CEN; h->State = HAL_TIM_STATE_BUSY; return HAL_OK; }
HAL_TIM_StateTypeDef HAL_TIM_Base_GetState(const TIM_HandleTypeDef *h) { return h->State; }
HAL_StatusTypeDef HAL_TIM_Base_Stop(TIM_HandleTypeDef *h) { h->Instance->CR1 &= ~TIM_CR1_CEN; h->State = HAL_TIM_STATE_READY; return HAL_OK; }
HAL_StatusTypeDef HAL_TIM_Base_Start_IT(TIM_HandleTypeDef *h)
{
    h->Instance->DIER |= TIM_DIER_UIE; h->Instance->CR1 |= TIM_CR1_CEN; h->State = HAL_TIM_STATE_BUSY;
    sim_cancel(tim_isr, h);
    sim_schedule(sim_now_ns() + tim_period_ns(h), -1, tim_isr, h, 0);
    return HAL_OK;
}
HAL_StatusTypeDef HAL_TIM_Base_Stop_IT(TIM_HandleTypeDef *h) { h->Instance->DIER &= ~TIM_DIER_UIE; h->Instance->CR1 &= ~TIM_CR1_CEN; sim_cancel(tim_isr, h); h->State = HAL_TIM_STATE_READY; return HAL_OK; }
HAL_StatusTypeDef HAL_TIM_PWM_Init(TIM_HandleTypeDef *h) { if (h->State == HAL_TIM_STATE_RESET) { h->Lock = HAL_UNLOCKED; HAL_TIM_PWM_MspInit(h); } tim_load(h); h->State = HAL_TIM_STATE_READY; return HAL_OK; }
HAL_StatusTypeDef HAL_TIM_PWM_DeInit(TIM_HandleTypeDef *h) { HAL_TIM_PWM_MspDeInit(h); h->State = HAL_TIM_STATE_RESET; return HAL_OK; }
static volatile uint32_t *ccr(TIM_HandleTypeDef *h, uint32_t ch)
{
    switch (ch) { case TIM_CHANNEL_1: return &h->Instance->CCR1; case TIM_CHANNEL_2: return &h->Instance->CCR2;
    case TIM_CHANNEL_3: return &h->Instance->CCR3; case TIM_CHANNEL_4: return &h->Instance->CCR4; default: return &h->Instance->CCR1; }
}
HAL_StatusTypeDef HAL_TIM_PWM_ConfigChannel(TIM_HandleTypeDef *h, const TIM_OC_InitTypeDef *c, uint32_t ch) { *ccr(h, ch) = c->Pulse; return HAL_OK; }
HAL_StatusTypeDef HAL_TIM_OC_ConfigChannel(TIM_HandleTypeDef *h, const TIM_OC_InitTypeDef *c, uint32_t ch) { *ccr(h, ch) = c->Pulse; return HAL_OK; }
static void pwm_report(TIM_HandleTypeDef *h, uint32_t ch, int on)
{
    struct __attribute__((packed)) { uint64_t t; uint8_t port, pin, value, pad; } m = {sim_local_to_global(sim_now_ns()), (uint8_t)(0x80 | ((((uintptr_t)h->Instance) >> 10) & 0x3F)), (uint8_t)(ch / 4), (uint8_t)on, 0};
    sim_send(SIM_MSG_GPIO, &m, sizeof m);
}
/* A PWM output started with the update interrupt enabled (DIER.UIE set through the register macros, as the AB's
 * stepper driver counts its steps in the TIM1 update callback) gets its update interrupts like Base_Start_IT. */
HAL_StatusTypeDef HAL_TIM_PWM_Start(TIM_HandleTypeDef *h, uint32_t ch)
{
    h->Instance->CR1 |= TIM_CR1_CEN; pwm_report(h, ch, 1);
    if (h->Instance->DIER & TIM_DIER_UIE) {
        sim_cancel(tim_isr, h);
        sim_schedule(sim_now_ns() + tim_period_ns(h), -1, tim_isr, h, 0);
    }
    return HAL_OK;
}
HAL_StatusTypeDef HAL_TIM_PWM_Stop(TIM_HandleTypeDef *h, uint32_t ch) { pwm_report(h, ch, 0); return HAL_OK; }
HAL_StatusTypeDef HAL_TIM_PWM_Start_IT(TIM_HandleTypeDef *h, uint32_t ch) { return HAL_TIM_PWM_Start(h, ch); }
HAL_StatusTypeDef HAL_TIM_PWM_Stop_IT(TIM_HandleTypeDef *h, uint32_t ch) { return HAL_TIM_PWM_Stop(h, ch); }
HAL_StatusTypeDef HAL_TIM_OC_Start(TIM_HandleTypeDef *h, uint32_t ch) { return HAL_TIM_PWM_Start(h, ch); }
HAL_StatusTypeDef HAL_TIM_OC_Stop(TIM_HandleTypeDef *h, uint32_t ch) { return HAL_TIM_PWM_Stop(h, ch); }
HAL_StatusTypeDef HAL_TIM_OC_Init(TIM_HandleTypeDef *h) { return HAL_TIM_PWM_Init(h); }
HAL_StatusTypeDef HAL_TIM_ConfigClockSource(TIM_HandleTypeDef *h, const TIM_ClockConfigTypeDef *c) { (void)h; (void)c; return HAL_OK; }
HAL_StatusTypeDef HAL_TIMEx_MasterConfigSynchronization(TIM_HandleTypeDef *h, const TIM_MasterConfigTypeDef *c) { (void)h; (void)c; return HAL_OK; }
HAL_StatusTypeDef HAL_TIMEx_ConfigBreakDeadTime(TIM_HandleTypeDef *h, const TIM_BreakDeadTimeConfigTypeDef *c) { (void)h; (void)c; return HAL_OK; }
HAL_StatusTypeDef HAL_TIMEx_PWMN_Start(TIM_HandleTypeDef *h, uint32_t ch) { return HAL_TIM_PWM_Start(h, ch); }
HAL_StatusTypeDef HAL_TIMEx_PWMN_Stop(TIM_HandleTypeDef *h, uint32_t ch) { return HAL_TIM_PWM_Stop(h, ch); }
void HAL_TIM_IRQHandler(TIM_HandleTypeDef *h) { (void)h; }
#endif

/* ---------------------------------------------------------------- flash */
#ifdef HAL_FLASH_MODULE_ENABLED
HAL_StatusTypeDef HAL_FLASH_Unlock(void) { return HAL_OK; }
HAL_StatusTypeDef HAL_FLASH_Lock(void) { return HAL_OK; }
HAL_StatusTypeDef HAL_FLASH_OB_Unlock(void) { return HAL_OK; }
HAL_StatusTypeDef HAL_FLASH_OB_Lock(void) { return HAL_OK; }
uint32_t HAL_FLASH_GetError(void) { return 0; }
#if defined(FLASH_TYPEPROGRAM_QUADWORD)
HAL_StatusTypeDef HAL_FLASH_Program(uint32_t type, uint32_t addr, uint32_t data)
#else
HAL_StatusTypeDef HAL_FLASH_Program(uint32_t type, uint32_t addr, uint64_t data)
#endif
{
    (void)type;
#if defined(FLASH_TYPEPROGRAM_QUADWORD)
    /* U5/H5: data is the address of the 16-byte source */
    const uint8_t *src = (const uint8_t *)(uintptr_t)(uint32_t)data; uint32_t n = 16;
#else
    uint64_t v = data; const uint8_t *src = (const uint8_t *)&v; uint32_t n = 8;
#endif
    volatile uint8_t *dst = (volatile uint8_t *)(uintptr_t)addr;
    for (uint32_t i = 0; i < n; i++) dst[i] &= src[i];  /* NOR: program clears bits */
    sim_busy_wait_ns(85000);
    return HAL_OK;
}
HAL_StatusTypeDef HAL_FLASHEx_Erase(FLASH_EraseInitTypeDef *e, uint32_t *err)
{
#if defined(FLASH_SECTOR_SIZE)
    uint32_t page = e->Sector, n = e->NbSectors, base = 0x08000000u, unit = FLASH_SECTOR_SIZE;
#else
    uint32_t page = e->Page, n = e->NbPages, base = 0x08000000u, unit = FLASH_PAGE_SIZE;
#endif
#if defined(FLASH_BANK_2) && defined(FLASH_BANK_SIZE)
    if (e->Banks == FLASH_BANK_2) base += FLASH_BANK_SIZE;
#endif
    if (e->TypeErase == FLASH_TYPEERASE_MASSERASE) { page = 0; n = (uint32_t)sim_env_long("SIM_FLASH_SIZE", 524288) / unit; }
    memset((void *)(uintptr_t)(base + page * unit), 0xFF, (size_t)n * unit);
    if (err) *err = 0xFFFFFFFFu;
    sim_busy_wait_ns((uint64_t)n * 22000000ull);
    return HAL_OK;
}
#endif

/* ---------------------------------------------------------------- ADC */
#ifdef HAL_ADC_MODULE_ENABLED
__attribute__((weak)) void HAL_ADC_ConvCpltCallback(ADC_HandleTypeDef *h) { (void)h; }
__attribute__((weak)) void HAL_ADC_ConvHalfCpltCallback(ADC_HandleTypeDef *h) { (void)h; }
__attribute__((weak)) void HAL_ADC_ErrorCallback(ADC_HandleTypeDef *h) { (void)h; }
__attribute__((weak)) void HAL_ADC_MspInit(ADC_HandleTypeDef *h) { (void)h; }
__attribute__((weak)) void HAL_ADC_MspDeInit(ADC_HandleTypeDef *h) { (void)h; }
typedef struct { ADC_HandleTypeDef *h; char name[8]; uint32_t rank_ch[16]; uint32_t last_ch; void *buf; uint32_t len, wide; int running; } adcs_t;
static adcs_t adcs_[6];
static adcs_t *adc_of(ADC_HandleTypeDef *h)
{
    for (int i = 0; i < 6; i++) if (adcs_[i].h == h) return &adcs_[i];
    for (int i = 0; i < 6; i++) if (!adcs_[i].h) {
        adcs_t *a = &adcs_[i]; a->h = h; strcpy(a->name, "adc?");
#ifdef ADC1
        if (h->Instance == ADC1) strcpy(a->name, "adc1");
#endif
#ifdef ADC2
        if (h->Instance == ADC2) strcpy(a->name, "adc2");
#endif
#ifdef ADC3
        if (h->Instance == ADC3) strcpy(a->name, "adc3");
#endif
#ifdef ADC4
        if (h->Instance == (void *)ADC4) strcpy(a->name, "adc4");
#endif
#ifdef ADC5
        if (h->Instance == ADC5) strcpy(a->name, "adc5");
#endif
        return a;
    }
    return &adcs_[0];
}
static uint32_t ch_decimal(uint32_t ch)
{
#ifdef __LL_ADC_CHANNEL_TO_DECIMAL_NB
    return __LL_ADC_CHANNEL_TO_DECIMAL_NB(ch);
#else
    return ch & 0x1F;
#endif
}
static uint32_t adc_value(adcs_t *a, uint32_t ch)
{
    int32_t v = sim_adc_sample(a->name, ch, -1);
    if (v >= 0) return (uint32_t)v;
#if defined(VREFINT_CAL_ADDR) && defined(LL_ADC_CHANNEL_VREFINT)
    if (ch == ch_decimal(LL_ADC_CHANNEL_VREFINT)) return *(volatile uint16_t *)VREFINT_CAL_ADDR;
#endif
#if defined(TEMPSENSOR_CAL1_ADDR) && defined(LL_ADC_CHANNEL_TEMPSENSOR_ADC1)
    if (ch == ch_decimal(LL_ADC_CHANNEL_TEMPSENSOR_ADC1)) return *(volatile uint16_t *)TEMPSENSOR_CAL1_ADDR - 8;
#endif
    return 2048;
}
HAL_StatusTypeDef HAL_ADC_Init(ADC_HandleTypeDef *h) { if (h->State == HAL_ADC_STATE_RESET) { h->Lock = HAL_UNLOCKED; HAL_ADC_MspInit(h); } adc_of(h); h->State = HAL_ADC_STATE_READY; h->ErrorCode = 0; return HAL_OK; }
HAL_StatusTypeDef HAL_ADC_DeInit(ADC_HandleTypeDef *h) { HAL_ADC_MspDeInit(h); h->State = HAL_ADC_STATE_RESET; return HAL_OK; }
#if defined(STM32U5)
#define SIM_ADC_CONF_CONST
#define SIM_ADC_BUF_CONST const
#else
#define SIM_ADC_CONF_CONST const
#define SIM_ADC_BUF_CONST
#endif
HAL_StatusTypeDef HAL_ADC_ConfigChannel(ADC_HandleTypeDef *h, SIM_ADC_CONF_CONST ADC_ChannelConfTypeDef *c)
{
    adcs_t *a = adc_of(h);
    uint32_t rank = c->Rank;
#ifdef ADC_REGULAR_RANK_1
    if (rank >= ADC_REGULAR_RANK_1) rank = (rank >> 6) & 0x1F ? ((rank >> 6) & 0xF) : rank;
#endif
    uint32_t idx = rank ? (rank - 1) & 15 : 0;
    a->rank_ch[idx] = ch_decimal(c->Channel); a->last_ch = a->rank_ch[idx];
    return HAL_OK;
}
#if defined(STM32U5)
HAL_StatusTypeDef HAL_ADCEx_Calibration_Start(ADC_HandleTypeDef *h, uint32_t mode, uint32_t sd) { (void)h; (void)mode; (void)sd; sim_busy_wait_ns(100000); return HAL_OK; }
#else
HAL_StatusTypeDef HAL_ADCEx_Calibration_Start(ADC_HandleTypeDef *h, uint32_t sd) { (void)h; (void)sd; sim_busy_wait_ns(100000); return HAL_OK; }
HAL_StatusTypeDef HAL_ADCEx_MultiModeConfigChannel(ADC_HandleTypeDef *h, const ADC_MultiModeTypeDef *m) { (void)h; (void)m; return HAL_OK; }
#endif
HAL_StatusTypeDef HAL_ADC_Start(ADC_HandleTypeDef *h) { adcs_t *a = adc_of(h); h->Instance->DR = adc_value(a, a->last_ch); h->State = HAL_ADC_STATE_REG_BUSY; return HAL_OK; }
HAL_StatusTypeDef HAL_ADC_Stop(ADC_HandleTypeDef *h) { h->State = HAL_ADC_STATE_READY; return HAL_OK; }
HAL_StatusTypeDef HAL_ADC_PollForConversion(ADC_HandleTypeDef *h, uint32_t t) { (void)t; sim_busy_wait_ns(5000); h->State |= HAL_ADC_STATE_REG_EOC; return HAL_OK; }
uint32_t HAL_ADC_GetValue(const ADC_HandleTypeDef *h) { return h->Instance->DR; }
uint32_t HAL_ADC_GetState(const ADC_HandleTypeDef *h) { return h->State; }
uint32_t HAL_ADC_GetError(const ADC_HandleTypeDef *h) { return h->ErrorCode; }
void HAL_ADC_IRQHandler(ADC_HandleTypeDef *h) { (void)h; }
static void adc_fill(adcs_t *a, uint32_t from, uint32_t to)
{
    uint32_t nconv = a->h->Init.NbrOfConversion ? a->h->Init.NbrOfConversion : 1;
    for (uint32_t i = from; i < to; i++) {
        uint32_t v = adc_value(a, a->rank_ch[i % nconv]);
        if (a->wide) ((uint32_t *)a->buf)[i] = v; else ((uint16_t *)a->buf)[i] = (uint16_t)v;
    }
}
static void adc_dma_isr(void *arg, uintptr_t half)
{
    adcs_t *a = arg;
    if (!a->running) return;
    uint64_t period = (uint64_t)sim_env_long("SIM_ADC_DMA_PERIOD_US", 1000) * 1000u;
    if (half) { adc_fill(a, 0, a->len / 2); HAL_ADC_ConvHalfCpltCallback(a->h); sim_schedule(sim_now_ns() + period / 2, -1, adc_dma_isr, a, 0); }
    else {
        adc_fill(a, a->len / 2, a->len); HAL_ADC_ConvCpltCallback(a->h);
        int circular = a->h->DMA_Handle &&
#if defined(DMA_LINKEDLIST_CIRCULAR)
            a->h->DMA_Handle->Mode == DMA_LINKEDLIST_CIRCULAR;
#else
            a->h->DMA_Handle->Init.Mode == DMA_CIRCULAR;
#endif
        if (circular || a->h->Init.ContinuousConvMode == ENABLE) sim_schedule(sim_now_ns() + period / 2, -1, adc_dma_isr, a, 1);
        else { a->running = 0; a->h->State = HAL_ADC_STATE_READY; }
    }
}
HAL_StatusTypeDef HAL_ADC_Start_DMA(ADC_HandleTypeDef *h, SIM_ADC_BUF_CONST uint32_t *buf, uint32_t len)
{
    adcs_t *a = adc_of(h);
    a->buf = (void *)buf; a->len = len; a->running = 1; a->wide = 0;
#if defined(DMA_MDATAALIGN_WORD)
    if (h->DMA_Handle && h->DMA_Handle->Init.MemDataAlignment == DMA_MDATAALIGN_WORD) a->wide = 1;
#elif defined(DMA_DEST_DATAWIDTH_WORD)
    if (h->DMA_Handle && h->DMA_Handle->Init.DestDataWidth == DMA_DEST_DATAWIDTH_WORD) a->wide = 1;
#endif
    h->State = HAL_ADC_STATE_REG_BUSY;
    sim_cancel(adc_dma_isr, a);
    uint64_t period = (uint64_t)sim_env_long("SIM_ADC_DMA_PERIOD_US", 1000) * 1000u;
    sim_schedule(sim_now_ns() + period / 2, -1, adc_dma_isr, a, len >= 2 ? 1 : 0);
    return HAL_OK;
}
HAL_StatusTypeDef HAL_ADC_Stop_DMA(ADC_HandleTypeDef *h) { adcs_t *a = adc_of(h); a->running = 0; sim_cancel(adc_dma_isr, a); h->State = HAL_ADC_STATE_READY; return HAL_OK; }
#endif

/* ---------------------------------------------------------------- DAC */
#ifdef HAL_DAC_MODULE_ENABLED
static uint32_t dac_values[2];
__attribute__((weak)) void HAL_DAC_MspInit(DAC_HandleTypeDef *h) { (void)h; }
__attribute__((weak)) void HAL_DAC_MspDeInit(DAC_HandleTypeDef *h) { (void)h; }
HAL_StatusTypeDef HAL_DAC_Init(DAC_HandleTypeDef *h) { if (h->State == HAL_DAC_STATE_RESET) { h->Lock = HAL_UNLOCKED; HAL_DAC_MspInit(h); } h->State = HAL_DAC_STATE_READY; return HAL_OK; }
HAL_StatusTypeDef HAL_DAC_DeInit(DAC_HandleTypeDef *h) { h->State = HAL_DAC_STATE_RESET; return HAL_OK; }
HAL_StatusTypeDef HAL_DAC_ConfigChannel(DAC_HandleTypeDef *h, const DAC_ChannelConfTypeDef *c, uint32_t ch) { (void)h; (void)c; (void)ch; return HAL_OK; }
/* The DAC registers mirror what the hardware would hold (the station-managed bay's plant reads the DAQ's DAC from
 * them): CR ENx follows Start/Stop, DHR12Rx holds the last software value. */
HAL_StatusTypeDef HAL_DAC_Start(DAC_HandleTypeDef *h, uint32_t ch) { if (h && h->Instance) h->Instance->CR |= DAC_CR_EN1 << (ch & 16u); return HAL_OK; }
HAL_StatusTypeDef HAL_DAC_Stop(DAC_HandleTypeDef *h, uint32_t ch) { if (h && h->Instance) h->Instance->CR &= ~(DAC_CR_EN1 << (ch & 16u)); return HAL_OK; }
HAL_StatusTypeDef HAL_DAC_SetValue(DAC_HandleTypeDef *h, uint32_t ch, uint32_t align, uint32_t v)
{
    (void)align; dac_values[ch ? 1 : 0] = v;
    if (h && h->Instance) { if (ch) h->Instance->DHR12R2 = v & 0xFFFu; else h->Instance->DHR12R1 = v & 0xFFFu; }
    return HAL_OK;
}
uint32_t HAL_DAC_GetValue(const DAC_HandleTypeDef *h, uint32_t ch) { (void)h; return dac_values[ch ? 1 : 0]; }
#if defined(STM32U5) || defined(DAC_AUTONOMOUS_MODE_ENABLE)
HAL_StatusTypeDef HAL_DACEx_SetConfigAutonomousMode(DAC_HandleTypeDef *h, const DAC_AutonomousModeConfTypeDef *c) { (void)h; (void)c; return HAL_OK; }
#endif
#endif

/* ---------------------------------------------------------------- I2C (LTC2990 models) */
#ifdef HAL_I2C_MODULE_ENABLED
int sim_i2c_write(const char *bus, int addr, const uint8_t *d, unsigned n);
int sim_i2c_read(const char *bus, int addr, uint8_t *d, unsigned n);
int sim_i2c_present(const char *bus, int addr);
__attribute__((weak)) void HAL_I2C_MspInit(I2C_HandleTypeDef *h) { (void)h; }
__attribute__((weak)) void HAL_I2C_MspDeInit(I2C_HandleTypeDef *h) { (void)h; }
static const char *i2c_bus(I2C_HandleTypeDef *h)
{
#ifdef I2C1
    if (h->Instance == I2C1) return "i2c1";
#endif
#ifdef I2C2
    if (h->Instance == I2C2) return "i2c2";
#endif
#ifdef I2C3
    if (h->Instance == I2C3) return "i2c3";
#endif
    return "i2c?";
}
static uint64_t i2c_ns(unsigned bytes) { return (uint64_t)(bytes + 2) * 9u * 2500u; } /* 400 kHz */
HAL_StatusTypeDef HAL_I2C_Init(I2C_HandleTypeDef *h) { if (h->State == HAL_I2C_STATE_RESET) { h->Lock = HAL_UNLOCKED; HAL_I2C_MspInit(h); } h->State = HAL_I2C_STATE_READY; h->ErrorCode = 0; return HAL_OK; }
HAL_StatusTypeDef HAL_I2C_DeInit(I2C_HandleTypeDef *h) { HAL_I2C_MspDeInit(h); h->State = HAL_I2C_STATE_RESET; return HAL_OK; }
HAL_StatusTypeDef HAL_I2CEx_ConfigAnalogFilter(I2C_HandleTypeDef *h, uint32_t f) { (void)h; (void)f; return HAL_OK; }
HAL_StatusTypeDef HAL_I2CEx_ConfigDigitalFilter(I2C_HandleTypeDef *h, uint32_t f) { (void)h; (void)f; return HAL_OK; }
uint32_t HAL_I2C_GetError(const I2C_HandleTypeDef *h) { return h->ErrorCode; }
HAL_I2C_StateTypeDef HAL_I2C_GetState(const I2C_HandleTypeDef *h) { return h->State; }
HAL_StatusTypeDef HAL_I2C_Mem_Write(I2C_HandleTypeDef *h, uint16_t dev, uint16_t reg, uint16_t rsz, uint8_t *d, uint16_t n, uint32_t t)
{
    (void)rsz; (void)t;
    uint8_t buf[260]; buf[0] = (uint8_t)reg; if (n > 255) n = 255; memcpy(buf + 1, d, n);
    sim_busy_wait_ns(i2c_ns(n + 1));
    if (sim_i2c_write(i2c_bus(h), dev >> 1, buf, n + 1u) < 0) { h->ErrorCode = HAL_I2C_ERROR_AF; return HAL_ERROR; }
    h->ErrorCode = 0; return HAL_OK;
}
HAL_StatusTypeDef HAL_I2C_Mem_Read(I2C_HandleTypeDef *h, uint16_t dev, uint16_t reg, uint16_t rsz, uint8_t *d, uint16_t n, uint32_t t)
{
    (void)rsz; (void)t;
    uint8_t r = (uint8_t)reg;
    sim_busy_wait_ns(i2c_ns(n + 2));
    if (sim_i2c_write(i2c_bus(h), dev >> 1, &r, 1) < 0 || sim_i2c_read(i2c_bus(h), dev >> 1, d, n) < 0) { h->ErrorCode = HAL_I2C_ERROR_AF; return HAL_ERROR; }
    h->ErrorCode = 0; return HAL_OK;
}
HAL_StatusTypeDef HAL_I2C_Master_Transmit(I2C_HandleTypeDef *h, uint16_t dev, uint8_t *d, uint16_t n, uint32_t t)
{
    (void)t; sim_busy_wait_ns(i2c_ns(n));
    if (sim_i2c_write(i2c_bus(h), dev >> 1, d, n) < 0) { h->ErrorCode = HAL_I2C_ERROR_AF; return HAL_ERROR; }
    return HAL_OK;
}
HAL_StatusTypeDef HAL_I2C_Master_Receive(I2C_HandleTypeDef *h, uint16_t dev, uint8_t *d, uint16_t n, uint32_t t)
{
    (void)t; sim_busy_wait_ns(i2c_ns(n));
    if (sim_i2c_read(i2c_bus(h), dev >> 1, d, n) < 0) { h->ErrorCode = HAL_I2C_ERROR_AF; return HAL_ERROR; }
    return HAL_OK;
}
HAL_StatusTypeDef HAL_I2C_IsDeviceReady(I2C_HandleTypeDef *h, uint16_t dev, uint32_t tr, uint32_t t)
{
    (void)tr; (void)t; sim_busy_wait_ns(i2c_ns(0));
    return sim_i2c_present(i2c_bus(h), dev >> 1) ? HAL_OK : HAL_ERROR;
}
#endif

/* ---------------------------------------------------------------- SPI (device models plug in sim_spi_xfer) */
#ifdef HAL_SPI_MODULE_ENABLED
__attribute__((weak)) void sim_spi_xfer(const char *bus, const uint8_t *tx, uint8_t *rx, uint16_t n) { (void)bus; (void)tx; if (rx) memset(rx, 0, n); }
__attribute__((weak)) void HAL_SPI_TxRxCpltCallback(SPI_HandleTypeDef *h) { (void)h; }
__attribute__((weak)) void HAL_SPI_TxCpltCallback(SPI_HandleTypeDef *h) { (void)h; }
__attribute__((weak)) void HAL_SPI_RxCpltCallback(SPI_HandleTypeDef *h) { (void)h; }
__attribute__((weak)) void HAL_SPI_ErrorCallback(SPI_HandleTypeDef *h) { (void)h; }
__attribute__((weak)) void HAL_SPI_MspInit(SPI_HandleTypeDef *h) { (void)h; }
__attribute__((weak)) void HAL_SPI_MspDeInit(SPI_HandleTypeDef *h) { (void)h; }
static const char *spi_bus(SPI_HandleTypeDef *h)
{
#ifdef SPI1
    if (h->Instance == SPI1) return "spi1";
#endif
#ifdef SPI2
    if (h->Instance == SPI2) return "spi2";
#endif
#ifdef SPI3
    if (h->Instance == SPI3) return "spi3";
#endif
    return "spi?";
}
static uint64_t spi_ns(uint16_t n) { return (uint64_t)n * 8u * 100u; } /* ~10 MHz */
HAL_StatusTypeDef HAL_SPI_Init(SPI_HandleTypeDef *h) { if (h->State == HAL_SPI_STATE_RESET) { h->Lock = HAL_UNLOCKED; HAL_SPI_MspInit(h); } h->State = HAL_SPI_STATE_READY; h->ErrorCode = 0; return HAL_OK; }
HAL_StatusTypeDef HAL_SPI_DeInit(SPI_HandleTypeDef *h) { HAL_SPI_MspDeInit(h); h->State = HAL_SPI_STATE_RESET; return HAL_OK; }
HAL_SPI_StateTypeDef HAL_SPI_GetState(const SPI_HandleTypeDef *h) { return h->State; }
uint32_t HAL_SPI_GetError(const SPI_HandleTypeDef *h) { return h->ErrorCode; }
HAL_StatusTypeDef HAL_SPI_TransmitReceive(SPI_HandleTypeDef *h, const uint8_t *tx, uint8_t *rx, uint16_t n, uint32_t t)
{
    (void)t;
    if (h->State != HAL_SPI_STATE_READY) return HAL_BUSY;
    sim_busy_wait_ns(spi_ns(n));
    sim_spi_xfer(spi_bus(h), tx, rx, n);
    return HAL_OK;
}
HAL_StatusTypeDef HAL_SPI_Transmit(SPI_HandleTypeDef *h, const uint8_t *tx, uint16_t n, uint32_t t)
{
    (void)t; if (h->State != HAL_SPI_STATE_READY) return HAL_BUSY;
    sim_busy_wait_ns(spi_ns(n)); sim_spi_xfer(spi_bus(h), tx, NULL, n); return HAL_OK;
}
HAL_StatusTypeDef HAL_SPI_Receive(SPI_HandleTypeDef *h, uint8_t *rx, uint16_t n, uint32_t t)
{
    (void)t; if (h->State != HAL_SPI_STATE_READY) return HAL_BUSY;
    sim_busy_wait_ns(spi_ns(n)); sim_spi_xfer(spi_bus(h), NULL, rx, n); return HAL_OK;
}
static void spi_done(void *arg, uintptr_t a2) { (void)a2; SPI_HandleTypeDef *h = arg; h->State = HAL_SPI_STATE_READY; HAL_SPI_TxRxCpltCallback(h); }
HAL_StatusTypeDef HAL_SPI_TransmitReceive_DMA(SPI_HandleTypeDef *h, const uint8_t *tx, uint8_t *rx, uint16_t n)
{
    if (h->State != HAL_SPI_STATE_READY) return HAL_BUSY;
    h->State = HAL_SPI_STATE_BUSY_TX_RX;
    sim_spi_xfer(spi_bus(h), tx, rx, n);
    sim_schedule(sim_now_ns() + spi_ns(n), -1, spi_done, h, 0);
    return HAL_OK;
}
HAL_StatusTypeDef HAL_SPI_TransmitReceive_IT(SPI_HandleTypeDef *h, const uint8_t *tx, uint8_t *rx, uint16_t n) { return HAL_SPI_TransmitReceive_DMA(h, tx, rx, n); }
HAL_StatusTypeDef HAL_SPI_Abort(SPI_HandleTypeDef *h) { sim_cancel(spi_done, h); h->State = HAL_SPI_STATE_READY; return HAL_OK; }
void HAL_SPI_IRQHandler(SPI_HandleTypeDef *h) { (void)h; }
#if defined(STM32U5)
HAL_StatusTypeDef HAL_SPIEx_SetConfigAutonomousMode(SPI_HandleTypeDef *h, const SPI_AutonomousModeConfTypeDef *c) { (void)h; (void)c; return HAL_OK; }
#endif
#endif

/* ---------------------------------------------------------------- caches, USB device controller, CRC */
#ifdef HAL_ICACHE_MODULE_ENABLED
HAL_StatusTypeDef HAL_ICACHE_Enable(void) { return HAL_OK; }
HAL_StatusTypeDef HAL_ICACHE_Disable(void) { return HAL_OK; }
HAL_StatusTypeDef HAL_ICACHE_ConfigAssociativityMode(uint32_t m) { (void)m; return HAL_OK; }
HAL_StatusTypeDef HAL_ICACHE_Invalidate(void) { return HAL_OK; }
uint32_t HAL_ICACHE_IsEnabled(void) { return 1; }
#endif
#ifdef HAL_DCACHE_MODULE_ENABLED
__attribute__((weak)) void HAL_DCACHE_MspInit(DCACHE_HandleTypeDef *h) { (void)h; }
__attribute__((weak)) void HAL_DCACHE_MspDeInit(DCACHE_HandleTypeDef *h) { (void)h; }
HAL_StatusTypeDef HAL_DCACHE_Init(DCACHE_HandleTypeDef *h) { HAL_DCACHE_MspInit(h); return HAL_OK; }
HAL_StatusTypeDef HAL_DCACHE_DeInit(DCACHE_HandleTypeDef *h) { HAL_DCACHE_MspDeInit(h); return HAL_OK; }
HAL_StatusTypeDef HAL_DCACHE_InvalidateByAddr(DCACHE_HandleTypeDef *h, const uint32_t *a, uint32_t s) { (void)h; (void)a; (void)s; return HAL_OK; }
HAL_StatusTypeDef HAL_DCACHE_CleanByAddr(DCACHE_HandleTypeDef *h, const uint32_t *a, uint32_t s) { (void)h; (void)a; (void)s; return HAL_OK; }
HAL_StatusTypeDef HAL_DCACHE_CleanInvalidByAddr(DCACHE_HandleTypeDef *h, const uint32_t *a, uint32_t s) { (void)h; (void)a; (void)s; return HAL_OK; }
HAL_StatusTypeDef HAL_DCACHE_Invalidate(DCACHE_HandleTypeDef *h) { (void)h; return HAL_OK; }
#endif
#ifdef HAL_PCD_MODULE_ENABLED
__attribute__((weak)) void HAL_PCD_MspInit(PCD_HandleTypeDef *h) { (void)h; }
__attribute__((weak)) void HAL_PCD_MspDeInit(PCD_HandleTypeDef *h) { (void)h; }
HAL_StatusTypeDef HAL_PCD_Init(PCD_HandleTypeDef *h) { HAL_PCD_MspInit(h); h->State = HAL_PCD_STATE_READY; return HAL_OK; }
HAL_StatusTypeDef HAL_PCD_DeInit(PCD_HandleTypeDef *h) { HAL_PCD_MspDeInit(h); h->State = HAL_PCD_STATE_RESET; return HAL_OK; }
HAL_StatusTypeDef HAL_PCD_Start(PCD_HandleTypeDef *h) { (void)h; return HAL_OK; }
HAL_StatusTypeDef HAL_PCD_Stop(PCD_HandleTypeDef *h) { (void)h; return HAL_OK; }
void HAL_PCD_IRQHandler(PCD_HandleTypeDef *h) { (void)h; }
HAL_StatusTypeDef HAL_PCDEx_PMAConfig(PCD_HandleTypeDef *h, uint16_t a, uint16_t b, uint32_t c) { (void)h; (void)a; (void)b; (void)c; return HAL_OK; }
HAL_StatusTypeDef HAL_PCDEx_SetRxFiFo(PCD_HandleTypeDef *h, uint16_t s) { (void)h; (void)s; return HAL_OK; }
HAL_StatusTypeDef HAL_PCDEx_SetTxFiFo(PCD_HandleTypeDef *h, uint8_t f, uint16_t s) { (void)h; (void)f; (void)s; return HAL_OK; }
#endif
