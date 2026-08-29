use serde::Serialize;

#[derive(Clone, Copy, Debug, Default, Serialize)]
pub struct PoolStats {
    pub capacity: usize,
    pub high_water: usize,
    pub allocations: usize,
    pub allocation_failures: usize,
    pub bytes_in_use: usize,
}

#[derive(Debug)]
pub struct FixedPool {
    stats: PoolStats,
}

impl FixedPool {
    pub fn new(capacity: usize) -> Self {
        Self {
            stats: PoolStats {
                capacity,
                ..Default::default()
            },
        }
    }
    pub fn allocate(&mut self, bytes: usize) -> bool {
        if bytes > self.stats.capacity.saturating_sub(self.stats.bytes_in_use) {
            self.stats.allocation_failures += 1;
            return false;
        }
        self.stats.bytes_in_use += bytes;
        self.stats.allocations += 1;
        self.stats.high_water = self.stats.high_water.max(self.stats.bytes_in_use);
        true
    }
    pub fn release(&mut self, bytes: usize) -> bool {
        if bytes > self.stats.bytes_in_use {
            return false;
        }
        self.stats.bytes_in_use -= bytes;
        true
    }
    pub fn stats(&self) -> PoolStats {
        self.stats
    }
}
