use crate::core::{FixedPool, PoolStats};
use anyhow::{ensure, Result};
use serde::{Deserialize, Serialize};
use std::collections::VecDeque;

#[derive(Clone, Debug, Deserialize)]
pub struct TrafficConfig {
    #[serde(default = "default_iterations")]
    pub iterations: usize,
    #[serde(default = "default_payload")]
    pub max_payload: usize,
    #[serde(default)]
    pub queue_depth: usize,
    #[serde(default = "default_immediate")]
    pub immediate_dispatch: bool,
}
impl Default for TrafficConfig {
    fn default() -> Self {
        Self {
            iterations: default_iterations(),
            max_payload: default_payload(),
            queue_depth: 0,
            immediate_dispatch: true,
        }
    }
}
fn default_iterations() -> usize {
    10_000
}
fn default_payload() -> usize {
    256
}
fn default_immediate() -> bool {
    true
}

#[derive(Clone, Debug, Serialize)]
pub struct TrafficReport {
    pub scope: &'static str,
    pub firmware_path_exercised: bool,
    pub packets_attempted: usize,
    pub packets_dispatched: usize,
    pub pool: PoolStats,
}

pub fn run(config: &TrafficConfig, pool_size: usize, seed: u64) -> Result<TrafficReport> {
    ensure!(config.max_payload > 0, "max_payload must be positive");
    ensure!(
        config.immediate_dispatch || config.queue_depth > 0,
        "queued dispatch requires queue_depth"
    );
    let (mut state, mut pool, mut outstanding, mut dispatched) =
        (seed.max(1), FixedPool::new(pool_size), VecDeque::new(), 0);
    for _ in 0..config.iterations {
        state = state.wrapping_mul(6364136223846793005).wrapping_add(1);
        let bytes = 24 + ((state >> 32) as usize % config.max_payload);
        if pool.allocate(bytes) {
            if config.immediate_dispatch {
                ensure!(pool.release(bytes), "pool underflow");
                dispatched += 1;
            } else {
                outstanding.push_back(bytes);
                if outstanding.len() >= config.queue_depth {
                    let bytes = outstanding.pop_front().unwrap();
                    ensure!(pool.release(bytes), "pool underflow");
                    dispatched += 1;
                }
            }
        } else if let Some(bytes) = outstanding.pop_front() {
            ensure!(pool.release(bytes), "pool underflow");
            dispatched += 1;
        }
    }
    while let Some(bytes) = outstanding.pop_front() {
        ensure!(pool.release(bytes), "pool underflow");
        dispatched += 1;
    }
    let stats = pool.stats();
    ensure!(
        stats.bytes_in_use == 0,
        "SEDSNet traffic leaked pool memory"
    );
    Ok(TrafficReport {
        scope: "behavioral_pool_model",
        firmware_path_exercised: false,
        packets_attempted: config.iterations,
        packets_dispatched: dispatched,
        pool: stats,
    })
}
