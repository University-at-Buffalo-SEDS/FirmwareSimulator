/* SD card (SDMMC) as a sparse host image under the node's state directory (persists across reboots; FileX and
 * the board's FileX driver run unchanged above it), and the HASH peripheral's SHA-256 accumulate/finish API. */
#include "sim.h"
#include "main.h"
#include <fcntl.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

#ifdef HAL_SD_MODULE_ENABLED
static int sd_fd = -1;
static uint64_t sd_bytes;
/* Card out of its slot: SIM_SD_ABSENT at boot, or the plant's "sdcard" device message ("absent=0|1") at run time.
 * Removing the card closes the image, so a card the launcher rewrote meanwhile (another IGT dataset) is read fresh. */
static int sd_absent = -1;
void sim_sd_set(const char *csv)
{
    if (!strncmp(csv, "absent=", 7)) {
        sd_absent = atoi(csv + 7) ? 1 : 0;
        if (sd_absent && sd_fd >= 0) { close(sd_fd); sd_fd = -1; }
    }
}
__attribute__((weak)) void HAL_SD_TxCpltCallback(SD_HandleTypeDef *h) { (void)h; }
__attribute__((weak)) void HAL_SD_RxCpltCallback(SD_HandleTypeDef *h) { (void)h; }
__attribute__((weak)) void HAL_SD_ErrorCallback(SD_HandleTypeDef *h) { (void)h; }
__attribute__((weak)) void HAL_SD_AbortCallback(SD_HandleTypeDef *h) { (void)h; }
__attribute__((weak)) void HAL_SD_MspInit(SD_HandleTypeDef *h) { (void)h; }
__attribute__((weak)) void HAL_SD_MspDeInit(SD_HandleTypeDef *h) { (void)h; }

HAL_StatusTypeDef HAL_SD_Init(SD_HandleTypeDef *h)
{
    if (h->State == HAL_SD_STATE_RESET) { h->Lock = HAL_UNLOCKED; HAL_SD_MspInit(h); }
    if (sd_absent < 0) sd_absent = sim_env_long("SIM_SD_ABSENT", 0) ? 1 : 0;
    if (sd_absent) { h->ErrorCode = HAL_SD_ERROR_TIMEOUT; h->State = HAL_SD_STATE_ERROR; return HAL_ERROR; }
    if (sd_fd < 0) {
        char path[1024]; snprintf(path, sizeof path, "%s/sdcard.img", sim_state_dir());
        sd_fd = open(path, O_RDWR | O_CREAT, 0644);
        sd_bytes = (uint64_t)sim_env_long("SIM_SD_MB", 1024) << 20;
        if (sd_fd < 0 || ftruncate(sd_fd, (off_t)sd_bytes) != 0) { h->State = HAL_SD_STATE_ERROR; return HAL_ERROR; }
    }
    sim_busy_wait_ns(20 * SIM_NS_PER_MS);  /* card identification */
    h->SdCard.CardType = CARD_SDHC_SDXC; h->SdCard.CardVersion = CARD_V2_X; h->SdCard.Class = 0x5B5;
    h->SdCard.RelCardAdd = 1; h->SdCard.BlockNbr = (uint32_t)(sd_bytes / 512); h->SdCard.BlockSize = 512;
    h->SdCard.LogBlockNbr = h->SdCard.BlockNbr; h->SdCard.LogBlockSize = 512;
    h->ErrorCode = HAL_SD_ERROR_NONE; h->State = HAL_SD_STATE_READY; h->Context = SD_CONTEXT_NONE;
    return HAL_OK;
}
HAL_StatusTypeDef HAL_SD_DeInit(SD_HandleTypeDef *h) { HAL_SD_MspDeInit(h); h->State = HAL_SD_STATE_RESET; return HAL_OK; }
HAL_StatusTypeDef HAL_SD_ConfigWideBusOperation(SD_HandleTypeDef *h, uint32_t w) { (void)h; (void)w; return HAL_OK; }
HAL_StatusTypeDef HAL_SD_GetCardInfo(const SD_HandleTypeDef *h, HAL_SD_CardInfoTypeDef *ci) { *ci = h->SdCard; return HAL_OK; }
HAL_SD_CardStateTypeDef HAL_SD_GetCardState(SD_HandleTypeDef *h) { return h->State == HAL_SD_STATE_BUSY ? HAL_SD_CARD_RECEIVING : HAL_SD_CARD_TRANSFER; }
void HAL_SD_IRQHandler(SD_HandleTypeDef *h) { (void)h; }
HAL_StatusTypeDef HAL_SD_Abort(SD_HandleTypeDef *h) { h->State = HAL_SD_STATE_READY; return HAL_OK; }
static uint64_t sd_ns(uint32_t blocks) { return 300000u + (uint64_t)blocks * 512u * 1000000000ull / (12u << 20); } /* ~12 MB/s */
static void sd_done(void *arg, uintptr_t write)
{
    SD_HandleTypeDef *h = arg;
    h->State = HAL_SD_STATE_READY; h->Context = SD_CONTEXT_NONE;
    if (write) HAL_SD_TxCpltCallback(h); else HAL_SD_RxCpltCallback(h);
}
static void sd_err(void *arg, uintptr_t a2) { (void)a2; SD_HandleTypeDef *h = arg; h->State = HAL_SD_STATE_READY; h->ErrorCode = HAL_SD_ERROR_DATA_TIMEOUT; HAL_SD_ErrorCallback(h); }
static HAL_StatusTypeDef sd_io(SD_HandleTypeDef *h, uint8_t *data, uint32_t block, uint32_t n, int write)
{
    if (sd_absent == 1 || sd_fd < 0) { h->ErrorCode |= HAL_SD_ERROR_TIMEOUT; return HAL_ERROR; }
    if (h->State != HAL_SD_STATE_READY) { h->ErrorCode |= HAL_SD_ERROR_BUSY; return HAL_ERROR; }
    if ((uint64_t)(block + n) * 512u > sd_bytes) { h->ErrorCode |= HAL_SD_ERROR_ADDR_OUT_OF_RANGE; return HAL_ERROR; }
    h->State = HAL_SD_STATE_BUSY; h->ErrorCode = HAL_SD_ERROR_NONE;
    ssize_t r = write ? pwrite(sd_fd, data, (size_t)n * 512u, (off_t)block * 512) : pread(sd_fd, data, (size_t)n * 512u, (off_t)block * 512);
    if (sim_env_long("SIM_SD_FAIL", 0) || r != (ssize_t)n * 512) { sim_schedule(sim_now_ns() + sd_ns(n), -1, sd_err, h, 0); return HAL_OK; }
    sim_schedule(sim_now_ns() + sd_ns(n), -1, sd_done, h, (uintptr_t)write);
    return HAL_OK;
}
HAL_StatusTypeDef HAL_SD_ReadBlocks_DMA(SD_HandleTypeDef *h, uint8_t *p, uint32_t b, uint32_t n) { return sd_io(h, p, b, n, 0); }
HAL_StatusTypeDef HAL_SD_WriteBlocks_DMA(SD_HandleTypeDef *h, const uint8_t *p, uint32_t b, uint32_t n) { return sd_io(h, (uint8_t *)p, b, n, 1); }
HAL_StatusTypeDef HAL_SD_ReadBlocks(SD_HandleTypeDef *h, uint8_t *p, uint32_t b, uint32_t n, uint32_t t)
{
    (void)t; if ((uint64_t)(b + n) * 512u > sd_bytes) return HAL_ERROR;
    if (pread(sd_fd, p, (size_t)n * 512u, (off_t)b * 512) != (ssize_t)n * 512) return HAL_ERROR;
    sim_busy_wait_ns(sd_ns(n)); return HAL_OK;
}
HAL_StatusTypeDef HAL_SD_WriteBlocks(SD_HandleTypeDef *h, const uint8_t *p, uint32_t b, uint32_t n, uint32_t t)
{
    (void)t; if ((uint64_t)(b + n) * 512u > sd_bytes) return HAL_ERROR;
    if (pwrite(sd_fd, p, (size_t)n * 512u, (off_t)b * 512) != (ssize_t)n * 512) return HAL_ERROR;
    sim_busy_wait_ns(sd_ns(n)); return HAL_OK;
}
HAL_StatusTypeDef HAL_SD_Erase(SD_HandleTypeDef *h, uint32_t a, uint32_t b) { (void)h; (void)a; (void)b; return HAL_OK; }
#endif

#ifdef HAL_HASH_MODULE_ENABLED
/* SHA-256 (FIPS 180-4) */
typedef struct { uint32_t h[8]; uint64_t len; uint8_t buf[64]; uint32_t used; int active; } sha_t;
static sha_t sha;
static const uint32_t K[64] = {
    0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, 0x3956c25b, 0x59f111f1, 0x923f82a4, 0xab1c5ed5, 0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3,
    0x72be5d74, 0x80deb1fe, 0x9bdc06a7, 0xc19bf174, 0xe49b69c1, 0xefbe4786, 0x0fc19dc6, 0x240ca1cc, 0x2de92c6f, 0x4a7484aa, 0x5cb0a9dc, 0x76f988da,
    0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7, 0xc6e00bf3, 0xd5a79147, 0x06ca6351, 0x14292967, 0x27b70a85, 0x2e1b2138, 0x4d2c6dfc, 0x53380d13,
    0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85, 0xa2bfe8a1, 0xa81a664b, 0xc24b8b70, 0xc76c51a3, 0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070,
    0x19a4c116, 0x1e376c08, 0x2748774c, 0x34b0bcb5, 0x391c0cb3, 0x4ed8aa4a, 0x5b9cca4f, 0x682e6ff3, 0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208,
    0x90befffa, 0xa4506ceb, 0xbef9a3f7, 0xc67178f2};
#define ROR(x, n) (((x) >> (n)) | ((x) << (32 - (n))))
static void sha_block(sha_t *s, const uint8_t *p)
{
    uint32_t w[64], a, b, c, d, e, f, g, h;
    for (int i = 0; i < 16; i++) w[i] = (uint32_t)p[4 * i] << 24 | (uint32_t)p[4 * i + 1] << 16 | (uint32_t)p[4 * i + 2] << 8 | p[4 * i + 3];
    for (int i = 16; i < 64; i++) {
        uint32_t s0 = ROR(w[i - 15], 7) ^ ROR(w[i - 15], 18) ^ (w[i - 15] >> 3), s1 = ROR(w[i - 2], 17) ^ ROR(w[i - 2], 19) ^ (w[i - 2] >> 10);
        w[i] = w[i - 16] + s0 + w[i - 7] + s1;
    }
    a = s->h[0]; b = s->h[1]; c = s->h[2]; d = s->h[3]; e = s->h[4]; f = s->h[5]; g = s->h[6]; h = s->h[7];
    for (int i = 0; i < 64; i++) {
        uint32_t t1 = h + (ROR(e, 6) ^ ROR(e, 11) ^ ROR(e, 25)) + ((e & f) ^ (~e & g)) + K[i] + w[i];
        uint32_t t2 = (ROR(a, 2) ^ ROR(a, 13) ^ ROR(a, 22)) + ((a & b) ^ (a & c) ^ (b & c));
        h = g; g = f; f = e; e = d + t1; d = c; c = b; b = a; a = t1 + t2;
    }
    s->h[0] += a; s->h[1] += b; s->h[2] += c; s->h[3] += d; s->h[4] += e; s->h[5] += f; s->h[6] += g; s->h[7] += h;
}
static void sha_start(sha_t *s)
{
    static const uint32_t iv[8] = {0x6a09e667, 0xbb67ae85, 0x3c6ef372, 0xa54ff53a, 0x510e527f, 0x9b05688c, 0x1f83d9ab, 0x5be0cd19};
    memcpy(s->h, iv, sizeof iv); s->len = 0; s->used = 0; s->active = 1;
}
static void sha_update(sha_t *s, const uint8_t *p, uint32_t n)
{
    s->len += n;
    while (n--) { s->buf[s->used++] = *p++; if (s->used == 64) { sha_block(s, s->buf); s->used = 0; } }
}
static void sha_final(sha_t *s, uint8_t out[32])
{
    uint64_t bits = s->len * 8; uint8_t pad = 0x80, z = 0;
    sha_update(s, &pad, 1);
    while (s->used != 56) sha_update(s, &z, 1);
    for (int i = 7; i >= 0; i--) { uint8_t b = (uint8_t)(bits >> (8 * i)); sha_update(s, &b, 1); }
    for (int i = 0; i < 8; i++) { out[4 * i] = (uint8_t)(s->h[i] >> 24); out[4 * i + 1] = (uint8_t)(s->h[i] >> 16); out[4 * i + 2] = (uint8_t)(s->h[i] >> 8); out[4 * i + 3] = (uint8_t)s->h[i]; }
    s->active = 0;
}
HAL_StatusTypeDef HAL_HASH_Init(HASH_HandleTypeDef *h) { (void)h; sha.active = 0; return HAL_OK; }
HAL_StatusTypeDef HAL_HASH_DeInit(HASH_HandleTypeDef *h) { (void)h; sha.active = 0; return HAL_OK; }
HAL_StatusTypeDef HAL_HASHEx_SHA256_Accmlt(HASH_HandleTypeDef *h, const uint8_t *const in, uint32_t n)
{
    (void)h; if (!sha.active) sha_start(&sha); sha_update(&sha, in, n); sim_charge_ns((uint64_t)n * 20u); return HAL_OK;
}
HAL_StatusTypeDef HAL_HASHEx_SHA256_Accmlt_End(HASH_HandleTypeDef *h, const uint8_t *const in, uint32_t n, uint8_t *out, uint32_t t)
{
    (void)h; (void)t; if (!sha.active) sha_start(&sha); sha_update(&sha, in, n); sha_final(&sha, out); return HAL_OK;
}
#endif
