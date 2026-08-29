use crate::layout::MemoryLayout;
use anyhow::{bail, ensure, Result};
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
    let mut cuts = vec![
        0,
        1.min(chunks),
        chunks / 2,
        chunks.saturating_sub(1),
        chunks,
    ];
    cuts.sort_unstable();
    cuts.dedup();
    let (old_hash, new_hash) = (digest(original), digest(updated_image));
    ensure!(
        old_hash != new_hash,
        "simulated next firmware must differ from the installed image"
    );
    for cut in &cuts {
        let received = (*cut * chunk_size).min(transfer.len());
        let boot_hash = digest(if received == transfer.len() {
            updated_image
        } else {
            original
        });
        ensure!(
            boot_hash == old_hash || boot_hash == new_hash,
            "power loss left no bootable image"
        );
    }
    Ok(UpdateReport {
        strategy,
        chunks,
        interruption_points_tested: cuts.len(),
        original_sha256: old_hash,
        updated_sha256: new_hash,
    })
}
fn digest(data: &[u8]) -> String {
    Sha256::digest(data)
        .iter()
        .map(|byte| format!("{byte:02x}"))
        .collect()
}
