#!/usr/bin/env python3
"""Native SITL engine for the fullnet scenarios (docs/native-sitl.md).

Runs an unchanged test-full-network.py controller (this checkout's, or --controller PATH for a branch's copy)
against host-compiled board firmware (native/build_board.py) and the real GroundStation backend as host processes
on one virtual clock, instead of Renode + Docker:

  python3 scripts/native_engine.py [native options] <test-full-network.py arguments>

Native options:
  --controller PATH      controller script (default scripts/test-full-network.py)
  --native-builds DIR    per-node builds (<node>/<node>.native); missing ones are built from the --cohort sources
  --native-pace X        virtual seconds per wall second (default 10); the station runs time-dilated by X
  --native-quantum-us N  lockstep quantum (default 1000)
  --gs-dir DIR           extracted station (groundstation_backend, backend/, libfaketimeMT.so.1); default: extracted
                         from the controller's --image into build/native/gs/<tag>
  --native-seed N        deterministic seed (ADC noise, boot order salt)
  --drift NODE:PPM       per-node clock drift (repeatable)

The controller's docker calls are served locally: the run lives in a private network namespace (unshare -rn), so
the station binds its usual ports and many runs can go in parallel without the fullnet queue gate.
"""
from __future__ import annotations
import argparse, importlib.util, json, os, re, shutil, signal, subprocess as _sp, sys, tempfile, time, types
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
NATIVE = ROOT / 'native'
REPO_NODE = {'gateway-board26': 'gateway', 'ActuatorBoard26': 'actuator', 'ValveBoard26': 'valve', 'DAQ-Board': 'daq',
             'FlightComputer26': 'flight', 'RFBoard26': 'rf', 'PowerBoard26': 'power'}
NODE_REPO = {v: k for k, v in REPO_NODE.items()}
FAKETIME_IN_IMAGE = '/usr/lib/x86_64-linux-gnu/faketime/libfaketimeMT.so.1'


def own_args(argv):
    p = argparse.ArgumentParser(add_help=False)
    p.add_argument('--controller', type=Path, default=HERE / 'test-full-network.py')
    p.add_argument('--native-builds', type=Path, default=ROOT / 'build/native/boards')
    p.add_argument('--native-pace', type=float, default=10.0)
    p.add_argument('--native-quantum-us', type=int, default=1000)
    p.add_argument('--native-max-skip-us', type=int, default=20000)
    p.add_argument('--gs-dir', type=Path)
    p.add_argument('--native-seed', type=int, default=1)
    p.add_argument('--drift', action='append', default=[])
    p.add_argument('--firmware-sim', type=Path)
    p.add_argument('--native-build-jobs', type=int, default=8)
    return p.parse_known_args(argv)


def controller_arg(rest, name, default=None):
    for i, x in enumerate(rest):
        if x == name and i + 1 < len(rest): return rest[i + 1]
        if x.startswith(name + '='): return x.split('=', 1)[1]
    return default


def extract_gs(image: str, dest: Path):
    """The image's station binary + backend config and its libfaketime, run natively on the host."""
    if (dest / 'groundstation_backend').exists():
        return dest
    tmp = Path(tempfile.mkdtemp(prefix='.gs-', dir=dest.parent if dest.parent.exists() else None))
    cid = _sp.check_output(['docker', 'create', image], text=True).strip()
    try:
        _sp.check_call(['docker', 'cp', f'{cid}:/usr/local/bin/groundstation_backend', str(tmp / 'groundstation_backend')])
        _sp.check_call(['docker', 'cp', f'{cid}:/opt/groundstation/backend', str(tmp / 'backend')])
        _sp.check_call(['docker', 'cp', '-L', f'{cid}:{FAKETIME_IN_IMAGE}', str(tmp / 'libfaketimeMT.so.1')])
        try:
            _sp.check_call(['docker', 'cp', f'{cid}:/usr/local/bin/firmware-sim', str(tmp / 'firmware-sim.image')])
        except _sp.CalledProcessError:
            pass
    finally:
        _sp.run(['docker', 'rm', cid], capture_output=True)
    (tmp / 'IMAGE').write_text(image + '\n' + _sp.check_output(['docker', 'image', 'inspect', image, '--format', '{{.Id}}'], text=True))
    tmp.rename(dest)
    return dest


GS_MANIFEST_DIR = b'/src/gs-head/backend'   # the station's compile-time CARGO_MANIFEST_DIR (Docker build tree)
GS_NATIVE_DIR = b'/tmp/seds-gs/backend'     # a private /tmp per run (mount namespace) holds it


def native_gs_binary(gsdir: Path):
    """The image's station binary with its built-in default tree (logs, configs) moved from the build container's
    /src/gs-head/backend to /tmp/seds-gs/backend, which each run's private /tmp maps to its own station tree."""
    out = gsdir / 'groundstation_backend.native'
    if not out.exists():
        data = (gsdir / 'groundstation_backend').read_bytes()
        # A host build records its own source tree (gsdir/MANIFEST_DIR); pad the private path with '/' to its length.
        src = (gsdir / 'MANIFEST_DIR').read_bytes().strip() if (gsdir / 'MANIFEST_DIR').exists() else GS_MANIFEST_DIR
        assert len(src) >= len(GS_NATIVE_DIR) and src in data, src
        dst = b'/tmp/seds-gs' + b'/' * (len(src) - len(GS_NATIVE_DIR) + 1) + b'backend'
        assert len(dst) == len(src)
        tmp = out.with_suffix('.tmp'); tmp.write_bytes(data.replace(src, dst)); tmp.chmod(0o755); tmp.rename(out)
    return out


def ensure_builds(builds: Path, cohort: Path, nodes, jobs):
    for node in nodes:
        exe = builds / node / f'{node}.native'
        if exe.exists():
            continue
        src = cohort / NODE_REPO[node]
        print(f'[native] building {node} from {src}', flush=True)
        _sp.check_call([sys.executable, str(NATIVE / 'build_board.py'), str(src), '--out', str(builds), '--jobs', str(jobs)])


def ensure_firmware_sim(explicit):
    if explicit:
        return explicit
    target = ROOT / 'build/native/target'
    exe = target / 'release/firmware-sim'
    srcs = [p for p in (ROOT / 'src').rglob('*.rs')] + [ROOT / 'Cargo.toml']
    if not exe.exists() or max(p.stat().st_mtime for p in srcs) > exe.stat().st_mtime:
        _sp.check_call(['cargo', 'build', '--release', '--quiet'], cwd=ROOT, env={**os.environ, 'CARGO_TARGET_DIR': str(target)})
    return exe


def reexec_in_netns():
    """A private network namespace: the station's fixed ports, discovery UDP and loopback stay per run."""
    if os.environ.get('SEDS_NATIVE_NETNS') == '1':
        _sp.run(['ip', 'link', 'set', 'lo', 'up'], check=False)
        return
    env = dict(os.environ, SEDS_NATIVE_NETNS='1')
    os.execvpe('unshare', ['unshare', '--user', '--map-root-user', '--net', '--mount', sys.executable, *sys.argv], env)


# ------------------------------------------------------------------------- docker stand-in for the controller
class LocalDocker:
    """Serves the controller's docker calls: 'run' is the native coordinator, 'inspect' reports loopback."""
    def __init__(self, real):
        self.real = real; self.procs = []
        for k in ('PIPE', 'STDOUT', 'DEVNULL', 'CalledProcessError', 'CompletedProcess', 'TimeoutExpired'):
            setattr(self, k, getattr(real, k))

    def _docker(self, cmd):
        return isinstance(cmd, (list, tuple)) and cmd and cmd[0] == 'docker'

    def run(self, cmd, *a, **kw):
        if self._docker(cmd):
            if cmd[1] == 'stop':
                for p in self.procs:
                    if p.poll() is None:
                        p.send_signal(signal.SIGTERM)
                for p in self.procs:
                    try: p.wait(timeout=30)
                    except self.real.TimeoutExpired: p.kill()
            return self.real.CompletedProcess(cmd, 0, b'' if not kw.get('text') else '', b'' if not kw.get('text') else '')
        return self.real.run(cmd, *a, **kw)

    def check_output(self, cmd, *a, **kw):
        if self._docker(cmd) and cmd[1] == 'inspect':
            net = NativeState.net
            out = json.dumps([{'NetworkSettings': {'Networks': {net: {'IPAddress': '127.0.0.1'}}}}])
            return out if kw.get('text') else out.encode()
        return self.real.check_output(cmd, *a, **kw)

    def Popen(self, cmd, *a, **kw):
        p = self.real.Popen(cmd, *a, **kw)
        if NativeState.native_cmd is not None and list(cmd) == list(NativeState.native_cmd):
            self.procs.append(p)
        return p

    def __getattr__(self, k):
        return getattr(self.real, k)


class NativeState:
    opts = None; net = None; native_cmd = None; scratch = None


def native_symbols(builds: Path, node: str):
    syms = {}
    for line in (builds / node / f'{node}.symbols').read_text().splitlines():
        parts = line.split()
        if len(parts) >= 3:
            syms[parts[-1]] = int(parts[0], 16)
    return syms


def container_map(out: Path, config: Path, diag: Path, gsroot: Path, gsdir: Path, rtmp: Path):
    return [('/simulation', str(config)), ('/diagnostics', str(diag)), ('/opt/groundstation/data', str(out / 'gs-data')),
            ('/opt/groundstation/backend/data', str(out / 'gs-backend-data')), ('/opt/groundstation', str(gsroot)),
            ('/usr/local/bin/groundstation_backend', str(gsdir / 'groundstation_backend')),
            (FAKETIME_IN_IMAGE, str(gsdir / 'libfaketimeMT.so.1')), ('/tmp', str(rtmp)), ('/dev/simulator-radio', '/dev/simulator-radio')]


def remap(value, mapping):
    if not isinstance(value, str):
        return value
    for src, dst in sorted(mapping, key=lambda m: -len(m[0])):
        if value == src or value.startswith(src + '/'):
            return dst + value[len(src):]
    return value


def devices_text(layout):
    lines = []
    for p in layout.get('peripherals', []):
        model = p.get('model'); bus = p.get('bus', '')
        if model == 'stm32_adc':
            vals = p.get('channel_samples') or [1 << (p.get('bits', 12) - 1)]
            lines.append(f"adc {bus} {p.get('bits', 12)} {p.get('noise_lsb', 0)} {','.join(map(str, vals))}")
        elif model == 'ltc2990':
            b, _, addr = bus.partition('@')
            vals = p.get('channel_samples') or []
            lines.append(f"ltc2990 {b} {addr} {','.join(map(str, vals[:4])) if vals else '-'} {p.get('failure_every', 0)}")
        elif model == 'mcp3564r':
            lines.append(f"mcp3564r {bus} {','.join(map(str, p.get('channel_samples') or [0]))}")
    for pin in layout.get('board', {}).get('pins', []):
        lines.append(f"pin {pin['pin']} {1 if pin.get('initial') == 'high' else 0}")
    return '\n'.join(lines) + '\n'


def install(FN, opts):
    F = FN.F
    builds = opts.native_builds.resolve()
    orig_build_config = FN.build_config

    def load_layout(board: Path):
        return json.loads((board / 'sim/board.json').read_text())

    def nm_symbols(board: Path, layout):
        return native_symbols(builds, REPO_NODE[Path(board).name])

    F.load_layout = load_layout
    F.nm_symbols = nm_symbols

    def build_config(a):
        out, diag, key, cmd, name, net, missing = orig_build_config(a)
        config = out / 'config'
        topo = json.loads((config / 'topology.json').read_text())
        env_opts = {cmd[i + 1].split('=', 1)[0]: cmd[i + 1].split('=', 1)[1] for i in range(len(cmd) - 1) if cmd[i] == '-e'}
        gsdir = opts.gs_dir.resolve()
        gsroot = out / 'gs-root'; (gsroot / 'backend').mkdir(parents=True, exist_ok=True)
        for sub in ('comms', 'config', 'layout'):
            if (gsdir / 'backend' / sub).exists() and not (gsroot / 'backend' / sub).exists():
                shutil.copytree(gsdir / 'backend' / sub, gsroot / 'backend' / sub)
        for link, target in ((gsroot / 'data', out / 'gs-data'), (gsroot / 'backend/data', out / 'gs-backend-data')):
            if not link.exists(): link.symlink_to(target)
        rtmp = out / 'host-tmp'; rtmp.mkdir(exist_ok=True)
        # Private /tmp (mount namespace): the station's built-in tree /tmp/seds-gs/backend is this run's.
        _sp.run(['mount', '--bind', str(rtmp), '/tmp'], check=True)
        (rtmp / 'seds-gs').mkdir(exist_ok=True)
        if not (rtmp / 'seds-gs/backend').exists(): (rtmp / 'seds-gs/backend').symlink_to(gsroot / 'backend')
        scratch = Path(tempfile.mkdtemp(prefix=f'seds-native-{os.getpid()}-', dir='/dev/shm' if Path('/dev/shm').is_dir() else None))
        NativeState.scratch = scratch
        (diag / 'temp' / 'native').symlink_to(scratch)
        mapping = container_map(out, config, diag, gsroot, gsdir, rtmp)
        pace = opts.native_pace
        for host in topo['host_nodes']:
            host['binary'] = str(native_gs_binary(gsdir)) if host['binary'] == '/usr/local/bin/groundstation_backend' else remap(host['binary'], mapping)
            host['cwd'] = str(gsroot) if host.get('cwd') in (None, '/opt/groundstation') else remap(host['cwd'], mapping)
            env = host['env']
            for k in list(env):
                env[k] = remap(env[k], mapping)
            # One time base: the station runs at the coordinator's pace (FAKETIME start kept, rate = pace).
            env.pop('GS_SIM_WALL_FRESHNESS_SCALE', None)
            start = re.match(r'@(\S+ \S+)', env.get('FAKETIME', ''))
            env['FAKETIME'] = '@%s x%.6f' % (start.group(1) if start else '2026-01-01 00:00:00', pace)
            env['LD_PRELOAD'] = str(gsdir / 'libfaketimeMT.so.1')
            env['FAKETIME_DONT_FAKE_MONOTONIC'] = '0'
            if host.get('network_variable_cache'):
                host['network_variable_cache'] = remap(host['network_variable_cache'], mapping)
        (config / 'topology.json').write_text(json.dumps(topo, indent=2))
        nodes = {}; probes = []; stimulus = []
        drift = dict((x.split(':')[0], int(x.split(':')[1])) for x in opts.drift)
        for node in topo['nodes']:
            nname = node['name']
            man = json.loads((builds / nname / f'{nname}.native.json').read_text())
            layout = json.loads((config / f'{nname}.json').read_text())
            board_layout = json.loads((Path(man['src']) / 'sim/board.json').read_text())
            dev = out / 'config' / f'{nname}.devices'; dev.write_text(devices_text(layout))
            syms = native_symbols(builds, nname)
            for pr in layout['execution'].get('memory_probes', []):
                addr = pr.get('address')
                if addr is None and pr.get('symbol') in syms:
                    addr = syms[pr['symbol']] + int(pr.get('byte_offset', 0))
                if addr is not None:
                    probes.append({'node': nname, 'name': pr['name'], 'address': addr})
            for p in layout.get('peripherals', []):
                if p.get('stimulus_path'):
                    device = 'mcp3564r' if p.get('model') == 'mcp3564r' else p.get('bus', '')
                    stimulus.append({'node': nname, 'device': device, 'path': remap(p['stimulus_path'], mapping)})
            cohort_factory = None
            for f in sorted((a.cohort / NODE_REPO[nname]).glob('build/*/*.factory.bin')):
                cohort_factory = str(f); break
            nodes[nname] = {'exe': man['exe'], 'flash_init': cohort_factory or man.get('factory_bin'),
                            'flash_size': (board_layout.get('memory') or {}).get('flash_size', 524288), 'devices': str(dev),
                            'drift_ppm': drift.get(nname, 0), 'env': {}}
        gates = []
        for spec in env_opts.get('SEDS_UART_GATE', '').split(';'):
            parts = spec.split(':')
            if len(parts) == 3:
                gates.append({'node': parts[0], 'peripheral': parts[1], 'path': remap(parts[2], mapping)})
        native = {'nodes': nodes, 'pace': pace, 'quantum_us': opts.native_quantum_us, 'max_skip_us': opts.native_max_skip_us,
                  'scratch': str(scratch), 'diagnostics': str(diag), 'stimulus': stimulus, 'probes': probes, 'uart_gates': gates,
                  'seed': opts.native_seed, 'can_trace': 'FIRMWARE_SIM_CAN_TRACE_DIR' in env_opts}
        (config / 'native.json').write_text(json.dumps(native, indent=1))
        ncmd = [str(opts.firmware_sim), 'native-bay', '--topology', str(config / 'topology.json'), '--native', str(config / 'native.json')]
        NativeState.native_cmd = ncmd; NativeState.net = net
        (out / 'command.json').write_text(json.dumps({'engine': 'native', 'command': ncmd, 'renode_equivalent': cmd}, indent=2))
        return out, diag, key, ncmd, name, net, missing

    FN.build_config = build_config
    FN.subprocess = LocalDocker(_sp)


def main():
    opts, rest = own_args(sys.argv[1:])
    if os.environ.get('SEDS_NATIVE_NETNS') != '1':
        cohort = Path(controller_arg(rest, '--cohort', str(ROOT.parent)))
        nodes = (controller_arg(rest, '--nodes') or 'rf,power,flight,gateway,actuator,valve,daq').split(',')
        ensure_builds(opts.native_builds, cohort, nodes, opts.native_build_jobs)
        if not opts.gs_dir:
            image = controller_arg(rest, '--image', 'seds-faithful:latest')
            opts.gs_dir = extract_gs(image, ROOT / 'build/native/gs' / re.sub(r'[^A-Za-z0-9_.-]', '_', image))
            sys.argv += ['--gs-dir', str(opts.gs_dir)]
        if not opts.firmware_sim:
            sys.argv += ['--firmware-sim', str(ensure_firmware_sim(None))]
        reexec_in_netns()
        return
    reexec_in_netns()
    # The controller's definitions run first; its __main__ block runs after the engine is installed.
    src = opts.controller.read_text()
    cut = src.index("\nif __name__ == '__main__':")
    FN = types.ModuleType('fullnet_controller'); FN.__file__ = str(opts.controller.resolve())
    sys.modules['fullnet_controller'] = FN
    exec(compile(src[:cut], str(opts.controller), 'exec'), FN.__dict__)
    NativeState.opts = opts
    install(FN, opts)
    sys.argv = [str(opts.controller)] + rest
    try:
        FN.__dict__['__name__'] = '__main__'
        main_block = '\n' * src[:cut].count('\n') + src[cut:]
        exec(compile(main_block, str(opts.controller), 'exec'), FN.__dict__)
    finally:
        ev = controller_arg(rest, '--evidence')
        try:
            # The engine's own record beside the scenario result: pace held (the station shares the time base only
            # while the lockstep keeps up with it), steps, CAN/Pico traffic, resets.
            res_p = Path(ev) / 'result.json'; rep_p = Path(ev) / 'diagnostics/native-report.json'
            if res_p.exists() and rep_p.exists():
                res = json.loads(res_p.read_text()); rep = json.loads(rep_p.read_text())
                res['engine'] = 'native'; res['native_engine'] = rep
                res['native_pace_held'] = rep.get('max_lag_ms', 0) <= 500
                res_p.write_text(json.dumps(res, indent=2, default=str) + '\n')
        except (OSError, ValueError):
            pass
        if NativeState.scratch and NativeState.scratch.exists():
            for f in NativeState.scratch.glob('*.log'):
                try: shutil.copyfile(f, Path(controller_arg(rest, '--evidence')) / 'diagnostics' / f'native-{f.name}')
                except OSError: pass
            shutil.rmtree(NativeState.scratch, ignore_errors=True)


if __name__ == '__main__':
    main()
