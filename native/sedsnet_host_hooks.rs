//! Host (i686 Linux) build of a board's embedded SEDSnet SDK for native SITL.
//! The bare-metal SDK build supplies its allocator and panic handler only for target_os = "none"; this wrapper
//! supplies the same hooks for the host target, so the SDK allocates from the board's own pool
//! (telemetryMalloc/telemetryFree, the board's TLSF/byte pool) and panics through telemetry_panic_hook.
#![no_std]
extern crate sedsnet;
pub use sedsnet::*;

use core::alloc::{GlobalAlloc, Layout};
use core::mem::size_of;

unsafe extern "C" {
    fn telemetryMalloc(size: usize) -> *mut core::ffi::c_void;
    fn telemetryFree(ptr: *mut core::ffi::c_void);
    fn telemetry_panic_hook(msg: *const u8, len: usize);
    fn sim_rust_panic(msg: *const u8, len: usize) -> !;
}

struct TelemetryAlloc;

#[inline]
fn align_up(addr: usize, align: usize) -> usize {
    (addr + (align - 1)) & !(align - 1)
}

unsafe impl GlobalAlloc for TelemetryAlloc {
    unsafe fn alloc(&self, layout: Layout) -> *mut u8 {
        let align = layout.align().max(size_of::<usize>());
        let header = size_of::<usize>();
        let total = match layout.size().checked_add(align).and_then(|v| v.checked_add(header)) {
            Some(v) => v,
            None => return core::ptr::null_mut(),
        };
        let raw = unsafe { telemetryMalloc(total) as *mut u8 };
        if raw.is_null() {
            return core::ptr::null_mut();
        }
        let base = raw as usize + header;
        let aligned = align_up(base, align) as *mut u8;
        unsafe { *(aligned as *mut usize).offset(-1) = raw as usize };
        aligned
    }
    unsafe fn dealloc(&self, ptr: *mut u8, _layout: Layout) {
        if ptr.is_null() {
            return;
        }
        let raw = unsafe { *(ptr as *mut usize).offset(-1) as *mut core::ffi::c_void };
        unsafe { telemetryFree(raw) };
    }
}

#[global_allocator]
static A: TelemetryAlloc = TelemetryAlloc;

#[panic_handler]
fn panic(_info: &core::panic::PanicInfo) -> ! {
    let msg = b"rust panic";
    unsafe {
        telemetry_panic_hook(msg.as_ptr(), msg.len());
        sim_rust_panic(msg.as_ptr(), msg.len())
    }
}
