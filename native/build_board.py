#!/usr/bin/env python3
"""Build one board's production firmware as a host (x86, 32-bit) SITL executable.

The board's own CMake (ARM toolchain) is configured, never built, so the exact source list, defines and include
paths of the flight image come from its compile_commands.json/build.ninja, and the SEDSnet SDK is prepared with
the board's schema and build options. Then:
  * SEDSnet (the board's prepared SDK, embedded feature set and env) is built for i686-unknown-linux-gnu through
    a tiny wrapper crate that supplies the embedded allocator/panic hooks the bare-metal build gets from the SDK;
  * every application, ThreadX-kernel, FileX, LaunchCore and middleware source of the flight link is compiled
    with host gcc -m32 against the board's real HAL/CMSIS headers plus native/shim (hardware boundary);
  * HAL driver sources, the ThreadX Cortex-M port, startup/syscalls/sysmem and USBX are replaced by the shim.

usage: build_board.py <board-src> --out <dir> [--node NAME] [--sdk DIR[@REF]] [--launchcore DIR@REF] [--filex DIR@REF]
                      [--define K=V ...] [--jobs N] [--keep-arm]
Output: <out>/<node>.native (+ <out>/<node>.native.json manifest, <out>/<node>.symbols for probes).
"""
from __future__ import annotations
import argparse, hashlib, io, json, os, re, shlex, shutil, subprocess, sys, tarfile, time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HERE = Path(__file__).resolve().parent
SHIM = HERE / 'shim'
SEDS = HERE.parents[3] if (HERE.parents[3] / 'SEDSLaunchCore').exists() else Path('/home/rylan/Documents/Gitkraken/SEDS')
CMAKE = shutil.which('cmake') or '/home/rylan/st/stm32cubeclt_1.22.0/CMake/bin/cmake'
# Host ABI: 32-bit like the MCU. "i686" on x86_64 build/run hosts; "armhf" (ARMv7 hard-float, statically linked so a
# 64-bit Raspberry Pi with 32-bit compat needs no armhf libraries) for aarch64 stations. FIRMWARE_SIM_HOST selects it.
HOSTS = {
    'i686': {'cc': 'gcc', 'cflags': ['-m32'], 'target': 'i686-unknown-linux-gnu', 'nm': 'nm', 'ldflags': ['-m32', '-no-pie']},
    'armhf': {'cc': 'arm-linux-gnueabihf-gcc', 'cflags': ['-marm', '-mfloat-abi=hard'], 'target': 'armv7-unknown-linux-gnueabihf',
              'nm': 'arm-linux-gnueabihf-nm', 'ldflags': ['-static', '-no-pie'],
              'cargo_env': {'CARGO_TARGET_ARMV7_UNKNOWN_LINUX_GNUEABIHF_LINKER': 'arm-linux-gnueabihf-gcc'}},
}
HOST = HOSTS[os.environ.get('FIRMWARE_SIM_HOST', 'i686')]
HOST_TARGET = HOST['target']
REPO_NODE = {'gateway-board26': 'gateway', 'ActuatorBoard26': 'actuator', 'ValveBoard26': 'valve', 'DAQ-Board': 'daq',
             'FlightComputer26': 'flight', 'RFBoard26': 'rf', 'PowerBoard26': 'power'}
# Hardware-only translation units, replaced by the shim (matched against the path).
EXCLUDE = [r'/Drivers/STM32[A-Z0-9]+xx_HAL_Driver/Src/', r'/threadx/ports/', r'/startup_stm32', r'/Core/Src/syscalls\.c$',
           r'/Core/Src/sysmem\.c$', r'/Core/Src/system_stm32', r'\.[sS]$', r'/usbx/', r'/USBX/', r'/Core/Src/stm32[a-z0-9]+xx_it\.c$', r'/Middlewares/ST/usbx',
           r'/ux_', r'_hal_timebase_tim\.c$', r'/Core/Src/abort_unwind\.c$', r'/filex/ports/', r'/levelx/']


# Options each board's own build.py passes on a normal firmware build (CMake defaults differ).
BOARD_DEFINES = {'FlightComputer26': ['ENABLE_TELEMETRY=ON']}
# Retained-RAM output sections (names from the board's link script) and link-time symbols the replaced startup
# code would have provided.
BOARD_RETAINED = {'FlightComputer26': ('.fc_watchdog_retained', '__fc_watchdog_retained_start', '__fc_watchdog_retained_end')}
BOARD_DEFSYMS = {'FlightComputer26': {'g_pfnVectors': 0x08002200, '__data_source_end': 0x08002200 + 0x70000}}


def log(*a):
    print('[native-build]', *a, flush=True)


def run(cmd, **kw):
    r = subprocess.run(cmd, capture_output=True, text=True, **kw)
    if r.returncode:
        raise SystemExit(f'command failed ({r.returncode}): {shlex.join(map(str, cmd))}\n{r.stdout[-6000:]}\n{r.stderr[-6000:]}')
    return r.stdout


def git_archive(repo: Path, ref: str, dest: Path):
    if dest.exists():
        shutil.rmtree(dest)
    if not (repo / '.git').exists() and not (repo / 'HEAD').exists():
        # a plain source tree (a staged copy for a remote or container build): copied as is, per build
        shutil.copytree(repo, dest, symlinks=True)
        stamp = repo / '.source-commit'
        return stamp.read_text().strip() if stamp.exists() else 'tree'
    dest.mkdir(parents=True)
    data = subprocess.check_output(['git', '-C', str(repo), 'archive', ref])
    with tarfile.open(fileobj=io.BytesIO(data)) as t:
        t.extractall(dest, filter='data')
    return subprocess.check_output(['git', '-C', str(repo), 'rev-parse', ref], text=True).strip()


def spec(value, default_ref='HEAD'):
    d, _, ref = value.partition('@')
    return Path(d), ref or default_ref


def provenance_deps(src: Path):
    p = src.parent / 'PROVENANCE.json'
    return json.loads(p.read_text()).get('_deps', {}) if p.exists() else {}


def cohort_options(src: Path):
    """User options of an existing ARM build of this tree (cohorts record e.g. -DFLIGHT_STATE_PROBE=ON)."""
    opts = {}
    for cache in sorted(src.glob('build/*/CMakeCache.txt')):
        for line in cache.read_text(errors='replace').splitlines():
            m = re.match(r'^([A-Z][A-Z0-9_]*):(BOOL|STRING)=(.*)$', line)
            if not m: continue
            k, t, v = m.groups()
            if k.startswith(('CMAKE_', 'FETCHCONTENT_', 'SEDSNET_ENV_CARGO', 'Python3_', 'SEDSNET_DIR', 'SEDSNET_REPOSITORY')): continue
            if k.endswith(('_DIR', '_EXECUTABLE', '_PATH', '_FILE', '_LIBRARY')): continue
            opts[k] = (t, v)
        break
    return opts


def configure(src: Path, out: Path, a):
    """ARM configure (no build) with pinned deps; returns the ARM build dir."""
    deps = out / 'deps'; arm = out / 'arm'
    pins = provenance_deps(src)
    sdk_dir, sdk_ref = spec(a.sdk) if a.sdk else ((Path(pins['sdk_dir']), pins['sdk']) if pins.get('sdk_dir') else (SEDS / 'SEDSnet', 'origin/main'))
    lc_dir, lc_ref = spec(a.launchcore) if a.launchcore else (SEDS / 'SEDSLaunchCore', pins.get('launchcore') or 'e2eea7d94245168dc63d31e160f4cb7bdc7688c3')
    fx_dir, fx_ref = spec(a.filex) if a.filex else (SEDS / '.workspace-progress/firmware-public439-qualification-20261103/deps/DAQ-Board/filex-src',
                                                    pins.get('filex') or 'b93ac4ae77ee12196f15f341cffb0a9810d2484f')
    stamp = {'src': str(src), 'sdk': [str(sdk_dir), sdk_ref], 'launchcore': [str(lc_dir), lc_ref], 'filex': [str(fx_dir), fx_ref],
             'defines': a.define, 'cohort_options': cohort_options(src)}
    stamp_path = out / 'configure.json'
    if stamp_path.exists() and (arm / 'build.ninja').exists() and json.loads(stamp_path.read_text()).get('inputs') == stamp:
        return arm, json.loads(stamp_path.read_text())
    shutil.rmtree(arm, ignore_errors=True)
    resolved = {'sdk': git_archive(sdk_dir, sdk_ref, deps / 'sdk'), 'launchcore': git_archive(lc_dir, lc_ref, deps / 'launchcore')}
    if fx_dir.exists():
        resolved['filex'] = git_archive(fx_dir, fx_ref, deps / 'filex')
    cfg = [CMAKE, '-G', 'Ninja', '-S', str(src), '-B', str(arm), '-DCMAKE_BUILD_TYPE=Release',
           '-DCMAKE_TOOLCHAIN_FILE=' + str(src / 'cmake/gcc-arm-none-eabi.cmake'),
           '-DFETCHCONTENT_SOURCE_DIR_SEDSNET=' + str(deps / 'sdk'), '-DFETCHCONTENT_SOURCE_DIR_SEDSLAUNCHCORE=' + str(deps / 'launchcore'),
           '-DFETCHCONTENT_SOURCE_DIR_FILEX=' + str(deps / 'filex'), '-DFETCHCONTENT_BASE_DIR=' + str(out / 'fetch'),
           '-DSEDSNET_ENV_CARGO_TARGET_DIR=' + str(out / 'cargo-arm'), '-DSEDS_FIRMWARE_SIM_TEST=OFF']
    for k, (t, v) in stamp['cohort_options'].items():
        cfg.append(f'-D{k}:{t}={v}')
    if not stamp['cohort_options']:
        cfg += [f'-D{d}' for d in BOARD_DEFINES.get(src.name, [])]
    cfg += [f'-D{d}' for d in a.define]
    env = {k: v for k, v in os.environ.items() if not k.startswith(('CARGO_', 'RUSTFLAGS', 'SEDSNET_TEST_'))}
    log('configure', src.name, '(ARM, configure only)')
    run(cfg, env=env, cwd=src)
    stamp_path.write_text(json.dumps({'inputs': stamp, 'resolved': resolved}, indent=1))
    return arm, json.loads(stamp_path.read_text())


def ninja_link(arm: Path):
    """Objects of the firmware executable link (not the bootloader), including those of the static libraries it
    links (e.g. FileX), and the SEDSnet build.py command."""
    text = (arm / 'build.ninja').read_text().replace('$\n', ' ')
    links = []
    for m in re.finditer(r'^build (\S+\.elf): (C_EXECUTABLE_LINKER\S*|CXX_EXECUTABLE_LINKER\S*) (.*)$', text, re.M):
        elf, rule, rest = m.groups()
        tail = text[m.end():m.end() + 200000].split('\nbuild ', 1)[0]
        links.append((elf, rest, tail))
    links = [l for l in links if 'Bootloader' not in l[0] and 'bootloader' not in l[0]]
    assert len(links) == 1, [l[0] for l in links]
    elf, rest, tail = links[0]
    objs = [o for o in rest.split('|')[0].split() if not o.endswith('.a')]
    libs = set(re.findall(r'(\S+\.a)\b', rest + ' ' + tail))
    statics = {}
    for m in re.finditer(r'^build (\S+\.a): C_STATIC_LIBRARY_LINKER\S* (.*)$', text, re.M):
        statics[m.group(1)] = m.group(2).split('|')[0].split()
    for lib in libs:
        for key, members in statics.items():
            if key == lib or key.endswith('/' + os.path.basename(lib)):
                objs += [o for o in members if o not in objs]
    m = re.search(r'build\.py (build --crate-type staticlib[^\n&|]*?)(?:\s*&&|\s*$)', text, re.M)
    assert m, 'SEDSnet build.py command not found in build.ninja'
    return elf, [o.replace('$ ', ' ') for o in objs], shlex.split(m.group(1))


def compile_db(arm: Path):
    db = json.loads((arm / 'compile_commands.json').read_text())
    by_out = {}
    for e in db:
        args = e.get('arguments') or shlex.split(e['command'])
        out = e.get('output')
        if out is None:
            out = args[args.index('-o') + 1] if '-o' in args else None
        by_out[os.path.normpath(os.path.join(e['directory'], out))] = (e['file'] if os.path.isabs(e['file']) else os.path.join(e['directory'], e['file']), args, e['directory'])
    return by_out


def host_flags(args):
    """Defines and include dirs of an ARM compile, as host flags; arch, LTO, specs and warnings dropped."""
    keep = []; it = iter(args[1:])
    for x in it:
        if x in ('-o', '-c', '-MF', '-MT', '-MQ'):
            next(it, None); continue
        if x.startswith(('-D', '-I', '-std=', '-include')):
            if x in ('-D', '-I', '-include'):
                x = x + next(it)
            if x.startswith('-I') and 'CMSIS' not in x and not os.path.isdir(x[2:]):
                continue
            keep.append(x)
        elif x in ('-isystem', '-iquote'):
            keep += [x, next(it)]
    return keep


def build_sdk(out: Path, arm: Path, build_args, jobs):
    """Host i686 staticlib of the board's prepared SDK through the hooks wrapper (same features/env as flight)."""
    sdk = None
    for line in (arm / 'CMakeCache.txt').read_text().splitlines():
        if line.startswith('SEDSNET_DIR:PATH='):
            sdk = Path(line.split('=', 1)[1])
    assert sdk and (sdk / 'Cargo.toml').exists(), sdk
    p = argparse.ArgumentParser(); p.add_argument('cmd'); p.add_argument('--crate-type'); p.add_argument('--features', action='append', default=[])
    p.add_argument('--target'); p.add_argument('--release', action='store_true'); p.add_argument('--device-id', default='')
    for f in ('--max-stack-payload', '--max-queue-budget', '--max-recent-rx-ids', '--static-schema-path', '--static-ipc-schema-path'):
        p.add_argument(f)
    p.add_argument('--env', action='append', default=[])
    ba = p.parse_args([x.replace('--target=', '--target=') for x in build_args])
    features = [f for v in ba.features for f in re.split(r'[,\s]+', v) if f]
    env = {k: v for k, v in os.environ.items() if not k.startswith(('CARGO_', 'RUSTFLAGS', 'SEDSNET_', 'MAX_', 'DEVICE_IDENTIFIER'))}
    if ba.device_id: env['DEVICE_IDENTIFIER'] = ba.device_id
    for k in ('max_stack_payload', 'max_queue_budget', 'max_recent_rx_ids'):
        if getattr(ba, k): env[k.upper()] = getattr(ba, k)
    if ba.static_schema_path: env['SEDSNET_STATIC_SCHEMA_PATH'] = ba.static_schema_path
    if ba.static_ipc_schema_path: env['SEDSNET_STATIC_IPC_SCHEMA_PATH'] = ba.static_ipc_schema_path
    for kv in ba.env:
        k, _, v = kv.partition('=')
        if k != 'CARGO_TARGET_DIR': env[k] = v
    # The SDK manifest also emits its own staticlib, which has no allocator/panic hooks off target_os="none";
    # only this private archive copy is built as an rlib under the hooks wrapper (the SEDSnet sources are untouched).
    manifest = (sdk / 'Cargo.toml').read_text()
    rlib = re.sub(r'(\[lib\][^\[]*?crate-type\s*=\s*)\[[^\]]*\]', r'\1["rlib"]', manifest, count=1, flags=re.S)
    if rlib != manifest:
        (sdk / 'Cargo.toml').write_text(rlib)
    wrap = out / 'sedsnet-host'; (wrap / 'src').mkdir(parents=True, exist_ok=True)
    (wrap / 'Cargo.toml').write_text(f'''[package]
name = "sedsnet_host"
version = "0.0.0"
edition = "2021"
publish = false
[lib]
crate-type = ["staticlib"]
path = "src/lib.rs"
[dependencies]
sedsnet = {{ package = "SEDSnet", path = "{sdk}", default-features = false, features = {json.dumps(features)} }}
[profile.release]
opt-level = 2
debug = 1
lto = false
codegen-units = 16
panic = "abort"
overflow-checks = false
incremental = false
[workspace]
''')
    shutil.copyfile(HERE / 'sedsnet_host_hooks.rs', wrap / 'src/lib.rs')
    lock = sdk / 'Cargo.lock'
    if lock.exists():
        shutil.copyfile(lock, wrap / 'Cargo.lock')
    env['CARGO_TARGET_DIR'] = str(out / 'cargo-host')
    env['CARGO_BUILD_JOBS'] = str(jobs)
    env['CARGO_INCREMENTAL'] = '0'
    log('sedsnet host staticlib', ' '.join(features), {k: env[k] for k in env if k in ('DEVICE_IDENTIFIER',) or k.startswith(('MAX_', 'QUEUE', 'STARTING', 'PAYLOAD'))})
    env.update(HOST.get('cargo_env', {}))
    run(['cargo', 'build', '--release', '--target', HOST_TARGET, '--manifest-path', str(wrap / 'Cargo.toml')], env=env)
    lib = out / 'cargo-host' / HOST_TARGET / 'release' / 'libsedsnet_host.a'
    final = out / 'libsedsnet_host.a'; shutil.copyfile(lib, final)
    return final, sdk


SHIM_FLAGS = [*HOST['cflags'], '-g', '-O1', '-fno-strict-aliasing', '-fwrapv', '-fno-common', '-pthread', '-w',
              '-fno-omit-frame-pointer', '-fno-pie', '-DSEDS_NATIVE_SITL=1', '-D__SEDS_NATIVE_SITL__=1']


def up_to_date(obj: Path, key: str):
    d = obj.with_suffix('.d'); k = obj.with_suffix('.key')
    if not (obj.exists() and d.exists() and k.exists()) or k.read_text() != key:
        return False
    t = obj.stat().st_mtime
    deps = d.read_text().replace('\\\n', ' ').split(':', 1)[-1].split()
    try:
        return all(os.stat(x).st_mtime <= t for x in deps)
    except OSError:
        return False


def compile_one(job):
    src, flags, obj = job
    obj.parent.mkdir(parents=True, exist_ok=True)
    cmd = [HOST['cc'], *flags, '-MMD', '-MF', str(obj.with_suffix('.d')), '-c', src, '-o', str(obj)]
    key = hashlib.sha256(json.dumps(cmd).encode()).hexdigest()
    if up_to_date(obj, key):
        return src, 0, '', cmd
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode == 0:
        obj.with_suffix('.key').write_text(key)
    return src, r.returncode, (r.stderr[-4000:] if r.returncode else ''), cmd


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('src', type=Path)
    ap.add_argument('--out', type=Path, required=True)
    ap.add_argument('--node')
    ap.add_argument('--sdk'); ap.add_argument('--launchcore'); ap.add_argument('--filex')
    ap.add_argument('--define', action='append', default=[])
    ap.add_argument('--jobs', type=int, default=4)
    ap.add_argument('--keep-arm', action='store_true', help='keep the ARM configure tree and SDK cargo target')
    ap.add_argument('--factory', action='store_true',
                    help='also build the ARM factory image (bootloader+app+metadata) once, as the native flash first-boot image')
    a = ap.parse_args()
    src = a.src.resolve(); node = a.node or REPO_NODE[src.name]
    out = (a.out / node).resolve(); out.mkdir(parents=True, exist_ok=True)
    t0 = time.monotonic()
    arm, conf = configure(src, out, a)
    elf, objs, sdk_args = ninja_link(arm)
    db = compile_db(arm)
    sdk_stamp = out / 'sedsnet-host.stamp'; key = json.dumps([conf, sdk_args, (HERE / 'sedsnet_host_hooks.rs').read_text()], sort_keys=True)
    if (out / 'libsedsnet_host.a').exists() and sdk_stamp.exists() and sdk_stamp.read_text() == key:
        lib = out / 'libsedsnet_host.a'
    else:
        lib, _ = build_sdk(out, arm, sdk_args, a.jobs)
        sdk_stamp.write_text(key)
    family = None; sources = []; skipped = []
    for o in objs:
        key = os.path.normpath(os.path.join(arm, o))
        if key not in db:
            if o.endswith(('.a', '.o', '.obj')) and not o.endswith('.obj'):
                skipped.append(o)
            continue
        file, args, _ = db[key]
        if any(re.search(p, file) for p in EXCLUDE):
            skipped.append(file); continue
        flags = host_flags(args)
        fam = next((x[2:] for x in flags if re.match(r'-DSTM32[A-Z]\d{3}', x)), None)
        family = family or fam
        sources.append((file, flags))
    assert family, 'device define not found'
    # Shim: compiled with the board's own include set (HAL/CMSIS/device/ThreadX/FileX headers) so its handle
    # types are the board's; the shim headers come first.
    app_flags = next(f for s, f in sources if '/Core/Src/main.c' in s)
    shim_inc = [f'-I{SHIM / "include"}', f'-include', str(SHIM / 'include' / 'sim_preinclude.h')]
    jobs = []
    objdir = out / 'obj'
    for i, (file, flags) in enumerate(sources):
        extra = []
        if file.endswith('/Core/Src/main.c'):
            extra = ['-Dmain=sim_board_main']
        name = re.sub(r'[^A-Za-z0-9_.]', '_', os.path.relpath(file, src) if file.startswith(str(src)) else file)[-120:]
        jobs.append((file, SHIM_FLAGS + shim_inc + flags + extra, objdir / f'{i:03d}_{name}.o'))
    for i, f in enumerate(sorted((SHIM / 'src').glob('*.c'))):
        jobs.append((str(f), SHIM_FLAGS + ['-Wall', '-Wno-unused-function', '-D_GNU_SOURCE', '-DSIM_SHIM_TU'] + shim_inc + app_flags, objdir / f'shim_{f.stem}.o'))
    log(node, f'{len(sources)} firmware sources ({family}), {len(skipped)} replaced by the shim; compiling with {a.jobs} jobs')
    fails = []
    with ThreadPoolExecutor(a.jobs) as ex:
        for s, rc, err, cmd in ex.map(compile_one, jobs):
            if rc: fails.append((s, err, cmd))
    if fails:
        for s, err, cmd in fails[:12]:
            print('---', s, '\n', err, file=sys.stderr)
        (out / 'failed-compile.txt').write_text('\n'.join(shlex.join(c) for _, _, c in fails))
        raise SystemExit(f'{len(fails)} compile failures')
    extra_link = []
    ret = BOARD_RETAINED.get(src.name)
    script = out / 'native-retained.ld'
    sec, a0, a1 = ret if ret else ('.sim_retained_none', '__sim_retained_unused0', '__sim_retained_unused1')
    script.write_text(f'''SECTIONS {{ .sim_retained (NOLOAD) : ALIGN(32) {{ __sim_retained_start = .; {a0} = .; KEEP(*({sec})) {a1} = .; __sim_retained_end = .; }} }} INSERT AFTER .bss;\n''')
    extra_link += [f'-Wl,-T,{script}']
    for k, v in BOARD_DEFSYMS.get(src.name, {}).items():
        extra_link.append(f'-Wl,--defsym,{k}=0x{v:08x}')
    exe = out / f'{node}.native'
    link = [HOST['cc'], *HOST['ldflags'], '-Wl,-Ttext-segment=0x70000000', '-g', '-o', str(exe), *[str(j[2]) for j in jobs], str(lib), '-Wl,--allow-multiple-definition',
            '-lpthread', '-lm', '-ldl', '-Wl,-z,noexecstack', *extra_link]
    r = subprocess.run(link, capture_output=True, text=True)
    if r.returncode:
        undef = sorted(set(re.findall(r"undefined reference to `([^']+)'", r.stderr)))
        (out / 'undefined.txt').write_text('\n'.join(undef) + '\n')
        raise SystemExit(f'link failed: {len(undef)} undefined symbols (see {out / "undefined.txt"})\n' + r.stderr[-3000:])
    syms = run([HOST['nm'], '-n', '--defined-only', str(exe)])
    (out / f'{node}.symbols').write_text(syms)
    factory = next(iter(sorted(src.glob('build/*/*.factory.bin'))), None)
    if factory is None and (out / 'factory.bin').exists():
        factory = out / 'factory.bin'
    if factory is None and a.factory:
        log(node, 'building the ARM factory image (first-boot flash)')
        env = {k: v for k, v in os.environ.items() if not k.startswith(('CARGO_', 'RUSTFLAGS', 'SEDSNET_TEST_'))}
        env['CARGO_BUILD_JOBS'] = str(a.jobs)
        run([CMAKE, '--build', str(arm), '--target', 'factory-image', '--parallel', str(a.jobs)], env=env)
        built = next(iter(sorted(arm.glob('*.factory.bin'))))
        shutil.copyfile(built, out / 'factory.bin'); factory = out / 'factory.bin'
        run([CMAKE, '--build', str(arm), '--target', 'clean'], env=env)
    layout = json.loads((src / 'sim/board.json').read_text())
    manifest = {'node': node, 'repo': src.name, 'src': str(src), 'family': family, 'exe': str(exe), 'factory_bin': str(factory) if factory else None,
                'memory': layout.get('memory'), 'configure': conf, 'sedsnet_args': sdk_args, 'sources': len(sources),
                'replaced': skipped, 'seconds': round(time.monotonic() - t0, 1),
                'commit': subprocess.run(['git', '-C', str(src), 'rev-parse', 'HEAD'], capture_output=True, text=True).stdout.strip() or None}
    (out / f'{node}.native.json').write_text(json.dumps(manifest, indent=1))
    if not a.keep_arm:
        # The host SDK archive is kept (libsedsnet_host.a); its cargo target (~240 MB) is not.
        shutil.rmtree(out / 'cargo-arm', ignore_errors=True)
        shutil.rmtree(out / 'cargo-host', ignore_errors=True)
    log(node, 'built', exe, f'{manifest["seconds"]} s')


if __name__ == '__main__':
    main()
