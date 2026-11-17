/* simclock: the host station's clocks follow the native bay's virtual clock (LD_PRELOAD, x86_64).
 *
 * The coordinator publishes, before each lockstep window [t, horizon], the window and its current virtual/wall rate in
 * a shared page (SIMCLOCK_FILE). Every clock the process reads is derived from it: REALTIME = SIMCLOCK_EPOCH_MS +
 * virtual, the MONOTONIC family = a fixed base + virtual, never past the window's horizon, never backwards. Timed waits
 * (sleeps, epoll/poll, futex waits under Mutex/Condvar/park) are converted to short wall slices of their remaining
 * virtual time at the current rate and re-checked, so timers fire at their virtual deadlines whatever the bay's speed:
 * the station runs faster than real time when the bay does, and waits when the bay stalls (host load) instead of
 * running ahead of it. No station code changes: it reads time through the C library like any process.
 * Unset SIMCLOCK_FILE: every call passes straight through. */
#define _GNU_SOURCE
#include <dlfcn.h>
#include <errno.h>
#include <fcntl.h>
#include <linux/futex.h>
#include <poll.h>
#include <signal.h>
#include <stdarg.h>
#include <stdatomic.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#include <sys/epoll.h>
#include <sys/mman.h>
#include <sys/syscall.h>
#include <sys/time.h>
#include <time.h>
#include <unistd.h>

typedef struct {
    volatile uint64_t seq, virt_ns, horizon_ns, real_mono_ns, rate_milli;
} page_t;

static page_t *page;
static int64_t epoch_ns;
static const int64_t MONO_BASE_NS = 1000000000000ll; /* 1000 s: an ordinary uptime look */
static _Atomic int64_t last_virtual;
static int (*real_clock_gettime)(clockid_t, struct timespec *);
static int (*real_epoll_wait)(int, struct epoll_event *, int, int);
static int (*real_epoll_pwait)(int, struct epoll_event *, int, int, const sigset_t *);
static int (*real_poll)(struct pollfd *, nfds_t, int);
static int (*real_ppoll)(struct pollfd *, nfds_t, const struct timespec *, const sigset_t *);
static int (*real_nanosleep)(const struct timespec *, struct timespec *);
static int (*real_clock_nanosleep)(clockid_t, int, const struct timespec *, struct timespec *);
static long (*real_syscall)(long, ...);
static int inited;
#define SLICE_NS 2000000ll /* re-check virtual deadlines at least every 2 ms of wall time */

static void init(void)
{
    if (inited) return;
    real_clock_gettime = dlsym(RTLD_NEXT, "clock_gettime");
    real_epoll_wait = dlsym(RTLD_NEXT, "epoll_wait");
    real_epoll_pwait = dlsym(RTLD_NEXT, "epoll_pwait");
    real_poll = dlsym(RTLD_NEXT, "poll");
    real_ppoll = dlsym(RTLD_NEXT, "ppoll");
    real_nanosleep = dlsym(RTLD_NEXT, "nanosleep");
    real_clock_nanosleep = dlsym(RTLD_NEXT, "clock_nanosleep");
    real_syscall = dlsym(RTLD_NEXT, "syscall");
    const char *path = getenv("SIMCLOCK_FILE");
    if (path && *path) {
        int fd = open(path, O_RDONLY | O_CLOEXEC);
        if (fd >= 0) {
            void *p = mmap(NULL, 4096, PROT_READ, MAP_SHARED, fd, 0);
            if (p != MAP_FAILED) page = p;
            close(fd);
        }
        const char *e = getenv("SIMCLOCK_EPOCH_MS");
        epoch_ns = (e ? strtoll(e, NULL, 10) : 1767225600000ll) * 1000000ll;
    }
    inited = 1;
}

static int64_t real_mono(void)
{
    struct timespec ts; real_clock_gettime(CLOCK_MONOTONIC, &ts);
    return (int64_t)ts.tv_sec * 1000000000ll + ts.tv_nsec;
}

typedef struct { int64_t virt, horizon, real, rate; } snap_t;
static snap_t snapshot(void)
{
    snap_t s;
    for (;;) {
        uint64_t a = __atomic_load_n(&page->seq, __ATOMIC_ACQUIRE);
        if (a & 1) continue;
        s.virt = (int64_t)page->virt_ns; s.horizon = (int64_t)page->horizon_ns;
        s.real = (int64_t)page->real_mono_ns; s.rate = (int64_t)page->rate_milli;
        if (__atomic_load_n(&page->seq, __ATOMIC_ACQUIRE) == a) return s;
    }
}

/* virtual ns now: the window start plus elapsed wall time at the current rate, capped at the window's horizon */
static int64_t vnow(void)
{
    snap_t s = snapshot();
    int64_t el = real_mono() - s.real; if (el < 0) el = 0;
    int64_t v = s.virt + (int64_t)((__int128)el * s.rate / 1000);
    if (v > s.horizon) v = s.horizon;
    int64_t last = atomic_load(&last_virtual);
    while (v > last && !atomic_compare_exchange_weak(&last_virtual, &last, v)) {}
    return v > last ? v : last;
}
static int64_t rate_milli(void) { int64_t r = snapshot().rate; return r > 0 ? r : 1; }

static int is_mono(clockid_t c)
{
    return c == CLOCK_MONOTONIC || c == CLOCK_MONOTONIC_RAW || c == CLOCK_MONOTONIC_COARSE || c == CLOCK_BOOTTIME;
}
static int is_real(clockid_t c) { return c == CLOCK_REALTIME || c == CLOCK_REALTIME_COARSE || c == CLOCK_TAI; }
static int64_t fake_clock(clockid_t c) { return (is_real(c) ? epoch_ns : MONO_BASE_NS) + vnow(); }
static void to_ts(int64_t ns, struct timespec *ts) { ts->tv_sec = ns / 1000000000ll; ts->tv_nsec = ns % 1000000000ll; }
static int64_t from_ts(const struct timespec *ts) { return (int64_t)ts->tv_sec * 1000000000ll + ts->tv_nsec; }

/* wall time to wait for `remaining` virtual ns: one slice at most */
static int64_t wall_slice(int64_t remaining)
{
    if (remaining <= 0) return 0;
    int64_t w = (int64_t)((__int128)remaining * 1000 / rate_milli());
    return w < SLICE_NS ? (w > 0 ? w : 1000) : SLICE_NS;
}

int clock_gettime(clockid_t c, struct timespec *ts)
{
    init();
    if (!page || !(is_mono(c) || is_real(c))) return real_clock_gettime(c, ts);
    to_ts(fake_clock(c), ts);
    return 0;
}
int gettimeofday(struct timeval *tv, void *tz)
{
    init();
    if (!page) { struct timespec ts; real_clock_gettime(CLOCK_REALTIME, &ts); if (tv) { tv->tv_sec = ts.tv_sec; tv->tv_usec = ts.tv_nsec / 1000; } return 0; }
    (void)tz;
    int64_t ns = fake_clock(CLOCK_REALTIME);
    if (tv) { tv->tv_sec = ns / 1000000000ll; tv->tv_usec = (ns % 1000000000ll) / 1000; }
    return 0;
}
time_t time(time_t *t)
{
    init();
    struct timespec ts; clock_gettime(CLOCK_REALTIME, &ts);
    if (t) *t = ts.tv_sec;
    return ts.tv_sec;
}

static int sleep_until_virtual(int64_t deadline_v)
{
    for (;;) {
        int64_t rem = deadline_v - vnow();
        if (rem <= 0) return 0;
        struct timespec w; to_ts(wall_slice(rem), &w);
        int r = real_clock_nanosleep(CLOCK_MONOTONIC, 0, &w, NULL);
        if (r == EINTR) return EINTR;
    }
}
int nanosleep(const struct timespec *req, struct timespec *rem)
{
    init();
    if (!page) return real_nanosleep(req, rem);
    int r = sleep_until_virtual(vnow() + from_ts(req));
    if (r) { errno = r; return -1; }
    return 0;
}
int clock_nanosleep(clockid_t c, int flags, const struct timespec *req, struct timespec *rem)
{
    init();
    if (!page || !(is_mono(c) || is_real(c))) return real_clock_nanosleep(c, flags, req, rem);
    int64_t deadline = (flags & TIMER_ABSTIME) ? from_ts(req) - (is_real(c) ? epoch_ns : MONO_BASE_NS) : vnow() + from_ts(req);
    return sleep_until_virtual(deadline);
}

/* readiness waits: a finite timeout becomes slices of virtual time; returning 0 early lets the caller (tokio's timer
 * driver) re-check its clock, so it only reports a timeout once the virtual deadline has passed */
static int ms_slice(int timeout_ms, int64_t *deadline_v)
{
    if (timeout_ms < 0) return -1;
    if (timeout_ms == 0) return 0;
    if (!*deadline_v) *deadline_v = vnow() + (int64_t)timeout_ms * 1000000ll;
    int64_t w = wall_slice(*deadline_v - vnow());
    return w <= 0 ? 0 : (int)((w + 999999) / 1000000);
}
int epoll_wait(int fd, struct epoll_event *ev, int n, int timeout)
{
    init();
    if (!page || timeout <= 0) return real_epoll_wait(fd, ev, n, timeout);
    int64_t deadline = 0;
    for (;;) {
        int r = real_epoll_wait(fd, ev, n, ms_slice(timeout, &deadline));
        if (r != 0 || vnow() >= deadline) return r;
    }
}
int epoll_pwait(int fd, struct epoll_event *ev, int n, int timeout, const sigset_t *ss)
{
    init();
    if (!page || timeout <= 0) return real_epoll_pwait(fd, ev, n, timeout, ss);
    int64_t deadline = 0;
    for (;;) {
        int r = real_epoll_pwait(fd, ev, n, ms_slice(timeout, &deadline), ss);
        if (r != 0 || vnow() >= deadline) return r;
    }
}
int poll(struct pollfd *f, nfds_t n, int timeout)
{
    init();
    if (!page || timeout <= 0) return real_poll(f, n, timeout);
    int64_t deadline = 0;
    for (;;) {
        int r = real_poll(f, n, ms_slice(timeout, &deadline));
        if (r != 0 || vnow() >= deadline) return r;
    }
}
int ppoll(struct pollfd *f, nfds_t n, const struct timespec *ts, const sigset_t *ss)
{
    init();
    if (!page || !ts || (ts->tv_sec == 0 && ts->tv_nsec == 0)) return real_ppoll(f, n, ts, ss);
    int64_t deadline = vnow() + from_ts(ts);
    for (;;) {
        struct timespec w; to_ts(wall_slice(deadline - vnow()), &w);
        int r = real_ppoll(f, n, &w, ss);
        if (r != 0 || vnow() >= deadline) return r;
    }
}

/* Rust std parks threads (Mutex/Condvar/thread::park_timeout, tokio's blocking pool) with raw futex syscalls. */
long syscall(long nr, ...)
{
    init();
    va_list ap; va_start(ap, nr);
    long a1 = va_arg(ap, long), a2 = va_arg(ap, long), a3 = va_arg(ap, long), a4 = va_arg(ap, long), a5 = va_arg(ap, long), a6 = va_arg(ap, long);
    va_end(ap);
    if (!page || nr != SYS_futex || !a4) return real_syscall(nr, a1, a2, a3, a4, a5, a6);
    int op = (int)a2, cmd = op & FUTEX_CMD_MASK;
    if (cmd != FUTEX_WAIT && cmd != FUTEX_WAIT_BITSET) return real_syscall(nr, a1, a2, a3, a4, a5, a6);
    const struct timespec *t = (const struct timespec *)a4;
    int realtime = (op & FUTEX_CLOCK_REALTIME) != 0;
    int64_t deadline = cmd == FUTEX_WAIT ? vnow() + from_ts(t) : from_ts(t) - (realtime ? epoch_ns : MONO_BASE_NS);
    for (;;) {
        int64_t w = wall_slice(deadline - vnow());
        if (w <= 0) { errno = ETIMEDOUT; return -1; }
        struct timespec ts;
        long r;
        if (cmd == FUTEX_WAIT) {
            to_ts(w, &ts);
            r = real_syscall(nr, a1, a2, a3, (long)&ts, a5, a6);
        } else {
            struct timespec now; real_clock_gettime(realtime ? CLOCK_REALTIME : CLOCK_MONOTONIC, &now);
            to_ts(from_ts(&now) + w, &ts);
            r = real_syscall(nr, a1, a2, a3, (long)&ts, a5, a6);
        }
        if (r == 0 || errno != ETIMEDOUT) return r;
        if (vnow() >= deadline) { errno = ETIMEDOUT; return -1; }
    }
}
