"""Unit and calibration checks for the empirical RFD900x ASYNC mesh model."""
import importlib.util
import json
from pathlib import Path
import statistics
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('rfd_async_mesh', ROOT / 'scripts/rfd_async_mesh.py')
mesh = importlib.util.module_from_spec(spec)
sys.modules['rfd_async_mesh'] = mesh
spec.loader.exec_module(mesh)
EXAMPLES = ROOT / 'examples/radio/rfd-async-mesh'


def quiet(**overrides):
    """Deterministic MAC: no random backoff, so timing is exact."""
    base = {'backoff_slots': 0, 'ber': 0.0, 'rrls_role': True}
    base.update(overrides)
    return mesh.make_config(base)


def frame(sim, src, size, kind='misc'):
    return mesh.Frame(kind, src, size, sim.now)


class ReceiveLog:
    def __init__(self, sim):
        self.events = []
        sim.on_frame = lambda node, f, via: self.events.append((sim.now, node, f, via))


def run_until(sim, t):
    sim.duration = t
    while sim.events and sim.events[0][0] <= t:
        when, _, fn, args = mesh.heapq.heappop(sim.events)
        sim.now = when
        fn(*args)
    sim.now = t


class AssumptionLedger(unittest.TestCase):
    def test_every_parameter_is_labelled_and_sourced(self):
        for name, param in mesh.PARAMS.items():
            self.assertIn(param.kind, (mesh.MEASURED, mesh.DOCUMENTED, mesh.INFERRED), name)
            self.assertTrue(param.source.strip(), name)

    def test_async_register_semantics_are_not_sik(self):
        # Async S4/S5 are retry counts; SiK TXPOWER/ECC must not leak in.
        self.assertIn('S4', mesh.PARAMS['max_retries'].source)
        self.assertIn('broadcast', mesh.PARAMS['global_retries'].source)
        self.assertNotIn('ecc', mesh.PARAMS)
        self.assertEqual(mesh.PARAMS['fec_factor'].value, 1.0)
        self.assertEqual(mesh.PARAMS['global_retries'].kind, mesh.MEASURED)

    def test_config_rejects_unknown_and_out_of_range(self):
        with self.assertRaises(ValueError):
            mesh.make_config({'txpower': 20})
        with self.assertRaises(ValueError):
            mesh.make_config({'global_retries': 0})
        with self.assertRaises(ValueError):
            mesh.make_config({'retry_mode': 'magic'})
        with self.assertRaises(ValueError):
            mesh.make_config({'stall_duty_rf': 1.0})

    def test_groundstation_low_latency_profile_maps_to_registers(self):
        ov = mesh.profile_overrides(EXAMPLES / 'gs-rfd-async-64k-low-latency.json')
        self.assertEqual((ov['max_retries'], ov['global_retries'], ov['serial_break_ms']), (2, 1, 10))
        self.assertEqual((ov['packet_gap_ms'], ov['rf_share_pct'], ov['air_kbps']), (15, 40, 64))
        mesh.make_config(ov)


class PhysicalLayer(unittest.TestCase):
    def test_single_packet_latency_is_uart_break_turnaround_air_uart(self):
        cfg = quiet()
        sim = mesh.MeshSim(cfg, 1, 1000, [], background=False)
        log = ReceiveLog(sim)
        sim.host['gs'].enqueue(frame(sim, 'gs', 100))
        run_until(sim, 1000)
        rf = [e for e in log.events if e[1] == 'rf']
        self.assertEqual(len(rf), 1)
        uart = 100 * 10 / 115200 * 1000
        air = (cfg['phy_overhead_bytes'] + 100) * 8 / 64
        expected = uart + cfg['serial_break_ms'] + cfg['turnaround_ms'] + air + uart
        self.assertAlmostEqual(rf[0][0], expected, places=6)

    def test_serial_break_splits_or_joins_packets(self):
        for gap, packets in ((20, 1), (80, 2)):
            cfg = quiet(aggregate_on_send=False)
            sim = mesh.MeshSim(cfg, 1, 1000, [], background=False)
            modem = sim.modem['gs']
            modem.serial_in([frame(sim, 'gs', 30)])
            run_until(sim, gap)
            modem.serial_in([frame(sim, 'gs', 30)])
            run_until(sim, 1000)
            self.assertEqual(sim.pid, packets, gap)

    def test_max_data_closes_packet_without_waiting_for_break(self):
        cfg = quiet(max_data=100)
        sim = mesh.MeshSim(cfg, 1, 1000, [], background=False)
        sim.modem['gs'].serial_in([frame(sim, 'gs', 60), frame(sim, 'gs', 60)])
        self.assertEqual(sim.pid, 1)

    def test_aggregation_merges_backlog_up_to_max_data(self):
        cfg = quiet(stall_duty_gs=0.0)
        sim = mesh.MeshSim(cfg, 1, 5000, [], background=False)
        modem = sim.modem['gs']
        modem.blocked_until = 1000.0
        for k in range(10):
            run_until(sim, k * 80)
            modem.serial_in([frame(sim, 'gs', 150)])
        run_until(sim, 5000)
        sizes = [size for _, _, size in sim.rx_bursts['rf']]
        self.assertEqual(sum(sizes), 1500)
        self.assertLessEqual(len(sizes), 2)


class Mac(unittest.TestCase):
    def collide(self, **overrides):
        cfg = quiet(carrier_sense=False, **overrides)
        sim = mesh.MeshSim(cfg, 3, 3000, [], background=False)
        sim.modem['gs'].serial_in([frame(sim, 'gs', 200)])
        sim.modem['rf'].serial_in([frame(sim, 'rf', 200)])
        run_until(sim, 3000)
        return sim

    def test_simultaneous_blind_starts_collide_and_resend(self):
        sim = self.collide(retry_mode='collision', backoff_slots=40)
        self.assertGreater(sim.stats['modem_retries']['gs'] + sim.stats['modem_retries']['rf'], 0)
        self.assertEqual(sim.stats['rx_frames']['rrls'], 2)

    def test_s5_of_one_disables_resend(self):
        sim = self.collide(retry_mode='collision', global_retries=1)
        self.assertEqual(sim.stats['modem_retries'], {'gs': 0, 'rf': 0, 'rrls': 0})
        self.assertEqual(sim.stats['rx_frames']['rrls'], 0)

    def test_carrier_sense_defers_instead_of_colliding(self):
        cfg = quiet(backoff_slots=0)
        sim = mesh.MeshSim(cfg, 3, 3000, [], background=False)
        sim.modem['gs'].serial_in([frame(sim, 'gs', 500)])
        run_until(sim, cfg['serial_break_ms'] + 10)
        sim.modem['rf'].serial_in([frame(sim, 'rf', 50)])
        run_until(sim, 3000)
        self.assertEqual(sim.stats['modem_retries']['rf'], 0)
        self.assertEqual(sim.stats['rx_frames']['gs'], 1)
        self.assertEqual(sim.stats['rx_frames']['rf'], 1)

    def test_unconditional_copies_use_airtime_but_deliver_once(self):
        cfg = quiet(retry_mode='unconditional', unconditional_copy_fraction=0.4, backoff_slots=8)
        sim = mesh.MeshSim(cfg, 3, 3000, [], background=False)
        sim.modem['gs'].serial_in([frame(sim, 'gs', 64)])
        run_until(sim, 3000)
        one = (cfg['phy_overhead_bytes'] + 64) * 8 / 64
        self.assertAlmostEqual(sim.stats['air_ms']['gs'], 4 * one, places=6)
        self.assertEqual(sim.stats['rx_frames']['rf'], 1)

    def test_half_duplex_receiver_misses_packets_while_transmitting(self):
        cfg = quiet(carrier_sense=False, retry_mode='none')
        sim = mesh.MeshSim(cfg, 3, 3000, [], background=False)
        sim.modem['rf'].serial_in([frame(sim, 'rf', 1000)])
        run_until(sim, cfg['serial_break_ms'] + 20)
        sim.modem['gs'].serial_in([frame(sim, 'gs', 20)])
        run_until(sim, 3000)
        # GS transmitted during RF's packet: neither GS nor RRLS can decode RF.
        self.assertEqual(sim.stats['rx_frames']['gs'], 0)


class SerialFlowControl(unittest.TestCase):
    def test_without_cts_overflow_corrupts_frames(self):
        cfg = quiet(serial_buffer_bytes=300, cts_gs=False)
        sim = mesh.MeshSim(cfg, 1, 5000, [], background=False)
        sim.modem['gs'].blocked_until = 4000.0
        for _ in range(6):
            sim.modem['gs'].serial_in([frame(sim, 'gs', 120)])
        run_until(sim, 5000)
        self.assertGreater(sim.modem['gs'].overflow_bytes, 0)
        self.assertGreater(sim.stats['crc_errors']['rf'], 0)

    def test_with_cts_host_waits_and_nothing_is_lost(self):
        cfg = quiet(serial_buffer_bytes=300, cts_rrls=True)
        sim = mesh.MeshSim(cfg, 1, 8000, [], background=False)
        sim.modem['rrls'].blocked_until = 3000.0
        for _ in range(6):
            sim.host['rrls'].enqueue(frame(sim, 'rrls', 120))
        run_until(sim, 8000)
        self.assertEqual(sim.modem['rrls'].overflow_bytes, 0)
        self.assertEqual(sim.stats['rx_frames']['gs'], 6)


class Scenarios(unittest.TestCase):
    def test_replay_is_deterministic_per_seed(self):
        cfg = mesh.make_config()
        a = mesh.underglow_replay(cfg, 4)
        b = mesh.underglow_replay(cfg, 4)
        self.assertEqual(a, b)

    def test_null_model_matches_nominal_simulator_not_hardware(self):
        # Working CSMA with collision-only resend gives sub-500 ms commands,
        # like the existing UART-hub scenes, and cannot produce RX gaps.
        s = mesh.summarize_replay(mesh.run_many('replay', mesh.make_config(), range(4)))
        self.assertLess(s['first_ack_ms_p10_p50_p90'][2], 500)
        self.assertEqual(s['zero_rx_seconds_mean'], 0)
        self.assertGreater(mesh.fit_score(s)[0], 8)

    def test_fitted_bases_reproduce_hardware_signatures(self):
        for name in ('fitted-tx-stall.json', 'fitted-unconditional-copies.json'):
            cfg = mesh.make_config(json.loads((EXAMPLES / name).read_text()))
            s = mesh.summarize_replay(mesh.run_many('replay', cfg, range(8)))
            p50 = s['first_ack_ms_p10_p50_p90'][1]
            self.assertTrue(1000 <= p50 <= 4000, (name, s))
            self.assertGreater(s['sends_mean'], 5, name)
            self.assertGreater(s['zero_rx_seconds_mean'], 5, name)
            self.assertGreater(s['longest_rx_gap_s_mean'], 2.0, name)
            self.assertGreater(s['barometer_delivery_ratio'], 0.6, name)
            self.assertLess(s['crc_per_frame'], 0.02, name)
            self.assertLess(mesh.fit_score(s)[0], 4.0, name)

    def test_long_rf_silence_triggers_relay_and_hysteresis_suppresses_it(self):
        def relay_fraction(loss_ms):
            cfg = mesh.make_config({'stall_duty_rf': 0.6, 'stall_mean_ms': 4000.0, 'relay_loss_ms': loss_ms})
            return statistics.fmean(mesh.command_stream(cfg, s, 60000.0)['relay_on_fraction'] for s in range(3))
        self.assertGreater(relay_fraction(3000), 0.05)
        self.assertLess(relay_fraction(15000), relay_fraction(3000))

    def test_rrls_off_removes_relay_and_health_airtime(self):
        on = mesh.command_stream(mesh.make_config(), 2, 40000.0)
        off = mesh.command_stream(mesh.make_config({'rrls_role': False}), 2, 40000.0)
        self.assertEqual(off['air_utilisation']['rrls'], 0.0)
        self.assertGreater(on['air_utilisation']['rrls'], 0.0)
        self.assertLess(off['gs_tx_Bps'], on['gs_tx_Bps'])

    def test_evaluation_ranks_candidates_and_honours_s5(self):
        base = json.loads((EXAMPLES / 'fitted-unconditional-copies.json').read_text())
        result = mesh.evaluate(base, {'baseline': {}, 'low_latency_profile': 'PROFILE'},
                               EXAMPLES / 'gs-rfd-async-64k-low-latency.json', seeds=2, jobs=1)
        self.assertEqual(mesh.rank(result)[0], 'low_latency_profile')
        self.assertLess(result['low_latency_profile']['uplink_copies_mean'],
                        result['baseline']['uplink_copies_mean'])


if __name__ == '__main__':
    unittest.main()
