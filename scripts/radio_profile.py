"""Explicit finite-radio scenarios; no host I2C hop or modem hardware writes."""
from copy import deepcopy
import json

REQUIRED = {'air_bits_per_second', 'uart_baud', 'tx_fifo_bytes', 'rx_fifo_bytes',
            'max_burst_bytes', 'packet_idle_us', 'overhead_bits', 'turn_us'}


def validate_profile(profile):
    if not isinstance(profile, dict) or set(profile) != REQUIRED:
        raise ValueError('radio profile must explicitly specify every capacity/packetization assumption')
    if any(type(value) is not int for value in profile.values()):
        raise ValueError('radio parameters must be integers')
    if not 1 <= profile['air_bits_per_second'] <= 10_000_000 or not 1 <= profile['uart_baud'] <= 5_000_000:
        raise ValueError('invalid radio rate')
    burst = profile['max_burst_bytes']
    if not 1 <= burst <= 65_536 or not all(burst <= profile[key] <= 1_048_576 for key in ('tx_fifo_bytes', 'rx_fifo_bytes')):
        raise ValueError('invalid bounded radio FIFO/burst budget')
    if not 1 <= profile['packet_idle_us'] <= 1_000_000 or not 0 <= profile['turn_us'] <= 1_000_000 or not 0 <= profile['overhead_bits'] <= 65_536:
        raise ValueError('invalid radio packetization/turn overhead')
    return deepcopy(profile)


def configure_host_heartbeat(topology, period_ms):
    """Explicit host traffic stress setting; never change firmware sensor rates."""
    if type(period_ms) is not int or not 100 <= period_ms <= 60_000:
        raise ValueError('host heartbeat period must be100..60000 milliseconds')
    for host in topology['host_nodes']:
        host.setdefault('env', {})['GS_HEARTBEAT_PERIOD_MS'] = str(period_ms)


def configure_radio_profile(directory, topology, profile, with_rrls=False):
    """Keep the existing command/source/SD gates; change only the radio medium."""
    profile = validate_profile(profile)
    links = [link for link in topology['links'] if link['name'] == 'rocket_radio']
    stations = [host for host in topology['host_nodes'] if host['name'] == 'groundstation']
    if len(links) != 1 or len(stations) != 1 or links[0]['kind'] != 'radio':
        raise ValueError('expected exactly one primary station and rocket radio link')
    link, station = links[0], stations[0]
    link['radio'] = profile
    station['env'].update(GS_VIDEO_MANAGED='0', GS_VIDEO_DISCOVERY='0')
    # Match the linked board RTC fixtures. Both hosted stations and every
    # restarted process use one epoch; slow wall execution must not retime UTC.
    station['env'].setdefault('GS_SIM_NETWORK_EPOCH_MS', '1767225600000')
    if not with_rrls:
        return
    if any(host['name'] == 'rrls' for host in topology['host_nodes']):
        raise ValueError('RRLS host already exists')
    station['env']['GS_RRLS_ROLE'] = 'primary'
    # Each station gets a distinct UART PTY on the same medium. RRLS has no
    # fill-side cable and no simulator command producer. Its live Mesh receives
    # AV data and can relay the primary's envelopes when direct radio is lost.
    (directory / 'comms-rrls.json').write_text(json.dumps({
        'version': 1, 'av_bay': {'interface': 'serial', 'port': '/dev/simulator-radio',
                               'baud_rate': profile['uart_baud'], 'protocol': 'raw_uart'}}, indent=2))
    rr = {'name': 'rrls', 'binary': station['binary'],
          'env': {'GS_RRLS_ROLE': 'rrls', 'GS_COMMS_LINK_CONFIG': '/simulation/comms-rrls.json',
                  'GS_SIMULATED_SERIAL_PTY': '1', 'GS_BIND_ADDRESS': '127.0.0.1:0',
                  'GS_HTTPS_BIND_ADDRESS': 'off', 'GS_VIDEO_MANAGED': '0', 'GS_VIDEO_DISCOVERY': '0',
                  'GS_DEBUG_PRINTS': '0', 'RUST_LOG': 'info'},
          'serial_links': [{'link': 'rocket_radio', 'env': 'GS_AV_BAY_SERIAL_PORT'}]}
    # Hosted recordings/session databases are relative to cwd. Sharing the
    # primary's cwd makes separate stations share users.db/WAL state and is not
    # a valid two-host simulation. /tmp exists in the qualification image; all
    # other mutable RRLS configuration paths are explicit and private as well.
    if station.get('cwd') == '/tmp':
        raise ValueError('primary working directory must differ from the isolated RRLS /tmp directory')
    rr['cwd'] = '/tmp'
    for key, filename in {
        'GS_USERS_FILE': 'users.json',
        'GS_FILL_TARGETS_CONFIG': 'fill-targets.json',
        'GS_FLIGHT_SETUP_CONFIG': 'flight-setup.json',
        'GS_GSE_CONFIG': 'gse.json',
        'GS_GSE_QUALIFICATION': 'gse-qualification.json',
        'GS_AUTO_ZERO_CONFIG': 'auto-zero.json',
        'GS_CALIBRATION_CONFIG_PATH': 'calibration.json',
        'GS_LOADCELL_CALIBRATION_PATH': 'loadcells.json',
        'GS_STATION_POSITIONS_PATH': 'station-positions.json',
        'GS_CHECKLISTS_FILE': 'checklists.json',
        'GS_MISSION_SETTINGS_PATH': 'mission-settings.json',
    }.items():
        rr['env'][key] = '/tmp/simulator-rrls-' + filename
    if 'args' in station:
        rr['args'] = deepcopy(station['args'])
    for key in ('GS_LAYOUT_PATH', 'GS_SIM_ROUTER_TIME_DIVISOR', 'GS_SIM_NETWORK_EPOCH_MS'):
        if key in station['env']:
            rr['env'][key] = station['env'][key]
    topology['host_nodes'].append(rr)
    link['endpoints'].append({'node': 'rrls', 'peripheral': 'radio'})
