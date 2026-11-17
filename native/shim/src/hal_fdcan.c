/* FDCAN at the HAL boundary (G4/U5/H5 FDCAN with 3 Tx FIFO/queue slots and 3-element Rx FIFOs).
 * Tx: a slot completes one frame time after it reaches the head of this controller's FIFO (nominal bit
 * timing from Init, frame size as the Renode wire model), only while the bus acknowledges (some other
 * controller connected); otherwise it stays pending (TXBRP) with ACK errors, error-passive, as on a lone board.
 * Rx: frames from the coordinator pass the configured standard/global filters into FIFO0/1 and raise the
 * activated notifications on the controller's IT0 line, gated by the simulated NVIC. Register mirrors
 * (TXBRP, TXFQS, PSR, ECR, RXF0S) are kept for firmware that reads them directly. */
#include "sim.h"
#include "main.h"
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#ifdef HAL_FDCAN_MODULE_ENABLED
#define MAX_FDCAN 3
#define MAX_FILTERS 28

typedef struct { uint32_t id, flags, len; uint8_t data[64]; uint32_t filter; int matched; } frame_t;
typedef struct {
    FDCAN_HandleTypeDef *h;
    char name[8];
    int irqn;
    int started, acked;
    uint32_t its, tx_buffer_its;
    frame_t tx[3]; uint32_t tx_pending, tx_order[3], tx_count; uint64_t head_done; uint32_t put_index;
    frame_t rx[2][3]; uint32_t rx_get[2], rx_fill[2];
    FDCAN_FilterTypeDef filters[MAX_FILTERS]; uint32_t nfilters;
    uint32_t non_std, non_ext, rej_rem_std, rej_rem_ext;
    uint32_t tec, lec, lost;
} fdcan_t;
static fdcan_t ports[MAX_FDCAN];
static const uint8_t dlc_bytes[16] = {0, 1, 2, 3, 4, 5, 6, 7, 8, 12, 16, 20, 24, 32, 48, 64};

static fdcan_t *port_of(const FDCAN_HandleTypeDef *h)
{
    for (int i = 0; i < MAX_FDCAN; i++) if (ports[i].h == h) return &ports[i];
    for (int i = 0; i < MAX_FDCAN; i++) {
        if (!ports[i].h) {
            fdcan_t *p = &ports[i];
            memset(p, 0, sizeof *p);
            p->h = (FDCAN_HandleTypeDef *)h; p->acked = 1;
#ifdef FDCAN1
            if (h->Instance == FDCAN1) { strcpy(p->name, "fdcan1"); p->irqn = FDCAN1_IT0_IRQn; }
#endif
#ifdef FDCAN2
            if (h->Instance == FDCAN2) { strcpy(p->name, "fdcan2"); p->irqn = FDCAN2_IT0_IRQn; }
#endif
#ifdef FDCAN3
            if (h->Instance == FDCAN3) { strcpy(p->name, "fdcan3"); p->irqn = FDCAN3_IT0_IRQn; }
#endif
            return p;
        }
    }
    return &ports[0];
}

static uint32_t dlc_code(uint32_t dl)
{
    /* HAL DataLength is the DLC code (0..15) in these HAL versions; older ones shift it by 16. */
    return dl > 15 ? (dl >> 16) & 15 : dl;
}

static void mirror(fdcan_t *p)
{
    FDCAN_GlobalTypeDef *r = p->h->Instance;
    r->TXBRP = p->tx_pending;
    uint32_t free_level = 3 - p->tx_count;
    r->TXFQS = free_level | (p->put_index << 16) | ((free_level == 0) << 21) | ((p->tx_count ? p->tx_order[0] : 0) << 8);
    r->RXF0S = p->rx_fill[0] | (p->rx_get[0] << 8) | (((p->rx_get[0] + p->rx_fill[0]) % 3) << 16) | ((p->rx_fill[0] == 3) << 24);
    r->RXF1S = p->rx_fill[1] | (p->rx_get[1] << 8) | (((p->rx_get[1] + p->rx_fill[1]) % 3) << 16) | ((p->rx_fill[1] == 3) << 24);
    uint32_t ep = p->tec >= 128, bo = p->tec >= 256;
    r->PSR = (p->lec & 7u) | (ep << 5) | (bo << 7) | (p->tec >= 96 ? 1u << 6 : 0) | (7u << 8);
    r->ECR = (p->tec > 255 ? 255 : p->tec);
}

static uint64_t frame_ns(fdcan_t *p, uint32_t len)
{
    const FDCAN_InitTypeDef *in = &p->h->Init;
    uint64_t clocks_per_bit = (uint64_t)(in->NominalPrescaler ? in->NominalPrescaler : 1) * (1 + in->NominalTimeSeg1 + in->NominalTimeSeg2);
    uint64_t clock = (uint64_t)sim_env_long("SIM_FDCAN_CLOCK_HZ", 170000000);
    uint64_t bits = 67 + 8ull * len; bits += (bits + 4) / 5;
    uint64_t ns = bits * clocks_per_bit * 1000000000ull / clock;
    return ns ? ns : 1000;
}

static void tx_complete_isr(void *arg, uintptr_t a2);
static void tx_kick(fdcan_t *p)
{
    if (!p->started || !p->tx_count || !p->acked) return;
    sim_cancel(tx_complete_isr, p);
    frame_t *f = &p->tx[p->tx_order[0]];
    uint64_t t = sim_now_ns() + frame_ns(p, f->len);
    if (p->head_done > t) t = p->head_done;
    sim_schedule(t, p->irqn, tx_complete_isr, p, 0);
}

__attribute__((weak)) void HAL_FDCAN_TxFifoEmptyCallback(FDCAN_HandleTypeDef *h) { (void)h; }
__attribute__((weak)) void HAL_FDCAN_TxBufferCompleteCallback(FDCAN_HandleTypeDef *h, uint32_t b) { (void)h; (void)b; }
__attribute__((weak)) void HAL_FDCAN_TxBufferAbortCallback(FDCAN_HandleTypeDef *h, uint32_t b) { (void)h; (void)b; }
__attribute__((weak)) void HAL_FDCAN_RxFifo0Callback(FDCAN_HandleTypeDef *h, uint32_t i) { (void)h; (void)i; }
__attribute__((weak)) void HAL_FDCAN_RxFifo1Callback(FDCAN_HandleTypeDef *h, uint32_t i) { (void)h; (void)i; }
__attribute__((weak)) void HAL_FDCAN_ErrorStatusCallback(FDCAN_HandleTypeDef *h, uint32_t i) { (void)h; (void)i; }
__attribute__((weak)) void HAL_FDCAN_ErrorCallback(FDCAN_HandleTypeDef *h) { (void)h; }

static void tx_complete_isr(void *arg, uintptr_t a2)
{
    (void)a2;
    fdcan_t *p = arg;
    if (!p->started || !p->tx_count || !p->acked) return;
    uint32_t slot = p->tx_order[0];
    frame_t *f = &p->tx[slot];
    struct __attribute__((packed)) { uint64_t t; char port[8]; uint8_t flags, len, pad[2]; uint32_t id; } m;
    m.t = sim_local_to_global(sim_now_ns());
    memcpy(m.port, p->name, 8); m.flags = (uint8_t)f->flags; m.len = (uint8_t)f->len; m.pad[0] = m.pad[1] = 0; m.id = f->id;
    sim_send2(SIM_MSG_CAN_TX, &m, sizeof m, f->data, f->len);
    p->tx_pending &= ~(1u << slot);
    memmove(p->tx_order, p->tx_order + 1, sizeof(uint32_t) * 2);
    p->tx_count--;
    if (p->tec) p->tec--;
    p->lec = 0;
    p->head_done = sim_now_ns();
    mirror(p);
    if ((p->its & FDCAN_IT_TX_COMPLETE) && (p->tx_buffer_its & (1u << slot)))
        HAL_FDCAN_TxBufferCompleteCallback(p->h, 1u << slot);
    if ((p->its & FDCAN_IT_TX_FIFO_EMPTY) && !p->tx_count) HAL_FDCAN_TxFifoEmptyCallback(p->h);
    tx_kick(p);
}

HAL_StatusTypeDef HAL_FDCAN_Init(FDCAN_HandleTypeDef *h)
{
    if (!h) return HAL_ERROR;
    fdcan_t *p = port_of(h);
    if (h->State == HAL_FDCAN_STATE_RESET) { h->Lock = HAL_UNLOCKED; HAL_FDCAN_MspInit(h); }
    p->started = 0; p->tx_count = 0; p->tx_pending = 0; p->rx_fill[0] = p->rx_fill[1] = 0;
    sim_cancel(tx_complete_isr, p);
    h->ErrorCode = HAL_FDCAN_ERROR_NONE; h->State = HAL_FDCAN_STATE_READY; h->LatestTxFifoQRequest = 0;
    mirror(p);
    return HAL_OK;
}
HAL_StatusTypeDef HAL_FDCAN_DeInit(FDCAN_HandleTypeDef *h) { fdcan_t *p = port_of(h); p->started = 0; h->State = HAL_FDCAN_STATE_RESET; HAL_FDCAN_MspDeInit(h); return HAL_OK; }
__attribute__((weak)) void HAL_FDCAN_MspInit(FDCAN_HandleTypeDef *h) { (void)h; }
__attribute__((weak)) void HAL_FDCAN_MspDeInit(FDCAN_HandleTypeDef *h) { (void)h; }

HAL_StatusTypeDef HAL_FDCAN_ConfigFilter(FDCAN_HandleTypeDef *h, const FDCAN_FilterTypeDef *f)
{
    fdcan_t *p = port_of(h);
    for (uint32_t i = 0; i < p->nfilters; i++)
        if (p->filters[i].IdType == f->IdType && p->filters[i].FilterIndex == f->FilterIndex) { p->filters[i] = *f; return HAL_OK; }
    if (p->nfilters < MAX_FILTERS) p->filters[p->nfilters++] = *f;
    return HAL_OK;
}
HAL_StatusTypeDef HAL_FDCAN_ConfigGlobalFilter(FDCAN_HandleTypeDef *h, uint32_t ns, uint32_t ne, uint32_t rrs, uint32_t rre)
{
    fdcan_t *p = port_of(h);
    p->non_std = ns; p->non_ext = ne; p->rej_rem_std = rrs; p->rej_rem_ext = rre;
    return HAL_OK;
}
HAL_StatusTypeDef HAL_FDCAN_ConfigExtendedIdMask(FDCAN_HandleTypeDef *h, uint32_t m) { (void)h; (void)m; return HAL_OK; }
HAL_StatusTypeDef HAL_FDCAN_ConfigRxFifoOverwrite(FDCAN_HandleTypeDef *h, uint32_t f, uint32_t m) { (void)h; (void)f; (void)m; return HAL_OK; }
HAL_StatusTypeDef HAL_FDCAN_ConfigFifoWatermark(FDCAN_HandleTypeDef *h, uint32_t f, uint32_t w) { (void)h; (void)f; (void)w; return HAL_OK; }
HAL_StatusTypeDef HAL_FDCAN_ConfigTxDelayCompensation(FDCAN_HandleTypeDef *h, uint32_t a, uint32_t b) { (void)h; (void)a; (void)b; return HAL_OK; }
HAL_StatusTypeDef HAL_FDCAN_EnableTxDelayCompensation(FDCAN_HandleTypeDef *h) { (void)h; return HAL_OK; }
HAL_StatusTypeDef HAL_FDCAN_DisableTxDelayCompensation(FDCAN_HandleTypeDef *h) { (void)h; return HAL_OK; }
HAL_StatusTypeDef HAL_FDCAN_EnableISOMode(FDCAN_HandleTypeDef *h) { (void)h; return HAL_OK; }
HAL_StatusTypeDef HAL_FDCAN_DisableISOMode(FDCAN_HandleTypeDef *h) { (void)h; return HAL_OK; }
HAL_StatusTypeDef HAL_FDCAN_ConfigTimestampCounter(FDCAN_HandleTypeDef *h, uint32_t a) { (void)h; (void)a; return HAL_OK; }
HAL_StatusTypeDef HAL_FDCAN_EnableTimestampCounter(FDCAN_HandleTypeDef *h, uint32_t a) { (void)h; (void)a; return HAL_OK; }
HAL_StatusTypeDef HAL_FDCAN_ConfigInterruptLines(FDCAN_HandleTypeDef *h, uint32_t a, uint32_t b) { (void)h; (void)a; (void)b; return HAL_OK; }
HAL_StatusTypeDef HAL_FDCAN_EnableEdgeFiltering(FDCAN_HandleTypeDef *h) { (void)h; return HAL_OK; }

HAL_StatusTypeDef HAL_FDCAN_Start(FDCAN_HandleTypeDef *h)
{
    fdcan_t *p = port_of(h);
    if (h->State != HAL_FDCAN_STATE_READY) { h->ErrorCode |= HAL_FDCAN_ERROR_NOT_READY; return HAL_ERROR; }
    p->started = 1; p->tec = 0; p->lec = 0; h->State = HAL_FDCAN_STATE_BUSY; h->ErrorCode = HAL_FDCAN_ERROR_NONE;
    mirror(p);
    return HAL_OK;
}
HAL_StatusTypeDef HAL_FDCAN_Stop(FDCAN_HandleTypeDef *h)
{
    fdcan_t *p = port_of(h);
    if (h->State != HAL_FDCAN_STATE_BUSY) { h->ErrorCode |= HAL_FDCAN_ERROR_NOT_STARTED; return HAL_ERROR; }
    p->started = 0; p->tx_count = 0; p->tx_pending = 0; p->put_index = 0; p->head_done = 0;
    sim_cancel(tx_complete_isr, p);
    h->State = HAL_FDCAN_STATE_READY;
    mirror(p);
    return HAL_OK;
}

HAL_StatusTypeDef HAL_FDCAN_AddMessageToTxFifoQ(FDCAN_HandleTypeDef *h, const FDCAN_TxHeaderTypeDef *th, const uint8_t *data)
{
    fdcan_t *p = port_of(h);
    sim_charge_ns(800);
    if (h->State != HAL_FDCAN_STATE_BUSY) { h->ErrorCode |= HAL_FDCAN_ERROR_NOT_STARTED; return HAL_ERROR; }
    if (p->tx_count >= 3) { h->ErrorCode |= HAL_FDCAN_ERROR_FIFO_FULL; return HAL_ERROR; }
    uint32_t slot = p->put_index;
    for (int k = 0; k < 3 && (p->tx_pending & (1u << slot)); k++) slot = (slot + 1) % 3;
    frame_t *f = &p->tx[slot];
    f->id = th->Identifier;
    f->flags = (th->IdType == FDCAN_EXTENDED_ID ? SIM_CAN_EXT : 0) | (th->FDFormat == FDCAN_FD_CAN ? SIM_CAN_FD : 0) |
               (th->BitRateSwitch == FDCAN_BRS_ON ? SIM_CAN_BRS : 0) | (th->TxFrameType == FDCAN_REMOTE_FRAME ? SIM_CAN_RTR : 0);
    f->len = dlc_bytes[dlc_code(th->DataLength)];
    if (data && !(f->flags & SIM_CAN_RTR)) memcpy(f->data, data, f->len);
    p->tx_pending |= 1u << slot;
    p->tx_order[p->tx_count++] = slot;
    h->LatestTxFifoQRequest = 1u << slot;
    p->put_index = (slot + 1) % 3;
    mirror(p);
    if (p->tx_count == 1) tx_kick(p);
    if (!p->acked) { p->lec = 3; if (p->tec < 128) p->tec += 8; mirror(p); }
    return HAL_OK;
}
uint32_t HAL_FDCAN_GetTxFifoFreeLevel(const FDCAN_HandleTypeDef *h) { return 3 - port_of(h)->tx_count; }
uint32_t HAL_FDCAN_GetLatestTxFifoQRequestBuffer(const FDCAN_HandleTypeDef *h) { return h->LatestTxFifoQRequest; }
uint32_t HAL_FDCAN_IsTxBufferMessagePending(const FDCAN_HandleTypeDef *h, uint32_t b) { return (port_of(h)->tx_pending & b) ? 1 : 0; }
HAL_StatusTypeDef HAL_FDCAN_AbortTxRequest(FDCAN_HandleTypeDef *h, uint32_t idx)
{
    fdcan_t *p = port_of(h);
    if (h->State != HAL_FDCAN_STATE_BUSY) { h->ErrorCode |= HAL_FDCAN_ERROR_NOT_STARTED; return HAL_ERROR; }
    uint32_t n = 0, keep[3];
    for (uint32_t i = 0; i < p->tx_count; i++) {
        uint32_t s = p->tx_order[i];
        if (idx & (1u << s)) {
            /* a frame already on the wire (head) completes; queued ones are cancelled */
            if (i == 0 && p->acked && p->started) { keep[n++] = s; continue; }
            p->tx_pending &= ~(1u << s);
        } else keep[n++] = s;
    }
    memcpy(p->tx_order, keep, sizeof keep); p->tx_count = n;
    if (!n) sim_cancel(tx_complete_isr, p);
    mirror(p);
    return HAL_OK;
}

static int filter_match(const FDCAN_FilterTypeDef *f, uint32_t id)
{
    switch (f->FilterType) {
    case FDCAN_FILTER_RANGE: return id >= f->FilterID1 && id <= f->FilterID2;
    case FDCAN_FILTER_DUAL: return id == f->FilterID1 || id == f->FilterID2;
    case FDCAN_FILTER_MASK: return (id & f->FilterID2) == (f->FilterID1 & f->FilterID2);
    default: return 0;
    }
}

static void rx_isr(void *arg, uintptr_t fifo)
{
    fdcan_t *p = arg;
    uint32_t it = fifo ? FDCAN_IT_RX_FIFO1_NEW_MESSAGE : FDCAN_IT_RX_FIFO0_NEW_MESSAGE;
    if (fifo) HAL_FDCAN_RxFifo1Callback(p->h, it); else HAL_FDCAN_RxFifo0Callback(p->h, it);
}
static void rx_lost_isr(void *arg, uintptr_t fifo)
{
    fdcan_t *p = arg;
    uint32_t it = fifo ? FDCAN_IT_RX_FIFO1_MESSAGE_LOST : FDCAN_IT_RX_FIFO0_MESSAGE_LOST;
    if (fifo) HAL_FDCAN_RxFifo1Callback(p->h, it); else HAL_FDCAN_RxFifo0Callback(p->h, it);
}

/* Inbound frame at its local delivery time (scheduled as an always-on event, then raised on the IRQ line). */
static void deliver(void *arg, uintptr_t a2)
{
    fdcan_t *p = arg; frame_t *in = (frame_t *)a2;
    if (p->started) {
        int ext = (in->flags & SIM_CAN_EXT) != 0, fifo = -1, matched = 0; uint32_t idx = 0;
        if (!ext) {
            for (uint32_t i = 0; i < p->nfilters; i++) {
                const FDCAN_FilterTypeDef *f = &p->filters[i];
                if (f->IdType != FDCAN_STANDARD_ID || f->FilterConfig == FDCAN_FILTER_DISABLE || !filter_match(f, in->id)) continue;
                matched = 1; idx = f->FilterIndex;
                if (f->FilterConfig == FDCAN_FILTER_TO_RXFIFO0 || f->FilterConfig == FDCAN_FILTER_TO_RXFIFO0_HP) fifo = 0;
                else if (f->FilterConfig == FDCAN_FILTER_TO_RXFIFO1 || f->FilterConfig == FDCAN_FILTER_TO_RXFIFO1_HP) fifo = 1;
                else fifo = -1;
                break;
            }
        }
        if (!matched) {
            uint32_t g = ext ? p->non_ext : p->non_std;
            fifo = g == FDCAN_ACCEPT_IN_RX_FIFO0 ? 0 : g == FDCAN_ACCEPT_IN_RX_FIFO1 ? 1 : -1;
        }
        if (fifo >= 0) {
            if (p->rx_fill[fifo] >= 3) {
                p->lost++;
                if (p->its & (fifo ? FDCAN_IT_RX_FIFO1_MESSAGE_LOST : FDCAN_IT_RX_FIFO0_MESSAGE_LOST))
                    sim_schedule(sim_now_ns(), p->irqn, rx_lost_isr, p, (uintptr_t)fifo);
            } else {
                frame_t *slot = &p->rx[fifo][(p->rx_get[fifo] + p->rx_fill[fifo]) % 3];
                *slot = *in; slot->filter = idx; slot->matched = matched;
                p->rx_fill[fifo]++;
                mirror(p);
                if (p->its & (fifo ? FDCAN_IT_RX_FIFO1_NEW_MESSAGE : FDCAN_IT_RX_FIFO0_NEW_MESSAGE))
                    sim_schedule(sim_now_ns(), p->irqn, rx_isr, p, (uintptr_t)fifo);
            }
        }
    }
    free(in);
}

HAL_StatusTypeDef HAL_FDCAN_GetRxMessage(FDCAN_HandleTypeDef *h, uint32_t loc, FDCAN_RxHeaderTypeDef *rh, uint8_t *data)
{
    fdcan_t *p = port_of(h);
    sim_charge_ns(500);
    int fifo = loc == FDCAN_RX_FIFO1;
    if (h->State != HAL_FDCAN_STATE_BUSY) { h->ErrorCode |= HAL_FDCAN_ERROR_NOT_STARTED; return HAL_ERROR; }
    if (!p->rx_fill[fifo]) { h->ErrorCode |= HAL_FDCAN_ERROR_FIFO_EMPTY; return HAL_ERROR; }
    frame_t *f = &p->rx[fifo][p->rx_get[fifo]];
    rh->Identifier = f->id;
    rh->IdType = (f->flags & SIM_CAN_EXT) ? FDCAN_EXTENDED_ID : FDCAN_STANDARD_ID;
    rh->RxFrameType = (f->flags & SIM_CAN_RTR) ? FDCAN_REMOTE_FRAME : FDCAN_DATA_FRAME;
    uint32_t code = 0; while (code < 15 && dlc_bytes[code] < f->len) code++;
    rh->DataLength = (FDCAN_DLC_BYTES_8 > 15) ? (code << 16) : code;
    rh->ErrorStateIndicator = FDCAN_ESI_ACTIVE;
    rh->BitRateSwitch = (f->flags & SIM_CAN_BRS) ? FDCAN_BRS_ON : FDCAN_BRS_OFF;
    rh->FDFormat = (f->flags & SIM_CAN_FD) ? FDCAN_FD_CAN : FDCAN_CLASSIC_CAN;
    rh->RxTimestamp = (uint32_t)(sim_now_ns() / 1000u) & 0xFFFFu;
    rh->FilterIndex = f->filter;
    rh->IsFilterMatchingFrame = f->matched ? 0 : 1;
    if (data) memcpy(data, f->data, f->len);
    p->rx_get[fifo] = (p->rx_get[fifo] + 1) % 3; p->rx_fill[fifo]--;
    mirror(p);
    return HAL_OK;
}
uint32_t HAL_FDCAN_GetRxFifoFillLevel(const FDCAN_HandleTypeDef *h, uint32_t loc) { return port_of(h)->rx_fill[loc == FDCAN_RX_FIFO1]; }

HAL_StatusTypeDef HAL_FDCAN_ActivateNotification(FDCAN_HandleTypeDef *h, uint32_t its, uint32_t bufs)
{
    fdcan_t *p = port_of(h);
    p->its |= its;
    if (its & FDCAN_IT_TX_COMPLETE) p->tx_buffer_its |= bufs;
    return HAL_OK;
}
HAL_StatusTypeDef HAL_FDCAN_DeactivateNotification(FDCAN_HandleTypeDef *h, uint32_t its)
{
    fdcan_t *p = port_of(h); p->its &= ~its; return HAL_OK;
}
HAL_StatusTypeDef HAL_FDCAN_GetProtocolStatus(const FDCAN_HandleTypeDef *h, FDCAN_ProtocolStatusTypeDef *s)
{
    fdcan_t *p = port_of(h);
    memset(s, 0, sizeof *s);
    s->LastErrorCode = p->lec; s->DataLastErrorCode = 7;
    s->Activity = p->tx_count ? FDCAN_COM_STATE_TX : FDCAN_COM_STATE_IDLE;
    s->ErrorPassive = p->tec >= 128; s->Warning = p->tec >= 96; s->BusOff = 0;
    return HAL_OK;
}
HAL_StatusTypeDef HAL_FDCAN_GetErrorCounters(const FDCAN_HandleTypeDef *h, FDCAN_ErrorCountersTypeDef *c)
{
    fdcan_t *p = port_of(h); memset(c, 0, sizeof *c); c->TxErrorCnt = p->tec > 255 ? 255 : p->tec; return HAL_OK;
}
void HAL_FDCAN_IRQHandler(FDCAN_HandleTypeDef *h) { (void)h; }
HAL_FDCAN_StateTypeDef HAL_FDCAN_GetState(const FDCAN_HandleTypeDef *h) { return h->State; }
uint32_t HAL_FDCAN_GetError(const FDCAN_HandleTypeDef *h) { return h->ErrorCode; }

static void on_can_rx(uint16_t type, const uint8_t *b, uint32_t len)
{
    (void)type;
    struct __attribute__((packed)) { uint64_t t; char port[8]; uint8_t flags, len, pad[2]; uint32_t id; } m;
    if (len < sizeof m) return;
    memcpy(&m, b, sizeof m);
    for (int i = 0; i < MAX_FDCAN; i++) {
        fdcan_t *p = &ports[i];
        if (!p->h || strncmp(p->name, m.port, 8)) continue;
        frame_t *f = calloc(1, sizeof *f);
        f->id = m.id; f->flags = m.flags; f->len = m.len > 64 ? 64 : m.len;
        memcpy(f->data, b + sizeof m, f->len <= len - sizeof m ? f->len : len - sizeof m);
        extern uint64_t sim_global_to_local(uint64_t);
        uint64_t t = sim_global_to_local(m.t);
        if (t < sim_now_ns()) t = sim_now_ns();
        sim_schedule(t, -1, deliver, p, (uintptr_t)f);
        return;
    }
}
static void on_link(uint16_t type, const uint8_t *b, uint32_t len)
{
    (void)type;
    if (len < 9) return;
    for (int i = 0; i < MAX_FDCAN; i++) {
        fdcan_t *p = &ports[i];
        if (!p->h || strncmp(p->name, (const char *)b, 8)) continue;
        int was = p->acked; p->acked = b[8] != 0;
        if (p->acked && !was) { p->tec = 0; p->lec = 0; tx_kick(p); }
        if (!p->acked && was) { sim_cancel(tx_complete_isr, p); if (p->tx_count) { p->lec = 3; p->tec = 128; } }
        mirror(p);
    }
}
__attribute__((constructor)) static void fdcan_register(void)
{
    sim_register_rx(SIM_MSG_CAN_RX, on_can_rx);
    sim_register_rx(SIM_MSG_LINK, on_link);
}
#endif
