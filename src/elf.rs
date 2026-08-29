use crate::layout::MemoryLayout;
use anyhow::{ensure, Context, Result};
use std::{fs, path::Path};

const PT_LOAD: u32 = 1;
const ELF32_HEADER_SIZE: usize = 52;
const ELF32_PROGRAM_HEADER_SIZE: usize = 32;

pub fn validate_elf(path: &Path, memory: &MemoryLayout, label: &str) -> Result<()> {
    let bytes =
        fs::read(path).with_context(|| format!("reading {label} ELF {}", path.display()))?;
    ensure!(
        bytes.len() >= ELF32_HEADER_SIZE,
        "{label} ELF header is truncated"
    );
    ensure!(&bytes[0..4] == b"\x7fELF", "{label} is not an ELF file");
    ensure!(bytes[4] == 1, "{label} must be a 32-bit ELF");
    ensure!(bytes[5] == 1, "{label} must be a little-endian ELF");

    let entry = u32_at(&bytes, 24)? as u64;
    ensure!(
        fits_flash(entry, 1, memory),
        "{label} entry point 0x{entry:08x} is outside physical flash"
    );
    let phoff = u32_at(&bytes, 28)? as usize;
    let phentsize = u16_at(&bytes, 42)? as usize;
    let phnum = u16_at(&bytes, 44)? as usize;
    ensure!(
        phentsize >= ELF32_PROGRAM_HEADER_SIZE,
        "{label} ELF program header is too small"
    );
    ensure!(phnum > 0, "{label} ELF has no program headers");

    for index in 0..phnum {
        let offset = phoff
            .checked_add(
                index
                    .checked_mul(phentsize)
                    .context("ELF program header overflow")?,
            )
            .context("ELF program header overflow")?;
        ensure!(
            offset + ELF32_PROGRAM_HEADER_SIZE <= bytes.len(),
            "{label} ELF program header is truncated"
        );
        if u32_at(&bytes, offset)? != PT_LOAD {
            continue;
        }
        let file_offset = u32_at(&bytes, offset + 4)? as u64;
        let virtual_address = u32_at(&bytes, offset + 8)? as u64;
        let physical_address = u32_at(&bytes, offset + 12)? as u64;
        let file_size = u32_at(&bytes, offset + 16)? as u64;
        let memory_size = u32_at(&bytes, offset + 20)? as u64;
        ensure!(
            file_size <= memory_size,
            "{label} ELF LOAD {index} has filesz larger than memsz"
        );
        ensure!(
            file_offset
                .checked_add(file_size)
                .is_some_and(|end| end <= bytes.len() as u64),
            "{label} ELF LOAD {index} data is truncated"
        );
        ensure!(
            fits_physical(virtual_address, memory_size, memory),
            "{label} ELF LOAD {index} virtual range 0x{virtual_address:08x}..0x{:08x} exceeds every physical flash/RAM region",
            virtual_address.saturating_add(memory_size)
        );
        if file_size > 0 {
            ensure!(
                fits_physical(physical_address, file_size, memory),
                "{label} ELF LOAD {index} load range 0x{physical_address:08x}..0x{:08x} exceeds every physical flash/RAM region",
                physical_address.saturating_add(file_size)
            );
        }
    }
    Ok(())
}

fn fits_physical(base: u64, size: u64, memory: &MemoryLayout) -> bool {
    fits_flash(base, size, memory)
        || memory
            .ram_regions
            .iter()
            .any(|region| fits(base, size, region.base, region.size))
}

fn fits_flash(base: u64, size: u64, memory: &MemoryLayout) -> bool {
    fits(base, size, memory.flash_base, memory.flash_size)
}

fn fits(base: u64, size: u64, region_base: u64, region_size: u64) -> bool {
    if size == 0 {
        return base >= region_base && base <= region_base.saturating_add(region_size);
    }
    match (base.checked_add(size), region_base.checked_add(region_size)) {
        (Some(end), Some(region_end)) => base >= region_base && end <= region_end,
        _ => false,
    }
}

fn u16_at(bytes: &[u8], offset: usize) -> Result<u16> {
    let value = bytes
        .get(offset..offset + 2)
        .context("truncated ELF field")?;
    Ok(u16::from_le_bytes([value[0], value[1]]))
}

fn u32_at(bytes: &[u8], offset: usize) -> Result<u32> {
    let value = bytes
        .get(offset..offset + 4)
        .context("truncated ELF field")?;
    Ok(u32::from_le_bytes([value[0], value[1], value[2], value[3]]))
}

#[cfg(test)]
mod tests {
    use super::validate_elf;
    use crate::layout::{MemoryLayout, MemoryRegion};
    use std::{fs, path::Path};

    fn memory() -> MemoryLayout {
        MemoryLayout {
            flash_base: 0x0800_0000,
            flash_size: 0x80000,
            ram_regions: vec![MemoryRegion {
                name: "sram".into(),
                base: 0x2000_0000,
                size: 0x1c000,
            }],
            bootloader_size: 0x4000,
            slot_a_base: 0x0800_4000,
            slot_a_size: 0x74000,
            slot_b_base: None,
            slot_b_size: None,
            delta_base: None,
            delta_size: None,
            erase_size: 0x800,
            write_alignment: 8,
            sedsnet_pool: 4096,
        }
    }

    fn elf(load_vaddr: u32, load_paddr: u32, filesz: u32, memsz: u32) -> Vec<u8> {
        let mut data = vec![0_u8; 52 + 32 + filesz as usize];
        data[0..6].copy_from_slice(b"\x7fELF\x01\x01");
        data[24..28].copy_from_slice(&0x0800_4001_u32.to_le_bytes());
        data[28..32].copy_from_slice(&52_u32.to_le_bytes());
        data[42..44].copy_from_slice(&32_u16.to_le_bytes());
        data[44..46].copy_from_slice(&1_u16.to_le_bytes());
        data[52..56].copy_from_slice(&1_u32.to_le_bytes());
        data[56..60].copy_from_slice(&84_u32.to_le_bytes());
        data[60..64].copy_from_slice(&load_vaddr.to_le_bytes());
        data[64..68].copy_from_slice(&load_paddr.to_le_bytes());
        data[68..72].copy_from_slice(&filesz.to_le_bytes());
        data[72..76].copy_from_slice(&memsz.to_le_bytes());
        data
    }

    fn write(path: &Path, bytes: &[u8]) {
        fs::write(path, bytes).unwrap();
    }

    #[test]
    fn accepts_segment_at_exact_ram_limit() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("ok.elf");
        write(&path, &elf(0x2000_0000, 0x0800_5000, 4, 0x1c000));
        validate_elf(&path, &memory(), "firmware").unwrap();
    }

    #[test]
    fn rejects_segment_one_byte_beyond_ram() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("oom.elf");
        write(&path, &elf(0x2000_0000, 0x0800_5000, 4, 0x1c001));
        let error = validate_elf(&path, &memory(), "firmware")
            .unwrap_err()
            .to_string();
        assert!(error.contains("exceeds every physical flash/RAM region"));
    }

    #[test]
    fn rejects_segment_one_byte_beyond_flash() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("oversize.elf");
        write(&path, &elf(0x0800_0000, 0x0800_0000, 0x80001, 0x80001));
        let error = validate_elf(&path, &memory(), "firmware")
            .unwrap_err()
            .to_string();
        assert!(error.contains("exceeds every physical flash/RAM region"));
    }
}
