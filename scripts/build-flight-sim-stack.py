#!/usr/bin/env python3
"""Build the inputs for the operational flight simulation.

Steps (each can be run on its own; outputs go under --work):

  firmware  Release FC/RF/PB images from source snapshots (FC from its
            worktree HEAD, RF/PB from the given checkouts' HEAD) using each
            board's own `build.py build --release` with pinned SDK/LaunchCore
            sources, and assemble a seven-board cohort that reuses already
            built fill-system boards (gateway/actuator/valve/DAQ).
  gs        GroundStation backend (features firmware_sim only, i.e. the flight
            build) and the web UI (dist/public) in the offline builder image.
  tiles     A tiny offline placeholder tile set around the launch site.
  image     Runtime image: base simulator image + GS + UI + tiles + this
            checkout's Renode models.

All CPU use is capped (taskset / docker --cpus) to --cpus cores.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import math
import os
import shutil
import subprocess
import sys
import tarfile
import time
from pathlib import Path

SEDS = Path('/home/rylan/Documents/Gitkraken/SEDS')
WP = SEDS / '.workspace-progress'
HERE = Path(__file__).resolve().parent
SIM_ROOT = HERE.parent
DEFAULTS = {
    'fc': WP / 'states-fc-sim-20261108/FlightComputer26',
    'gs': WP / 'states-fc-sim-20261108/GroundStation26',
    'ui': WP / 'states-fc-sim-20261108/Seds-Ground-Station-Frontend',
    'rf': SEDS / 'RFBoard26',
    'pb': SEDS / 'PowerBoard26',
    'fill_cohort': WP / 'testfire-final2-20261108/cohort4',
    'sdk': (WP / 'published-board-debug-release-20261106/sdk-source', '2d2bcfb53f5ddca93c21858f0b8d5aaa0f03b7fc'),
    'launchcore': (SEDS / 'SEDSLaunchCore', 'e2eea7d94245168dc63d31e160f4cb7bdc7688c3'),
    'builder': 'seds-current411-builder-cache:compiled-baseline5',
    'cargo_extra': WP / 'testfire-final-20261107/build/cargo-extra',
    'base_image': 'seds-linked-current411-sim1791-model4:20261107',
    'firmware_sim': WP / 'testfire-final2-20261108/build/sim/firmware-sim',
}
CMAKE = '/home/rylan/st/stm32cubeclt_1.22.0/CMake/bin/cmake'
# Board -> (repo name, build subdirectory expected by sim/board.json loaders)
BOARDS = {'fc': ('FlightComputer26', 'Release'), 'rf': ('RFBoard26', 'Release'), 'pb': ('PowerBoard26', 'Release_Script')}
LAUNCH_SITE = (31.758, -102.046)  # FlightComputer26 Core/Inc/fcconfig.h LAUNCH_SITE_LAT/LON


def sh(cmd, log=None, env=None, cwd=None, cpus=3):
    cmd = [str(c) for c in cmd]
    if cmd[0] != 'docker':
        cmd = ['taskset', '-c', ','.join(str(i) for i in range(cpus))] + cmd
    print('+', ' '.join(cmd), flush=True)
    if log is None:
        subprocess.run(cmd, check=True, env=env, cwd=cwd)
    else:
        with open(log, 'a') as handle:
            rc = subprocess.run(cmd, env=env, cwd=cwd, stdout=handle, stderr=subprocess.STDOUT).returncode
        if rc:
            raise SystemExit(f'command failed ({rc}); see {log}')


def head(repo: Path) -> str:
    return subprocess.check_output(['git', '-C', str(repo), 'rev-parse', 'HEAD'], text=True).strip()


def dirty(repo: Path) -> list[str]:
    return subprocess.check_output(['git', '-C', str(repo), 'status', '--porcelain'], text=True).splitlines()


def archive(repo: Path, ref: str, dest: Path):
    dest.mkdir(parents=True, exist_ok=False)
    data = subprocess.check_output(['git', '-C', str(repo), 'archive', ref])
    with tarfile.open(fileobj=io.BytesIO(data)) as tar:
        tar.extractall(dest, filter='data')


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def firmware(args):
    work = args.work / 'firmware'
    deps = work / 'deps'
    if not (deps / 'sdk').exists():
        archive(DEFAULTS['sdk'][0], DEFAULTS['sdk'][1], deps / 'sdk')
        archive(DEFAULTS['launchcore'][0], DEFAULTS['launchcore'][1], deps / 'launchcore')
    cohort = args.work / 'cohort'
    cohort.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    for key in list(env):
        if key in ('RUSTFLAGS', 'CARGO_ENCODED_RUSTFLAGS', 'CARGO_TARGET_DIR') or key.startswith(('CARGO_PROFILE_', 'SEDSNET_TEST_')):
            env.pop(key)
    env.update(CARGO_BUILD_JOBS=str(args.cpus), CMAKE_BUILD_PARALLEL_LEVEL=str(args.cpus), SEDS_FIRMWARE_SIM_TEST='0')
    provenance = {'sdk': DEFAULTS['sdk'][1], 'launchcore': DEFAULTS['launchcore'][1], 'boards': {}}
    for key in args.boards:
        repo_name, subdir = BOARDS[key]
        repo = getattr(args, key)
        commit = head(repo)
        changes = dirty(repo)
        src = cohort / repo_name
        if src.exists():
            shutil.rmtree(src)
        archive(repo, commit, src)
        build = src / 'build' / subdir
        log = work / f'{repo_name}-build.log'
        log.unlink(missing_ok=True)
        # Seed the cache with pinned local dependency sources, then let the
        # board's own build.py configure/build its normal Release image.
        sh([CMAKE, '-G', 'Ninja', '-S', src, '-B', build, '-DCMAKE_BUILD_TYPE=Release',
            '-DCMAKE_TOOLCHAIN_FILE=' + str(src / 'cmake/gcc-arm-none-eabi.cmake'),
            '-DFETCHCONTENT_SOURCE_DIR_SEDSNET=' + str(deps / 'sdk'),
            '-DFETCHCONTENT_SOURCE_DIR_SEDSLAUNCHCORE=' + str(deps / 'launchcore'),
            '-DFETCHCONTENT_BASE_DIR=' + str(work / 'fetch' / repo_name),
            '-DSEDSNET_ENV_CARGO_TARGET_DIR=' + str(work / 'cargo' / repo_name)], log=log, env=env, cwd=src, cpus=args.cpus)
        sh([sys.executable, 'build.py', '--build-subdir', subdir, 'build', '--release'], log=log, env=env, cwd=src, cpus=args.cpus)
        text = log.read_text(errors='replace')
        usage = [line.strip() for line in text.splitlines() if line.strip().startswith(('FLASH', 'RAM', 'Memory region', 'SRAM', 'APP'))]
        cache = (build / 'CMakeCache.txt').read_text()
        sim_test = [line for line in cache.splitlines() if line.startswith('SEDS_FIRMWARE_SIM_TEST')]
        provenance['boards'][repo_name] = {
            'source': str(repo), 'commit': commit, 'source_dirty_paths_ignored': changes,
            'build_subdir': subdir, 'sim_test': sim_test, 'memory_usage_lines': usage,
            'artifacts': {p.name: sha(p) for p in build.iterdir() if p.suffix in ('.elf', '.bin', '.img')},
        }
        print(json.dumps({repo_name: usage}, indent=1), flush=True)
    for name in ('gateway-board26', 'ActuatorBoard26', 'ValveBoard26', 'DAQ-Board'):
        link = cohort / name
        if not link.exists():
            link.symlink_to(args.fill_cohort / name)
        provenance.setdefault('fill_boards', {})[name] = str((args.fill_cohort / name).resolve())
    previous = cohort / 'PROVENANCE.json'
    if previous.exists():
        old = json.loads(previous.read_text())
        old.get('boards', {}).update(provenance['boards'])
        provenance['boards'] = {**old.get('boards', {}), **provenance['boards']}
    previous.write_text(json.dumps(provenance, indent=2) + '\n')


def snapshot(repo: Path, dest: Path):
    """Tracked + untracked (non-ignored) working-tree files, i.e. include uncommitted edits."""
    if dest.exists():
        shutil.rmtree(dest)
    files = subprocess.check_output(['git', '-C', str(repo), 'ls-files', '-co', '--exclude-standard', '-z'], text=True).split('\0')
    for rel in filter(None, files):
        source = repo / rel
        if source.is_file():
            target = dest / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)


COMPILE = r'''#!/bin/bash
set -euo pipefail
trap 'chmod -R a+rwX /output || true' EXIT
R=/usr/local/cargo/registry
cp /extra/cache/codec2-0.3.1.crate $R/cache/index.crates.io-1949cf8c6b5b557f/
mkdir -p $R/index/index.crates.io-1949cf8c6b5b557f/.cache/co/de; cp /extra/index/codec2 $R/index/index.crates.io-1949cf8c6b5b557f/.cache/co/de/codec2
cp -a /inputs/gs /src/gs-head; cp /src/gs/Cargo.lock /src/gs-head/Cargo.lock
export GS_FRONTEND_SOURCE_DIR=/src/frontend SEDSNET_STATIC_SCHEMA_PATH=/src/gs-head/backend/config/telemetry_config.json
export RUSTUP_TOOLCHAIN=1.99.0 CARGO_TARGET_DIR=/tmp/cargo-installm1g2FE CARGO_INCREMENTAL=0 CARGO_BUILD_JOBS=@CPUS@
cd /src/gs-head
# Resolve the builder's cached lock offline for this source (Cargo.lock is untracked in GS).
cargo metadata --offline --format-version 1 > /dev/null
cp Cargo.lock /output/gs/Cargo.lock.used
cargo tree --locked --offline -p SEDSnet --depth 0 > /output/gs/groundstation-sedsnet-source.txt
cargo build --locked --offline --release -p groundstation_backend --features firmware_sim
cp "$CARGO_TARGET_DIR/release/groundstation_backend" /output/gs/
echo GS_DONE
cp -a /inputs/frontend /src/frontend-head; cp /src/frontend/Cargo.lock /src/frontend-head/Cargo.lock
cp -a /src/frontend/.ci-tools /src/frontend-head/.ci-tools; cp -a /src/frontend/ci-results /src/frontend-head/ci-results
ln -sf /src/frontend-head/.ci-tools/binaryen/bin/wasm-opt /usr/local/bin/wasm-opt
export CARGO_NET_OFFLINE=true; cd /src/frontend-head
/build-python/bin/python scripts/ci/frontend.py build --target web --profile release
rm -rf /output/frontend/frontend-public; cp -a dist/public /output/frontend/frontend-public
echo FRONTEND_DONE
'''


def gs(args):
    work = args.work / 'gs'
    for sub in ('inputs', 'output/gs', 'output/frontend', 'scripts'):
        (work / sub).mkdir(parents=True, exist_ok=True)
    snapshot(args.gs, work / 'inputs/gs')
    snapshot(args.ui, work / 'inputs/frontend')
    (work / 'scripts/compile.sh').write_text(COMPILE.replace('@CPUS@', str(args.cpus)))
    subprocess.run(['chmod', '-R', 'a+rwX', str(work / 'output')])
    sh(['chmod', '-R', 'a+rX', work / 'inputs', work / 'scripts'])
    log = work / 'build.log'
    log.unlink(missing_ok=True)
    sh(['docker', 'run', '--rm', '--name', f'seds-flight-sim-gsbuild-{os.getpid()}', '--cpus', str(args.cpus), '--memory', '12g',
        '--network', 'none', '--user', '0:0', '-v', f'{work}/inputs:/inputs:ro', '-v', f'{work}/scripts:/b:ro',
        '-v', f'{DEFAULTS["cargo_extra"]}:/extra:ro', '-v', f'{work}/output:/output', '--entrypoint', 'bash', DEFAULTS['builder'], '/b/compile.sh'], log=log)
    meta = {'gs_head': head(args.gs), 'gs_dirty': dirty(args.gs), 'ui_head': head(args.ui), 'ui_dirty': dirty(args.ui),
            'features': 'firmware_sim', 'gs_binary_sha256': sha(work / 'output/gs/groundstation_backend'), 'built_at': time.time()}
    (work / 'output/SOURCE.json').write_text(json.dumps(meta, indent=2) + '\n')


def tile_xy(lat, lon, z):
    n = 2 ** z
    x = int((lon + 180) / 360 * n)
    y = int((1 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2 * n)
    return x, y


def tiles(args):
    from PIL import Image, ImageDraw
    root = args.work / 'tiles/north_america/tiles'
    if root.exists():
        shutil.rmtree(root)
    count = 0
    for z in range(8, 17):
        cx, cy = tile_xy(*LAUNCH_SITE, z)
        radius = 1 if z < 15 else 2
        for x in range(cx - radius, cx + radius + 1):
            for y in range(cy - radius, cy + radius + 1):
                image = Image.new('RGB', (256, 256), (206, 196, 168))
                draw = ImageDraw.Draw(image)
                for i in range(0, 256, 32):
                    draw.line([(i, 0), (i, 255)], fill=(186, 176, 148))
                    draw.line([(0, i), (255, i)], fill=(186, 176, 148))
                draw.rectangle([0, 0, 255, 255], outline=(150, 140, 110))
                draw.text((8, 8), f'placeholder z{z} {x}/{y}', fill=(90, 80, 60))
                path = root / str(z) / str(x) / f'{y}.jpg'
                path.parent.mkdir(parents=True, exist_ok=True)
                image.save(path, 'JPEG', quality=70)
                count += 1
    (args.work / 'tiles/README.txt').write_text(f'{count} synthetic placeholder JPEG tiles (no real imagery) around {LAUNCH_SITE}, z8-16.\n')
    print(f'{count} tiles', flush=True)


DOCKERFILE = '''FROM {base}
USER root
RUN rm -rf /opt/groundstation/frontend/dist/public /opt/groundstation/backend/layout /opt/groundstation/backend/config /opt/groundstation/backend/comms /opt/firmware-sim/renode
COPY gs/output/gs/groundstation_backend /usr/local/bin/groundstation_backend
COPY firmware-sim /usr/local/bin/firmware-sim
COPY gs/output/frontend/frontend-public /opt/groundstation/frontend/dist/public
COPY gs/inputs/gs/backend/layout /opt/groundstation/backend/layout
COPY gs/inputs/gs/backend/config /opt/groundstation/backend/config
COPY gs/inputs/gs/backend/comms /opt/groundstation/backend/comms
COPY tiles/north_america /opt/groundstation/map-seed/north_america
COPY renode /opt/firmware-sim/renode
USER 10001:10001
LABEL org.seds.flight-sim.gs-binary-sha256="{gs_sha}" org.seds.flight-sim.sim-revision="{sim_rev}" org.seds.flight-sim.gs-head="{gs_head}" org.seds.flight-sim.ui-head="{ui_head}"
'''


def image(args):
    ctx = args.work / 'image-context'
    if ctx.exists():
        shutil.rmtree(ctx)
    ctx.mkdir(parents=True)
    shutil.copytree(args.work / 'gs/output', ctx / 'gs/output')
    for sub in ('layout', 'config', 'comms'):
        shutil.copytree(args.work / 'gs/inputs/gs/backend' / sub, ctx / 'gs/inputs/gs/backend' / sub)
    shutil.copytree(args.work / 'tiles/north_america', ctx / 'tiles/north_america')
    shutil.copytree(SIM_ROOT / 'renode', ctx / 'renode')
    shutil.copy2(DEFAULTS['firmware_sim'], ctx / 'firmware-sim')
    meta = json.loads((args.work / 'gs/output/SOURCE.json').read_text())
    sim_rev = head(SIM_ROOT)
    (ctx / 'Dockerfile').write_text(DOCKERFILE.format(base=DEFAULTS['base_image'], gs_sha=meta['gs_binary_sha256'], sim_rev=sim_rev, gs_head=meta['gs_head'], ui_head=meta['ui_head']))
    tag = args.tag
    sh(['docker', 'build', '-q', '-t', tag, str(ctx)], log=args.work / 'image-build.log')
    image_id = subprocess.check_output(['docker', 'image', 'inspect', tag, '--format', '{{.Id}}'], text=True).strip()
    (args.work / 'IMAGE-ID').write_text(image_id + '\n')
    (args.work / 'IMAGE.json').write_text(json.dumps({'tag': tag, 'id': image_id, 'sim_revision': sim_rev, 'renode_models_dirty': [p for p in dirty(SIM_ROOT) if 'renode/' in p], **meta}, indent=2) + '\n')
    print(image_id)


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('steps', nargs='+', choices=['firmware', 'gs', 'tiles', 'image'])
    p.add_argument('--work', type=Path, default=WP / 'states-fc-sim-20261108/flight-sim-build')
    p.add_argument('--cpus', type=int, default=3)
    p.add_argument('--boards', nargs='+', choices=list(BOARDS), default=list(BOARDS))
    p.add_argument('--tag', default='seds-flight-sim:latest')
    for key in ('fc', 'gs', 'ui', 'rf', 'pb', 'fill_cohort'):
        p.add_argument('--' + key.replace('_', '-'), type=Path, default=DEFAULTS[key])
    args = p.parse_args()
    args.work = args.work.resolve()
    args.work.mkdir(parents=True, exist_ok=True)
    for step in args.steps:
        globals()[step](args)


if __name__ == '__main__':
    main()
