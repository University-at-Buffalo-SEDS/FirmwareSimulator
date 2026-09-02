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

pub fn reset_vector(path: &Path, memory: &MemoryLayout, label: &str) -> Result<(u32, u32, u32)> {
    let bytes =
        fs::read(path).with_context(|| format!("reading {label} ELF {}", path.display()))?;
    ensure!(
        bytes.len() >= ELF32_HEADER_SIZE,
        "{label} ELF header is truncated"
    );
    ensure!(
        &bytes[0..6] == b"\x7fELF\x01\x01",
        "{label} must be a 32-bit little-endian ELF"
    );
    let entry = u32_at(&bytes, 24)?;
    let phoff = u32_at(&bytes, 28)? as usize;
    let phentsize = u16_at(&bytes, 42)? as usize;
    let phnum = u16_at(&bytes, 44)? as usize;
    ensure!(
        phentsize >= ELF32_PROGRAM_HEADER_SIZE,
        "{label} ELF program header is too small"
    );
    ensure!(phnum > 0, "{label} ELF has no program headers");
    for index in 0..phnum {
        let header = program_header_offset(phoff, phentsize, index, bytes.len(), label)?;
        if u32_at(&bytes, header)? != PT_LOAD {
            continue;
        }
        let file_offset = u32_at(&bytes, header + 4)? as usize;
        let physical_address = u32_at(&bytes, header + 12)?;
        let file_size = u32_at(&bytes, header + 16)? as usize;
        let file_end = file_offset
            .checked_add(file_size)
            .context("ELF segment overflow")?;
        ensure!(
            file_end <= bytes.len(),
            "{label} ELF LOAD {index} data is truncated"
        );
        if file_size < 8 {
            continue;
        }
        for offset in (file_offset..=file_end - 8).step_by(4) {
            let msp = u32_at(&bytes, offset)?;
            let reset = u32_at(&bytes, offset + 4)?;
            let valid_msp = memory.ram_regions.iter().any(|region| {
                let msp = msp as u64;
                msp >= region.base && msp <= region.base.saturating_add(region.size)
            });
            if valid_msp && reset == entry {
                let vector_table = physical_address
                    .checked_add((offset - file_offset) as u32)
                    .context("vector table address overflow")?;
                return Ok((msp, reset, vector_table));
            }
        }
    }
    anyhow::bail!("{label} ELF has no vector table whose reset handler matches entry 0x{entry:08x}")
}

pub fn flash_image(path: &Path, memory: &MemoryLayout, label: &str) -> Result<Vec<u8>> {
    let bytes =
        fs::read(path).with_context(|| format!("reading {label} ELF {}", path.display()))?;
    ensure!(
        bytes.len() >= ELF32_HEADER_SIZE && &bytes[0..6] == b"\x7fELF\x01\x01",
        "{label} must be a 32-bit little-endian ELF"
    );
    let phoff = u32_at(&bytes, 28)? as usize;
    let phentsize = u16_at(&bytes, 42)? as usize;
    let phnum = u16_at(&bytes, 44)? as usize;
    ensure!(
        phentsize >= ELF32_PROGRAM_HEADER_SIZE,
        "{label} ELF program header is too small"
    );
    ensure!(phnum > 0, "{label} ELF has no program headers");
    let mut loads = Vec::new();
    let mut image_len = 0usize;
    for index in 0..phnum {
        let header = program_header_offset(phoff, phentsize, index, bytes.len(), label)?;
        if u32_at(&bytes, header)? != PT_LOAD {
            continue;
        }
        let file_offset = u32_at(&bytes, header + 4)? as usize;
        let physical_address = u32_at(&bytes, header + 12)? as u64;
        let file_size = u32_at(&bytes, header + 16)? as usize;
        if file_size == 0 || !fits_flash(physical_address, file_size as u64, memory) {
            continue;
        }
        let source_end = file_offset
            .checked_add(file_size)
            .context("ELF segment overflow")?;
        let source = bytes
            .get(file_offset..source_end)
            .with_context(|| format!("{label} ELF LOAD {index} data is truncated"))?;
        let destination = usize::try_from(physical_address - memory.flash_base)
            .context("ELF flash image offset exceeds host limits")?;
        image_len = image_len.max(
            destination
                .checked_add(file_size)
                .context("ELF flash image overflow")?,
        );
        loads.push((destination, source));
    }
    ensure!(
        !loads.is_empty(),
        "{label} ELF has no initialized flash LOAD segments"
    );
    let mut image = vec![0xff; image_len];
    for (destination, source) in loads {
        image[destination..destination + source.len()].copy_from_slice(source);
    }
    Ok(image)
}

fn program_header_offset(
    phoff: usize,
    phentsize: usize,
    index: usize,
    file_len: usize,
    label: &str,
) -> Result<usize> {
    let header = phoff
        .checked_add(
            index
                .checked_mul(phentsize)
                .context("ELF program header overflow")?,
        )
        .context("ELF program header overflow")?;
    ensure!(
        header
            .checked_add(ELF32_PROGRAM_HEADER_SIZE)
            .is_some_and(|end| end <= file_len),
        "{label} ELF program header is truncated"
    );
    Ok(header)
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
    let end = offset.checked_add(2).context("ELF field offset overflow")?;
    let value = bytes.get(offset..end).context("truncated ELF field")?;
    Ok(u16::from_le_bytes([value[0], value[1]]))
}

fn u32_at(bytes: &[u8], offset: usize) -> Result<u32> {
    let end = offset.checked_add(4).context("ELF field offset overflow")?;
    let value = bytes.get(offset..end).context("truncated ELF field")?;
    Ok(u32::from_le_bytes([value[0], value[1], value[2], value[3]]))
}

#[cfg(test)]
mod tests {
    use super::{flash_image, reset_vector, validate_elf};
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
            persistent_data_base: None,
            persistent_data_size: None,
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

    #[test]
    fn finds_a_relocated_vector_table_inside_an_elf_segment() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("vectors.elf");
        let mut data = elf(0x0800_4000, 0x0800_4000, 0x300, 0x300);
        data[24..28].copy_from_slice(&0x0800_5101_u32.to_le_bytes());
        data[0x200..0x204].copy_from_slice(&0x2001_c000_u32.to_le_bytes());
        data[0x204..0x208].copy_from_slice(&0x0800_5101_u32.to_le_bytes());
        write(&path, &data);
        assert_eq!(
            reset_vector(&path, &memory(), "firmware").unwrap(),
            (0x2001_c000, 0x0800_5101, 0x0800_41ac)
        );
    }

    #[test]
    fn builds_erased_sparse_flash_image_from_physical_load_addresses() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("flash.elf");
        let mut data = elf(0x2000_0000, 0x0800_5000, 4, 8);
        data[84..88].copy_from_slice(&[1, 2, 3, 4]);
        write(&path, &data);
        let image = flash_image(&path, &memory(), "firmware").unwrap();
        assert_eq!(&image[0x5000..0x5004], &[1, 2, 3, 4]);
        assert!(image[..0x5000].iter().all(|byte| *byte == 0xff));
    }
}
