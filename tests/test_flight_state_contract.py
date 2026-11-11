import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts/flight_state_contract.py'
SPEC = importlib.util.spec_from_file_location('flight_state_contract', SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)

GS_TYPES = '''
pub enum NetworkFlightState {
    Startup = 0, Idle = 1, Fill = 2, Armed = 6, Launch = 7, Landed = 13, Aborted = 15, InFlight = 16,
}
'''
GS_NV = '''
fn flight_state_packet(state: NetworkFlightState, at: u64) -> Result<Packet> {
    packet_bytes_at(FLIGHT_STATE_TYPE, "FLIGHT_STATE", Arc::from([state as u8]), at)
}
#[cfg(test)]
mod tests {
    fn raw() { packet_bytes_at(FLIGHT_STATE_TYPE, "FLIGHT_STATE", Arc::from([3u8]), 1); }
}
'''
FC_STRUCTS = '''
typedef enum global_state : uint8_t {
  G_Startup, G_Idle, G_Fill, G_RETIRED_FillTest, G_RETIRED_NitrogenFill, G_RETIRED_NitrousFill,
  G_Armed, G_Launch, G_Ascent, G_Coast, G_Apogee, G_Descent, G_Reefing, G_Landed, G_Recovery,
  G_Aborted, G_InFlight,
  Global_States
} gnd_state;
'''
FC_API = '''
static inline gnd_state global_state_of(state local)
{
  switch (local) {
    case Startup:   return G_Startup;
    case Ascent:    return G_InFlight;
    case Recovery:  return G_Landed;
    default:        return G_Aborted;
  }
}
'''
FC_SRC = '''
static void publish(void) { log_flight_state(to_global_state(sm.flight)); }
static SedsResult implicit_postinit(const uint8_t *data, size_t len)
{ if (data == NULL || *data > G_InFlight) return SEDS_HANDLER_ERROR; return SEDS_OK; }
'''
BOARD_ENUM = '''
typedef enum {
    {P}STARTUP = 0, {P}IDLE = 1, {P}FILL = 2, {P}RETIRED_FILL_TEST = 3, {P}ARMED = 6,
    {P}LAUNCH = 7, {P}LANDED = 13, {P}ABORTED = 15, {P}IN_FLIGHT = 16,
} state_t;
'''
SCHEMA = {'types': [{'name': 'FLIGHT_STATE', 'element': {'kind': 'Static', 'data_type': 'UInt8', 'count': 1},
                     'reliable': 'Ordered', 'priority': 16}]}


def write(root, rel, text):
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def tree(root, **changes):
    files = {
        'GroundStation26/backend/src/types.rs': GS_TYPES,
        'GroundStation26/backend/src/network_variables.rs': GS_NV,
        'GroundStation26/backend/src/flight_sim.rs':
            'fn emit(&self) { let v = self.flight_state.network() as u8;\n Packet::new(data_type("FLIGHT_STATE"), v); }\n',
        'FlightComputer26/Core/Inc/fcstructs.h': FC_STRUCTS,
        'FlightComputer26/Core/Inc/fcapi.h': FC_API,
        'FlightComputer26/Core/Src/distribution.c': FC_SRC,
        'ActuatorBoard26/Core/Inc/thread_comm.h': BOARD_ENUM.replace('{P}', 'ACTUATOR_FLIGHT_STATE_'),
        'ValveBoard26/Core/Inc/thread_comm.h': BOARD_ENUM.replace('{P}', 'VALVE_FLIGHT_STATE_'),
        'ValveBoard26/Core/Src/main_thread.c': 'void f(void){ telemetry_publish_flight_state_at(VALVE_FLIGHT_STATE_LAUNCH, 1); }\n',
    }
    for repo in MODULE.BOARD_REPOS:
        files[f'{repo}/config/sedsnet.json'] = json.dumps(SCHEMA)
        files[f'{repo}/Core/Src/flight_state_cache.c'] = '#define FLIGHT_STATE_MAX_VALUE 16U\n'
    files.update(changes)
    for rel, text in files.items():
        if text is not None:
            write(root, rel, text)
    return MODULE.run(root)


class FlightStateContractTests(unittest.TestCase):
    def check(self, **changes):
        with tempfile.TemporaryDirectory() as directory:
            return tree(Path(directory), **changes).violations

    def test_a_tree_that_keeps_set_a_passes(self):
        self.assertEqual(self.check(), [])

    def test_gs_without_a_network_enum_fails(self):
        violations = self.check(**{'GroundStation26/backend/src/types.rs': 'pub enum FlightState { Startup = 0 }'})
        self.assertTrue(any('no NetworkFlightState' in v for v in violations), violations)

    def test_gs_network_enum_with_a_fill_sub_phase_fails(self):
        violations = self.check(**{'GroundStation26/backend/src/types.rs':
                                   GS_TYPES.replace('InFlight = 16', 'InFlight = 16, NitrousFill = 5')})
        self.assertTrue(any('!= set (a)' in v for v in violations), violations)

    def test_gs_raw_u8_flight_state_write_fails_but_test_code_is_ignored(self):
        raw = GS_NV + 'pub(crate) fn idle(at: u64) -> Result<Packet> {\n    packet_bytes_at(FLIGHT_STATE_TYPE, "FLIGHT_STATE", Arc::from([1u8]), at)\n}\n'
        violations = self.check(**{'GroundStation26/backend/src/network_variables.rs': raw})
        self.assertEqual(len(violations), 1, violations)
        self.assertIn('network_variables.rs:10', violations[0])

    def test_gs_sim_fc_publishing_its_detailed_phase_fails(self):
        violations = self.check(**{'GroundStation26/backend/src/flight_sim.rs':
            'fn emit(&self) { let v = self.flight_state as u8;\n Packet::new(data_type("FLIGHT_STATE"), v); }\n'})
        self.assertTrue(any('self.flight_state as u8' in v for v in violations), violations)

    def test_fc_publishing_ascent_or_recovery_fails(self):
        violations = self.check(**{'FlightComputer26/Core/Inc/fcapi.h':
                                   FC_API.replace('return G_InFlight', 'return G_Ascent').replace('return G_Landed', 'return G_Recovery')})
        self.assertTrue(any('G_Ascent=8' in v for v in violations), violations)
        self.assertTrue(any('G_Recovery=14' in v for v in violations), violations)

    def test_fc_without_in_flight_or_refusing_it_fails(self):
        violations = self.check(**{
            'FlightComputer26/Core/Inc/fcstructs.h': FC_STRUCTS.replace('G_Aborted, G_InFlight,', 'G_Aborted,'),
            'FlightComputer26/Core/Src/distribution.c': FC_SRC.replace('G_InFlight', 'G_Aborted')})
        self.assertTrue(any('no InFlight' in v for v in violations), violations)
        self.assertTrue(any('distribution.c' in v and 'refuses' in v for v in violations), violations)

    def test_fc_publication_bypassing_the_mapping_fails(self):
        violations = self.check(**{'FlightComputer26/Core/Src/distribution.c':
                                   FC_SRC.replace('to_global_state(sm.flight)', '&sm.global_state')})
        self.assertTrue(any('bypasses global_state_of' in v for v in violations), violations)

    def test_board_enum_with_an_unmarked_retired_value_fails(self):
        enum = BOARD_ENUM.replace('{P}', 'VALVE_FLIGHT_STATE_').replace('RETIRED_FILL_TEST', 'FILL_TEST')
        violations = self.check(**{'ValveBoard26/Core/Inc/thread_comm.h': enum})
        self.assertTrue(any('VALVE_FLIGHT_STATE_FILL_TEST=3' in v for v in violations), violations)

    def test_board_publishing_a_retired_value_fails(self):
        violations = self.check(**{'ValveBoard26/Core/Src/main_thread.c':
            'void f(void){ telemetry_publish_flight_state(VALVE_FLIGHT_STATE_RETIRED_FILL_TEST); }\n'})
        self.assertTrue(any('main_thread.c' in v and '=3' in v for v in violations), violations)

    def test_cache_refusing_in_flight_fails(self):
        violations = self.check(**{'RFBoard26/Core/Src/flight_state_cache.c': '#define FLIGHT_STATE_MAX_VALUE 15U\n'})
        self.assertEqual(violations, ['RFBoard26/Core/Src/flight_state_cache.c: FLIGHT_STATE_MAX_VALUE 15 refuses InFlight (16)'])

    def test_schema_listing_a_retired_value_or_disagreeing_fails(self):
        listed = json.loads(json.dumps(SCHEMA))
        listed['types'][0]['values'] = [0, 1, 2, 5]
        violations = self.check(**{'DAQ-Board/config/sedsnet.json': json.dumps(listed)})
        self.assertTrue(any('outside set (a): [5]' in v for v in violations), violations)
        widened = json.loads(json.dumps(SCHEMA))
        widened['types'][0]['element']['data_type'] = 'UInt16'
        violations = self.check(**{'GroundStation26/backend/tests/board-schemas/RFBoard26.json': json.dumps(widened)})
        self.assertTrue(any('not one UInt8' in v for v in violations), violations)
        self.assertTrue(any('differ between repos' in v for v in violations), violations)

    @unittest.skipUnless(os.environ.get('SEDS_CONTRACT_ROOT'), 'set SEDS_CONTRACT_ROOT to the directory holding every repo')
    def test_live_repositories_keep_set_a(self):
        report = MODULE.run(Path(os.environ['SEDS_CONTRACT_ROOT']))
        self.assertEqual(report.violations, [])


if __name__ == '__main__':
    unittest.main()
