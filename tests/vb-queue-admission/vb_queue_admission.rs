#![cfg(all(not(feature="std"),feature="discovery"))]
use sedsnet::config::{DataEndpoint,DataType,RuntimeMemoryConfig};
use sedsnet::router::{Router,RouterConfig};
use std::sync::{Arc,Mutex};
#[unsafe(no_mangle)] pub extern "C" fn telemetry_lock() {}
#[unsafe(no_mangle)] pub extern "C" fn telemetry_unlock() {}
#[test]
fn accepted_rare_voltage_survives_fast_same_sender_pressure_before_later_current() {
 let voltage=DataType::named("BATTERY_VOLTAGE");let current=DataType::named("BATTERY_CURRENT");let pressure=DataType::named("FUEL_TANK_PRESSURE");
 let sd=DataType::named("VALVE_PRESSURE_SD_V1");
 let mut tuning=sedsnet::config::runtime_tuning_config();
 tuning.reliable_max_pending=8;tuning.reliable_max_return_routes=8;tuning.reliable_max_end_to_end_pending=8;tuning.reliable_max_end_to_end_ack_cache=8;
 sedsnet::config::set_runtime_tuning_config(tuning).unwrap();
 let competing_sd=std::env::var_os("VB_REPRO_PROTECTED_SD").is_some();
 let service_before_current=std::env::var_os("VB_REPRO_SERVICE_BEFORE_CURRENT").is_some();
 let background=std::env::var("VB_REPRO_BACKGROUND").unwrap_or_default();
 let mut failures=Vec::new();
 for prefill in 0..12 {
  let captured=Arc::new(Mutex::new(Vec::new()));let sink=captured.clone();
  let memory=RuntimeMemoryConfig {max_queue_budget:8192,max_recent_rx_ids:16,starting_queue_size:std::env::var("VB_REPRO_STARTING_BYTES").ok().map(|v|v.parse().unwrap()).unwrap_or(512),queue_grow_step:1.0};
  let node=Router::new_with_clock(RouterConfig::new([]).with_sender("VB").with_memory_config(memory).unwrap(),Box::new(||0));
  let side=node.add_side_packed("can",move|wire| {if let Ok(packet)=sedsnet::wire_format::unpack_packet(wire){sink.lock().unwrap().push((packet.data_type(),packet.payload().to_vec()));} Ok(())});
  for(name,ep)in[("GS",DataEndpoint::named("GROUND_STATION")),("DAQ",DataEndpoint::named("SD_CARD"))] {
   node.rx_from_side(&sedsnet::discovery::build_discovery_announce(name,1,&if name=="DAQ"{vec![ep,DataEndpoint::named("VALVE_PRESSURE_LOG")]}else{vec![ep]}).unwrap(),side).unwrap();
  }
  node.process_all_queues_with_timeout(0).unwrap();captured.lock().unwrap().clear();
  for n in 0..prefill {node.log_queue(pressure,&[n as f32]).unwrap();}
  if background.contains("heartbeat") {node.log_queue(DataType::named("HEARTBEAT"),&[] as &[u8]).unwrap();}
  if background.contains("status") {node.log_queue(DataType::named("UMBILICAL_STATUS"),&[0u8;2]).unwrap();}
  if background.contains("discovery") {node.tx_queue(sedsnet::discovery::build_discovery_announce("VB",1,&[]).unwrap()).unwrap();}
  if competing_sd {node.log_queue(sd,&[0u8;44]).expect("protected raw SD publication accepted");}
  node.log_queue(voltage,&[12.25f32]).expect("rare report publication accepted");
  let after_voltage=node.queue_counters();
  for n in 0..32 {node.log_queue(pressure,&[n as f32]).unwrap();}
  if service_before_current {node.dispatch_tx_queue_with_timeout(0).unwrap();}
  node.log_queue(current,&[0.75f32]).unwrap();
  let before_drain=node.queue_counters();node.dispatch_tx_queue_with_timeout(0).unwrap();
  let capture=captured.lock().unwrap();let v=capture.iter().filter(|(ty,_)|*ty==voltage).count();let c=capture.iter().filter(|(ty,_)|*ty==current).count();let p=capture.iter().filter(|(ty,_)|*ty==pressure).count();let raw=capture.iter().filter(|(ty,_)|*ty==sd).count();
  println!("VB_QUEUE prefill={prefill} fair={} accepted_voltage=1 actual_voltage_callback={v} current={c} pressure={p} protected_sd={raw} service_before_current={service_before_current} after_voltage={after_voltage:?} before_drain={before_drain:?}",cfg!(feature="fair-admission"));
  if competing_sd {assert_eq!(raw,1,"protected SD must not be evicted");}
  if v!=1 {failures.push(prefill);}
  assert_eq!(c,1,"later current must demonstrate that route still works");
 }
 assert!(failures.is_empty(),"accepted rare voltage disappeared before CAN callback at prefill={failures:?}");
}
