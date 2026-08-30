use crate::layout::MemoryLayout;
use anyhow::{bail, ensure, Context, Result};
use serde::Serialize;
use sha2::{Digest, Sha256};

#[derive(Clone, Copy, Debug, Eq, PartialEq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum UpdateStrategy {
    DualSlot,
    DeltaOnly,
    RecoveryTransport,
}

#[derive(Clone, Debug, Serialize)]
pub struct UpdateReport {
    pub strategy: UpdateStrategy,
    pub chunks: usize,
    pub interruption_points_tested: usize,
    pub original_sha256: String,
    pub updated_sha256: String,
    pub old_image_boot_points: usize,
    pub new_image_boot_points: usize,
    pub recovery_required_points: usize,
    pub all_flash_operation_boundaries_tested: bool,
    pub cpu_reboots_executed: bool,
    pub updated_image_from_artifact: bool,
}

pub struct Flash {
    bytes: Vec<u8>,
    erase_size: usize,
    write_alignment: usize,
}
impl Flash {
    pub fn new(size: usize, erase_size: usize, write_alignment: usize) -> Self {
        Self {
            bytes: vec![0xff; size],
            erase_size,
            write_alignment,
        }
    }
    pub fn erase(&mut self, offset: usize, length: usize) -> Result<()> {
        ensure!(
            offset & (self.erase_size - 1) == 0 && length & (self.erase_size - 1) == 0,
            "unaligned erase"
        );
        self.bytes
            .get_mut(offset..offset + length)
            .ok_or_else(|| anyhow::anyhow!("erase outside flash"))?
            .fill(0xff);
        Ok(())
    }
    pub fn program(&mut self, offset: usize, payload: &[u8]) -> Result<()> {
        ensure!(
            offset & (self.write_alignment - 1) == 0,
            "unaligned program"
        );
        let padded_len = payload.len().div_ceil(self.write_alignment) * self.write_alignment;
        ensure!(
            offset + padded_len <= self.bytes.len(),
            "program outside flash"
        );
        for index in 0..padded_len {
            let incoming = payload.get(index).copied().unwrap_or(0xff);
            let old = self.bytes[offset + index];
            if incoming | old != old {
                bail!("zero-to-one flash transition");
            }
            self.bytes[offset + index] &= incoming;
        }
        Ok(())
    }

    pub fn read(&self, offset: usize, length: usize) -> Result<&[u8]> {
        self.bytes
            .get(offset..offset + length)
            .ok_or_else(|| anyhow::anyhow!("read outside flash"))
    }
}

pub fn interruption_matrix(
    original: &[u8],
    updated_image: &[u8],
    transfer: &[u8],
    memory: &MemoryLayout,
    chunk_size: usize,
) -> Result<UpdateReport> {
    ensure!(
        !original.is_empty() && !updated_image.is_empty(),
        "update images cannot be empty"
    );
    ensure!(!transfer.is_empty(), "OTA transfer cannot be empty");
    ensure!(chunk_size > 0, "OTA chunk_size must be positive");
    let strategy = if memory.slot_b_size.unwrap_or(0) >= updated_image.len() as u64 {
        UpdateStrategy::DualSlot
    } else if memory.delta_size.unwrap_or(0) > 0 {
        UpdateStrategy::DeltaOnly
    } else {
        UpdateStrategy::RecoveryTransport
    };
    let chunks = transfer.len().div_ceil(chunk_size);
    let (old_hash, new_hash) = (digest(original), digest(updated_image));
    ensure!(
        old_hash != new_hash,
        "simulated next firmware must differ from the installed image"
    );
    let flash_size: usize = memory.flash_size.try_into()?;
    let erase_size: usize = memory.erase_size.try_into()?;
    let write_alignment: usize = memory.write_alignment.try_into()?;
    let slot_a: usize = memory
        .slot_a_base
        .saturating_sub(memory.flash_base)
        .try_into()?;
    ensure!(
        original.len() <= memory.slot_a_size as usize,
        "original exceeds slot A"
    );
    ensure!(
        updated_image.len() <= memory.slot_a_size as usize,
        "updated image exceeds slot A"
    );
    let mut flash = Flash::new(flash_size, erase_size, write_alignment);
    flash.program(slot_a, original)?;
    let mut old_points = 0;
    let mut new_points = 0;
    let mut recovery_points = 0;
    let mut operations = 0;
    let classify = |flash: &Flash, new_offset: Option<usize>| -> Result<(bool, bool)> {
        let old_valid = digest(flash.read(slot_a, original.len())?) == old_hash;
        let new_valid = if let Some(offset) = new_offset {
            digest(flash.read(offset, updated_image.len())?) == new_hash
        } else {
            digest(flash.read(slot_a, updated_image.len())?) == new_hash
        };
        Ok((old_valid, new_valid))
    };
    let record = |flash: &Flash,
                  new_offset: Option<usize>,
                  old_points: &mut usize,
                  new_points: &mut usize,
                  recovery_points: &mut usize|
     -> Result<()> {
        match classify(flash, new_offset)? {
            (_, true) => *new_points += 1,
            (true, false) => *old_points += 1,
            (false, false) => *recovery_points += 1,
        }
        Ok(())
    };

    let destination = match strategy {
        UpdateStrategy::DualSlot => Some(
            memory
                .slot_b_base
                .context("dual-slot update has no slot B base")?
                .saturating_sub(memory.flash_base)
                .try_into()?,
        ),
        _ => None,
    };
    record(
        &flash,
        destination,
        &mut old_points,
        &mut new_points,
        &mut recovery_points,
    )?;

    if strategy == UpdateStrategy::DeltaOnly {
        let staging = memory
            .delta_base
            .context("delta update has no staging base")?
            .saturating_sub(memory.flash_base) as usize;
        ensure!(
            transfer.len() <= memory.delta_size.unwrap_or(0) as usize,
            "OTA transfer exceeds delta staging area"
        );
        for (index, payload) in transfer.chunks(chunk_size).enumerate() {
            let offset = staging + index * chunk_size;
            let aligned_offset = offset / write_alignment * write_alignment;
            ensure!(
                aligned_offset == offset,
                "OTA chunk size is not write aligned"
            );
            flash.program(offset, payload)?;
            operations += 1;
            record(
                &flash,
                destination,
                &mut old_points,
                &mut new_points,
                &mut recovery_points,
            )?;
        }
    }

    let target = destination.unwrap_or(slot_a);
    let erase_length = updated_image.len().div_ceil(erase_size) * erase_size;
    for offset in (0..erase_length).step_by(erase_size) {
        flash.erase(target + offset, erase_size)?;
        operations += 1;
        record(
            &flash,
            destination,
            &mut old_points,
            &mut new_points,
            &mut recovery_points,
        )?;
    }
    for (index, payload) in updated_image.chunks(chunk_size).enumerate() {
        let offset = target + index * chunk_size;
        ensure!(
            offset & (write_alignment - 1) == 0,
            "OTA chunk size is not write aligned"
        );
        flash.program(offset, payload)?;
        operations += 1;
        record(
            &flash,
            destination,
            &mut old_points,
            &mut new_points,
            &mut recovery_points,
        )?;
    }
    ensure!(
        new_points > 0,
        "completed update did not produce the new image"
    );
    Ok(UpdateReport {
        strategy,
        chunks,
        interruption_points_tested: operations + 1,
        original_sha256: old_hash,
        updated_sha256: new_hash,
        old_image_boot_points: old_points,
        new_image_boot_points: new_points,
        recovery_required_points: recovery_points,
        all_flash_operation_boundaries_tested: true,
        cpu_reboots_executed: false,
        updated_image_from_artifact: false,
    })
}
fn digest(data: &[u8]) -> String {
    Sha256::digest(data)
        .iter()
        .map(|byte| format!("{byte:02x}"))
        .collect()
}
