import importlib.util,json,tempfile,unittest
from pathlib import Path
root=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('recovery',root/'scripts/test-route-recovery.py')
recovery=importlib.util.module_from_spec(spec);spec.loader.exec_module(recovery)
def topology():
 return dict(virtual_time_ms=120000,sample_count=12,host_nodes=[dict(name='groundstation',serial_links=[dict(link='fill_pico',env='GS_FILL_PICO_DISCOVERY_PORT',transport='pico_fi_discovery_to_uart')])],links=[dict(name='fill_pico',kind='pico_fi',baud_rate=1000000,endpoints=[dict(node='groundstation',peripheral='fill_box'),dict(node='gateway',peripheral='usart2')])])
class HardwareFillProfileTests(unittest.TestCase):
 def test_actual_firmware_build_directories(self):
  for name in ["RFBoard26","FlightComputer26"]:self.assertEqual(recovery.built_subdir(Path(name)),"Release")
  self.assertEqual(recovery.built_subdir(Path("gateway-board26")),"Release_Script")
 def test_uses_selected_p6_real_delivery_clock_and_requires_all_complete_windows(self):
  with tempfile.TemporaryDirectory() as tmp:
   t=topology()
   recovery.configure_hardware_fill_profile(Path(tmp),t)
   self.assertEqual(json.loads((Path(tmp)/'hardware-fill-calibration.json').read_text())['kg50_input'],'p6_differential')
   self.assertEqual(t['host_nodes'][0]['env']['GS_SIM_VALIDATE_FILL_DELIVERY'],'1')
   self.assertEqual(t['host_log_assertions'][0]['minimum_occurrences'],9)
   self.assertEqual(len(t['host_log_assertions']),2)
   self.assertEqual(t['host_log_assertions'][0]['contains'],'sustained fill delivery ')
   self.assertNotIn('can_ack_events',t)
 def test_cannot_claim_baseline_with_short_execution_or_injected_faults(self):
  with tempfile.TemporaryDirectory() as tmp:
   for t in [dict(virtual_time_ms=16000),dict(virtual_time_ms=120000,reboots=[{}]),dict(virtual_time_ms=120000,can_ack_events=[{}])]:
    with self.assertRaises(ValueError):recovery.configure_hardware_fill_profile(Path(tmp),t)
 def test_gateway_records_each_sender_and_cpu_phase_without_relaxing_existing_loss_limits(self):
  layout={'execution':{'memory_probes':[dict(name='can_rx_ring_drops',symbol='g_rx_dropped_frames',maximum=0)]}}
  recovery.configure_hardware_fill_probes(layout,'gateway-board26')
  p={x['name']:x for x in layout['execution']['memory_probes']}
  self.assertEqual(p['can_rx_ring_drops']['maximum'],0)
  self.assertEqual(p['daq_can_drops']['byte_offset'],28)
  self.assertEqual(p['valve_can_drops']['byte_offset'],24)
  self.assertEqual(p['service_network_queues_ms']['byte_offset'],8)
  self.assertIn('can_admission_fail',p)
  self.assertEqual(p['network_memory_schema_bytes']['byte_offset'],32)
  self.assertEqual(p['network_memory_version']['minimum'],1)

 def test_rejects_legacy_or_bypassed_physical_transport(self):
  with tempfile.TemporaryDirectory() as tmp:
   for mode in ['pico_fi_i2c_to_uart','pico_fi_tcp_to_uart','serial']:
    t=topology();t['host_nodes'][0]['serial_links'][0]['transport']=mode
    with self.assertRaises(ValueError):recovery.configure_hardware_fill_profile(Path(tmp),t)
   t=topology();t['links'][0]['baud_rate']=115200
   with self.assertRaises(ValueError):recovery.configure_hardware_fill_profile(Path(tmp),t)
   t=topology();t['links'][0]['endpoints'].append(dict(node='gs_pico',peripheral='i2c'))
   with self.assertRaises(ValueError):recovery.configure_hardware_fill_profile(Path(tmp),t)

 def test_extended_fill_keeps_every_ten_second_window_and_no_faults(self):
  with tempfile.TemporaryDirectory() as tmp:
   t=topology();t['virtual_time_ms']=600000;t['sample_count']=60
   recovery.configure_hardware_fill_profile(Path(tmp),t)
   self.assertEqual(t['host_log_assertions'][0]['minimum_occurrences'],57)
 def test_missing_source_aware_evidence_cannot_pass(self):
  with tempfile.TemporaryDirectory() as tmp:
   with self.assertRaises(ValueError):recovery.validate_fill_evidence(Path(tmp),120000)

 def test_rejects_runner_extending_probe_count_without_extending_duration(self):
  with tempfile.TemporaryDirectory() as tmp:
   t=topology();t.update(virtual_time_ms=80000,sample_count=9)
   with self.assertRaisesRegex(ValueError,'ten seconds'):recovery.configure_hardware_fill_profile(Path(tmp),t)

 def test_daq_preserves_first_publication_refusal_in_existing_diagnostic_buffer(self):
  layout={};recovery.configure_hardware_fill_probes(layout,'DAQ-Board')
  probes={p['name']:p for p in layout['execution']['memory_probes']}
  self.assertEqual(probes['publication_failure_result']['byte_offset'],64)
  self.assertEqual(probes['publication_failure_queue_budget']['byte_offset'],4)
  self.assertEqual(probes['publication_failure_admission_rejections']['byte_offset'],80)
  self.assertNotIn('minimum',probes['publication_failure_snapshot_words'])

 def test_existing_daq_diagnostics_are_not_sampled_twice(self):
  original=dict(name='publication_failure_snapshot_words',symbol='g_telemetry_alloc_failure_stack_words')
  layout={'execution':{'memory_probes':[original,dict(name='analog_publish_failures',symbol='g_sim_analog_publish_failures',maximum=0)]}}
  recovery.configure_hardware_fill_probes(layout,'DAQ-Board')
  once=json.loads(json.dumps(layout))
  recovery.configure_hardware_fill_probes(layout,'DAQ-Board')
  self.assertEqual(layout,once)
  names=[p['name'] for p in layout['execution']['memory_probes']]
  self.assertEqual(len(names),len(set(names)))
  self.assertIn(original,layout['execution']['memory_probes'])
  self.assertEqual(layout['execution']['memory_probes'][1]['maximum'],0)

 def test_radio_control_probe_spans_entire_soak_and_rejects_missing_late_receipts(self):
  with tempfile.TemporaryDirectory() as tmp:
   for duration in [120000,600000]:
    t=topology();t.update(virtual_time_ms=duration,sample_count=duration//10000)
    recovery.configure_hardware_fill_profile(Path(tmp),t)
    env=t['host_nodes'][0]['env']
    samples=[int(n) for n in env['GS_SIM_SOAK_COMMAND_SAMPLES'].split(',')]
    self.assertEqual(samples,list(range(1,t['sample_count'])))
    self.assertEqual(env['GS_SIM_VALIDATE_SOAK_AV_BAY'],'1')
    self.assertEqual(env['GS_SIM_MANAGED_VARIABLE_MAX_LATENCY_MS'],'500')
    gate=t['host_log_assertions'][1]
    self.assertEqual(gate['minimum_occurrences'],len(samples))
    self.assertEqual(gate['contains'],'full-bay RF soak control acknowledged:')

 def test_receipts_cannot_replace_observed_underglow_state_and_fc_execution(self):
  with tempfile.TemporaryDirectory() as tmp:
   t=topology();recovery.configure_hardware_fill_profile(Path(tmp),t)
   states=[a for a in t['assertions'] if a['probe']=='underglow_enabled']
   self.assertEqual(len(states),33)
   for board in ['rf','power','flight']:
    checks=[a for a in states if a['node']==board]
    self.assertEqual([a['sample'] for a in checks],list(range(1,12)))
    self.assertEqual([a['minimum'] for a in checks],[i%2 for i in range(11)])
    self.assertTrue(all(a['minimum']==a['maximum'] for a in checks))
   executed=[a for a in t['assertions'] if a['probe']=='network_flight_commands_processed']
   self.assertEqual(len(executed),11)
   self.assertTrue(all(a['minimum_gain']==1 and a['maximum_gain']==1 and a['to_sample']==a['from_sample']+1 for a in executed))
