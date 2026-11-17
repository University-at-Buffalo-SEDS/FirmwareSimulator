/* U(S)ART at the HAL boundary. Tx bytes go to the coordinator stamped with their start time and baud (it paces
 * them on the wire); TxCplt fires after the frame's byte time. Rx bytes arrive from the coordinator and are
 * written through the board's DMA buffer exactly as the DMA would: the channel's remaining-count register is
 * kept current (__HAL_DMA_GET_COUNTER works), half/full transfer and IDLE raise HAL_UARTEx_RxEventCallback
 * with the HAL's event types, circular buffers wrap, normal-mode receptions end at TC/IDLE. */
#include "sim.h"
#include "main.h"
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#ifdef HAL_UART_MODULE_ENABLED
#define MAX_UART 8
typedef struct {
    UART_HandleTypeDef *h;
    char name[8];
    int irqn;
    uint8_t *buf; uint32_t size, pos; int circular, active, mode_idle;
    uint64_t last_rx;
    uint32_t overruns;
} uart_t;
static uart_t uarts[MAX_UART];

static void name_of(const void *inst, char *name, int *irqn)
{
    strcpy(name, "uart?"); *irqn = 0;
#define U(I, N, IRQ) if (inst == (const void *)I) { strcpy(name, N); *irqn = IRQ; return; }
#ifdef USART1
    U(USART1, "usart1", USART1_IRQn)
#endif
#ifdef USART2
    U(USART2, "usart2", USART2_IRQn)
#endif
#ifdef USART3
    U(USART3, "usart3", USART3_IRQn)
#endif
#ifdef UART4
    U(UART4, "uart4", UART4_IRQn)
#endif
#ifdef UART5
    U(UART5, "uart5", UART5_IRQn)
#endif
#ifdef USART6
    U(USART6, "usart6", USART6_IRQn)
#endif
#ifdef LPUART1
    U(LPUART1, "lpuart1", LPUART1_IRQn)
#endif
#undef U
}
const char *sim_port_name(const void *instance)
{
    static char n[8]; int irq; name_of(instance, n, &irq); return n;
}

static uart_t *uart_of(UART_HandleTypeDef *h)
{
    for (int i = 0; i < MAX_UART; i++) if (uarts[i].h == h) return &uarts[i];
    for (int i = 0; i < MAX_UART; i++) if (!uarts[i].h) { uarts[i].h = h; name_of(h->Instance, uarts[i].name, &uarts[i].irqn); return &uarts[i]; }
    return &uarts[0];
}
static uint64_t byte_ns(UART_HandleTypeDef *h) { uint32_t b = h->Init.BaudRate ? h->Init.BaudRate : 115200; return 10000000000ull / b; }

static void set_counter(DMA_HandleTypeDef *d, uint32_t remaining)
{
    if (!d || !d->Instance) return;
#if defined(DMA_CBR1_BNDT)
    d->Instance->CBR1 = (d->Instance->CBR1 & ~DMA_CBR1_BNDT) | (remaining & DMA_CBR1_BNDT);
#else
    d->Instance->CNDTR = remaining;
#endif
}
static int dma_circular(DMA_HandleTypeDef *d)
{
    if (!d) return 0;
#if defined(DMA_LINKEDLIST_CIRCULAR)
    return d->Mode == DMA_LINKEDLIST_CIRCULAR;
#else
    return d->Init.Mode == DMA_CIRCULAR;
#endif
}

__attribute__((weak)) void HAL_UART_TxCpltCallback(UART_HandleTypeDef *h) { (void)h; }
__attribute__((weak)) void HAL_UART_TxHalfCpltCallback(UART_HandleTypeDef *h) { (void)h; }
__attribute__((weak)) void HAL_UART_RxCpltCallback(UART_HandleTypeDef *h) { (void)h; }
__attribute__((weak)) void HAL_UART_RxHalfCpltCallback(UART_HandleTypeDef *h) { (void)h; }
__attribute__((weak)) void HAL_UART_ErrorCallback(UART_HandleTypeDef *h) { (void)h; }
__attribute__((weak)) void HAL_UART_AbortCpltCallback(UART_HandleTypeDef *h) { (void)h; }
__attribute__((weak)) void HAL_UART_AbortTransmitCpltCallback(UART_HandleTypeDef *h) { (void)h; }
__attribute__((weak)) void HAL_UART_AbortReceiveCpltCallback(UART_HandleTypeDef *h) { (void)h; }
__attribute__((weak)) void HAL_UARTEx_RxEventCallback(UART_HandleTypeDef *h, uint16_t s) { (void)h; (void)s; }
__attribute__((weak)) void HAL_UART_MspInit(UART_HandleTypeDef *h) { (void)h; }
__attribute__((weak)) void HAL_UART_MspDeInit(UART_HandleTypeDef *h) { (void)h; }

HAL_StatusTypeDef HAL_UART_Init(UART_HandleTypeDef *h)
{
    if (!h) return HAL_ERROR;
    if (h->gState == HAL_UART_STATE_RESET) { h->Lock = HAL_UNLOCKED; HAL_UART_MspInit(h); }
    uart_t *u = uart_of(h); u->active = 0;
    h->ErrorCode = HAL_UART_ERROR_NONE; h->gState = HAL_UART_STATE_READY; h->RxState = HAL_UART_STATE_READY;
    h->ReceptionType = HAL_UART_RECEPTION_STANDARD;
    return HAL_OK;
}
HAL_StatusTypeDef HAL_HalfDuplex_Init(UART_HandleTypeDef *h) { return HAL_UART_Init(h); }
HAL_StatusTypeDef HAL_UART_DeInit(UART_HandleTypeDef *h) { uart_of(h)->active = 0; HAL_UART_MspDeInit(h); h->gState = h->RxState = HAL_UART_STATE_RESET; return HAL_OK; }
HAL_StatusTypeDef HAL_UARTEx_SetTxFifoThreshold(UART_HandleTypeDef *h, uint32_t t) { (void)h; (void)t; return HAL_OK; }
HAL_StatusTypeDef HAL_UARTEx_SetRxFifoThreshold(UART_HandleTypeDef *h, uint32_t t) { (void)h; (void)t; return HAL_OK; }
HAL_StatusTypeDef HAL_UARTEx_DisableFifoMode(UART_HandleTypeDef *h) { (void)h; return HAL_OK; }
HAL_StatusTypeDef HAL_UARTEx_EnableFifoMode(UART_HandleTypeDef *h) { (void)h; return HAL_OK; }
HAL_UART_StateTypeDef HAL_UART_GetState(const UART_HandleTypeDef *h) { return h->gState | h->RxState; }
uint32_t HAL_UART_GetError(const UART_HandleTypeDef *h) { return h->ErrorCode; }
HAL_UART_RxEventTypeTypeDef HAL_UARTEx_GetRxEventType(const UART_HandleTypeDef *h) { return h->RxEventType; }
void HAL_UART_IRQHandler(UART_HandleTypeDef *h) { (void)h; }

static void send_bytes(uart_t *u, const uint8_t *data, uint16_t len)
{
    struct __attribute__((packed)) { uint64_t t; char port[8]; uint32_t baud; } m;
    m.t = sim_local_to_global(sim_now_ns()); memcpy(m.port, u->name, 8); m.baud = u->h->Init.BaudRate;
    sim_send2(SIM_MSG_UART_TX, &m, sizeof m, data, len);
}
static void tx_done(void *arg, uintptr_t a2)
{
    (void)a2;
    UART_HandleTypeDef *h = arg;
    if (h->gState != HAL_UART_STATE_BUSY_TX) return;
    h->gState = HAL_UART_STATE_READY;
    HAL_UART_TxCpltCallback(h);
}
static HAL_StatusTypeDef tx_async(UART_HandleTypeDef *h, const uint8_t *data, uint16_t len)
{
    sim_charge_ns(1500);
    if (h->gState != HAL_UART_STATE_READY) return HAL_BUSY;
    if (!data || !len) return HAL_ERROR;
    uart_t *u = uart_of(h);
    h->gState = HAL_UART_STATE_BUSY_TX; h->ErrorCode = HAL_UART_ERROR_NONE;
    h->pTxBuffPtr = (const uint8_t *)data; h->TxXferSize = len; h->TxXferCount = 0;
    send_bytes(u, data, len);
    sim_schedule(sim_now_ns() + (uint64_t)len * byte_ns(h), u->irqn, tx_done, h, 0);
    return HAL_OK;
}
HAL_StatusTypeDef HAL_UART_Transmit_DMA(UART_HandleTypeDef *h, const uint8_t *d, uint16_t n) { return tx_async(h, d, n); }
HAL_StatusTypeDef HAL_UART_Transmit_IT(UART_HandleTypeDef *h, const uint8_t *d, uint16_t n) { return tx_async(h, d, n); }
HAL_StatusTypeDef HAL_UART_Transmit(UART_HandleTypeDef *h, const uint8_t *d, uint16_t n, uint32_t timeout)
{
    (void)timeout;
    if (h->gState != HAL_UART_STATE_READY) return HAL_BUSY;
    uart_t *u = uart_of(h);
    h->gState = HAL_UART_STATE_BUSY_TX;
    send_bytes(u, d, n);
    sim_busy_wait_ns((uint64_t)n * byte_ns(h));
    h->gState = HAL_UART_STATE_READY;
    return HAL_OK;
}
HAL_StatusTypeDef HAL_UART_AbortTransmit(UART_HandleTypeDef *h) { sim_cancel(tx_done, h); h->gState = HAL_UART_STATE_READY; return HAL_OK; }
HAL_StatusTypeDef HAL_UART_AbortTransmit_IT(UART_HandleTypeDef *h) { HAL_UART_AbortTransmit(h); HAL_UART_AbortTransmitCpltCallback(h); return HAL_OK; }
HAL_StatusTypeDef HAL_UART_DMAStop(UART_HandleTypeDef *h) { HAL_UART_AbortTransmit(h); uart_of(h)->active = 0; h->RxState = HAL_UART_STATE_READY; return HAL_OK; }
HAL_StatusTypeDef HAL_UART_DMAPause(UART_HandleTypeDef *h) { (void)h; return HAL_OK; }
HAL_StatusTypeDef HAL_UART_DMAResume(UART_HandleTypeDef *h) { (void)h; return HAL_OK; }

static void idle_isr(void *arg, uintptr_t a2);
static HAL_StatusTypeDef rx_start(UART_HandleTypeDef *h, uint8_t *buf, uint16_t size, int to_idle, int dma)
{
    sim_charge_ns(1500);
    if (h->RxState != HAL_UART_STATE_READY) return HAL_BUSY;
    if (!buf || !size) return HAL_ERROR;
    uart_t *u = uart_of(h);
    u->buf = buf; u->size = size; u->pos = 0; u->active = 1; u->mode_idle = to_idle;
    u->circular = dma && dma_circular(h->hdmarx);
    h->pRxBuffPtr = buf; h->RxXferSize = size; h->RxXferCount = size;
    h->RxState = HAL_UART_STATE_BUSY_RX; h->ErrorCode = HAL_UART_ERROR_NONE;
    h->ReceptionType = to_idle ? HAL_UART_RECEPTION_TOIDLE : HAL_UART_RECEPTION_STANDARD;
    if (dma) set_counter(h->hdmarx, size);
    sim_cancel(idle_isr, u);
    return HAL_OK;
}
HAL_StatusTypeDef HAL_UARTEx_ReceiveToIdle_DMA(UART_HandleTypeDef *h, uint8_t *b, uint16_t n) { return rx_start(h, b, n, 1, 1); }
HAL_StatusTypeDef HAL_UARTEx_ReceiveToIdle_IT(UART_HandleTypeDef *h, uint8_t *b, uint16_t n) { return rx_start(h, b, n, 1, 0); }
HAL_StatusTypeDef HAL_UART_Receive_DMA(UART_HandleTypeDef *h, uint8_t *b, uint16_t n) { return rx_start(h, b, n, 0, 1); }
HAL_StatusTypeDef HAL_UART_Receive_IT(UART_HandleTypeDef *h, uint8_t *b, uint16_t n) { return rx_start(h, b, n, 0, 0); }
HAL_StatusTypeDef HAL_UART_AbortReceive(UART_HandleTypeDef *h)
{
    uart_t *u = uart_of(h); u->active = 0; sim_cancel(idle_isr, u);
    h->RxState = HAL_UART_STATE_READY; h->ReceptionType = HAL_UART_RECEPTION_STANDARD;
    return HAL_OK;
}
HAL_StatusTypeDef HAL_UART_AbortReceive_IT(UART_HandleTypeDef *h) { HAL_UART_AbortReceive(h); HAL_UART_AbortReceiveCpltCallback(h); return HAL_OK; }
HAL_StatusTypeDef HAL_UART_Abort(UART_HandleTypeDef *h) { HAL_UART_AbortTransmit(h); return HAL_UART_AbortReceive(h); }
HAL_StatusTypeDef HAL_UART_Abort_IT(UART_HandleTypeDef *h) { HAL_UART_Abort(h); HAL_UART_AbortCpltCallback(h); return HAL_OK; }

static void rx_event(uart_t *u, uint32_t type, uint16_t size)
{
    UART_HandleTypeDef *h = u->h;
    h->RxEventType = type;
    if (u->mode_idle) HAL_UARTEx_RxEventCallback(h, size);
    else if (type == HAL_UART_RXEVENT_HT) HAL_UART_RxHalfCpltCallback(h);
    else if (type == HAL_UART_RXEVENT_TC) HAL_UART_RxCpltCallback(h);
}
static void finish_normal(uart_t *u)
{
    u->active = 0; u->h->RxState = HAL_UART_STATE_READY; u->h->ReceptionType = HAL_UART_RECEPTION_STANDARD;
}
static void idle_isr(void *arg, uintptr_t a2)
{
    (void)a2;
    uart_t *u = arg;
    if (!u->active || !u->mode_idle || u->pos == 0 || u->pos == u->size) return;
    uint16_t pos = (uint16_t)u->pos;
    if (!u->circular) finish_normal(u);
    rx_event(u, HAL_UART_RXEVENT_IDLE, pos);
}

/* Bytes arrive (ISR context, the UART's line). */
static void rx_bytes(void *arg, uintptr_t a2)
{
    uart_t *u = arg; uint8_t *chunk = (uint8_t *)a2;
    uint32_t n; memcpy(&n, chunk, 4);
    for (uint32_t i = 0; i < n; i++) {
        if (!u->active) { u->overruns++; continue; }
        u->buf[u->pos++] = chunk[4 + i];
        UART_HandleTypeDef *h = u->h;
        if (h->hdmarx) set_counter(h->hdmarx, u->size - u->pos);
        h->RxXferCount = (uint16_t)(u->size - u->pos);
        if (u->pos == u->size / 2 && u->size >= 2 && (h->hdmarx || !u->mode_idle)) rx_event(u, HAL_UART_RXEVENT_HT, (uint16_t)(u->size / 2));
        if (u->pos == u->size) {
            uint16_t sz = (uint16_t)u->size;
            if (u->circular) { u->pos = 0; if (h->hdmarx) set_counter(h->hdmarx, u->size); }
            else finish_normal(u);
            rx_event(u, HAL_UART_RXEVENT_TC, sz);
        }
    }
    free(chunk);
    sim_cancel(idle_isr, u);
    if (u->active && u->mode_idle) sim_schedule(sim_now_ns() + byte_ns(u->h), u->irqn, idle_isr, u, 0);
}

static void on_uart_rx(uint16_t type, const uint8_t *b, uint32_t len)
{
    (void)type;
    if (len < 16) return;
    uint64_t t; memcpy(&t, b, 8);
    for (int i = 0; i < MAX_UART; i++) {
        uart_t *u = &uarts[i];
        if (!u->h || strncmp(u->name, (const char *)b + 8, 8)) continue;
        uint32_t n = len - 16;
        uint8_t *chunk = malloc(n + 4); memcpy(chunk, &n, 4); memcpy(chunk + 4, b + 16, n);
        extern uint64_t sim_global_to_local(uint64_t);
        uint64_t at = sim_global_to_local(t);
        if (at < sim_now_ns()) at = sim_now_ns();
        sim_schedule(at, u->irqn, rx_bytes, u, (uintptr_t)chunk);
        return;
    }
}
__attribute__((constructor)) static void uart_register(void) { sim_register_rx(SIM_MSG_UART_RX, on_uart_rx); }
#endif

#ifdef HAL_DMA_MODULE_ENABLED
HAL_StatusTypeDef HAL_DMA_Init(DMA_HandleTypeDef *h) { if (h) h->State = HAL_DMA_STATE_READY; return HAL_OK; }
HAL_StatusTypeDef HAL_DMA_DeInit(DMA_HandleTypeDef *h) { if (h) h->State = HAL_DMA_STATE_RESET; return HAL_OK; }
void HAL_DMA_IRQHandler(DMA_HandleTypeDef *h) { (void)h; }
#if defined(STM32U5) || defined(STM32H5)
HAL_DMA_StateTypeDef HAL_DMA_GetState(const DMA_HandleTypeDef *h)
#else
HAL_DMA_StateTypeDef HAL_DMA_GetState(DMA_HandleTypeDef *h)
#endif
{ return h ? h->State : HAL_DMA_STATE_RESET; }
HAL_StatusTypeDef HAL_DMA_Abort(DMA_HandleTypeDef *h) { if (h) h->State = HAL_DMA_STATE_READY; return HAL_OK; }
HAL_StatusTypeDef HAL_DMA_Abort_IT(DMA_HandleTypeDef *h) { if (h) h->State = HAL_DMA_STATE_READY; return HAL_OK; }
#if defined(DMA_CBR1_BNDT)
HAL_StatusTypeDef HAL_DMA_ConfigChannelAttributes(DMA_HandleTypeDef *h, uint32_t a) { (void)h; (void)a; return HAL_OK; }
#endif
#endif
