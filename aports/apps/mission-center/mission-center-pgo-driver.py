#!/usr/bin/env python3
"""Exercise build orchestration without compiling or requiring a display."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

HERE = Path(__file__).resolve().parent
DRIVER = HERE / 'mission-center-pgo-build.sh'
NVTOP = '3d4a953da02bc18886734613bb9f60ff80669de7.tar.gz'
# name, cargo package, installed file, training role
# Gatherer first: training the UI spawns a gatherer, so magpie has to be built
# and rewritten before the application is trained.
BINARIES = [('magpie', 'magpie', 'missioncenter-magpie', 'magpie'),
            ('missioncenter', 'missioncenter', 'missioncenter', 'ui')]
TOOLS = ['id', 'rustc', 'cargo', 'cargo-pgo', 'cargo-auditable', 'llvm-bolt', 'merge-fdata',
         'labwc', 'dbus-run-session', 'gdbus', 'timeout', 'python3']
MOCK = '''#!/usr/bin/env python3
import os, pathlib, subprocess, sys
p = pathlib.Path
args = sys.argv[1:]
tool = p(sys.argv[0]).name
if tool == 'id':
    print('1000')
elif tool == 'rustc':
    print('host: ' + os.environ['TEST_HOST'])
elif tool == 'cargo':
    if args == ['pgo', 'info']:
        sys.exit(0)
    flags = os.environ.get('RUSTFLAGS', '')
    assert '-Ctarget-cpu=generic' in flags, flags
    assert '-Csymbol-mangling-version=v0' in flags, flags
    assert flags.index('-Clink-arg=-Wl,-z,nopack-relative-relocs') > flags.index('-Clink-arg=-Wl,-z,pack-relative-relocs'), flags
    assert ('-Clink-arg=-Wl,-fini,0' in flags) == (os.environ['TEST_FINI_ARRAY'] == '1'), flags
    if args[:1] == ['pgo']:
        cargo_args = args[args.index('--') + 1:]
        subprocess.run(['cargo', 'build', *cargo_args], check=True)
        work = p(cargo_args[cargo_args.index('--target-dir') + 1])
        directory = work / ('bolt-profiles' if args[1] == 'bolt' else 'pgo-profiles')
        if args[1] != 'bolt' or args[2] == 'build':
            directory.mkdir(parents=True, exist_ok=True)
    else:
        assert args[:2] == ['auditable', 'build'], args
        work = p(args[args.index('--target-dir') + 1])
        name = args[args.index('--bin') + 1]
        target = args[args.index('--target') + 1]
        if name == 'magpie':
            # build.rs only stays offline when the vendored archive is staged
            # in its drop-in directory, which cargo scopes to the target triple.
            staged = work / target / 'release' / 'build' / 'native' / os.environ['TEST_NVTOP']
            assert staged.is_file(), staged
            assert staged.read_bytes(), staged
        for suffix in ['', '-bolt-instrumented', '-bolt-optimized']:
            binary = work / target / 'release' / (name + suffix)
            binary.parent.mkdir(parents=True, exist_ok=True)
            binary.write_text(name + suffix)
            binary.chmod(0o755)
elif tool == 'timeout':
    # timeout 300 sh <trainer> <binary> <role>
    assert args[0] == '300' and args[1] == 'sh', args
    binary = p(args[3])
    role = args[4]
    name = binary.name.removesuffix('-bolt-instrumented').removesuffix('-bolt-optimized')
    phase = 'pgo' if binary.name == name else ('bolt' if binary.name.endswith('-instrumented') else 'final')
    with open(os.environ['TEST_LOG'], 'a') as stream:
        stream.write(name + ':' + phase + ':' + role + '\\n')
    if os.environ.get('TEST_FAIL') == name + ':' + phase:
        sys.exit(1 if phase == 'final' else 0)
    if phase == 'pgo':
        profile = p(os.environ['LLVM_PROFILE_FILE'].replace('%m', '1').replace('%p', '2'))
    elif phase == 'bolt':
        profile = binary.parents[2] / 'bolt-profiles' / name / 'profile.1.fdata'
    else:
        sys.exit(0)
    profile.parent.mkdir(parents=True, exist_ok=True)
    profile.write_text('profile')
'''


class DriverTests(unittest.TestCase):
    def run_driver(self, arch='x86_64', failure='', target=None, abi='musl', env_extra=None,
                   cwd=None):
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch)
            tools = root / 'tools'
            tools.mkdir()
            for name in TOOLS:
                path = tools / name
                path.write_text(MOCK.replace('#!/usr/bin/env python3', '#!' + sys.executable, 1))
                path.chmod(0o755)
            # A fake Mission Center source root: the driver takes its own
            # location from $PWD, so it runs with this as the working directory.
            (root / 'Cargo.toml').write_text('[package]\nname = "missioncenter"\n')
            (root / 'subprojects' / 'magpie').mkdir(parents=True)
            (root / 'subprojects' / 'magpie' / 'Cargo.toml').write_text('[package]\nname = "magpie"\n')
            trainer = root / 'trainer.sh'
            trainer.write_text('#!/bin/sh\nexit 0\n')
            trainer.chmod(0o755)
            nvtop = root / NVTOP
            nvtop.write_bytes(b'not really a tarball')
            host = arch + '-test-linux-' + abi
            build = root / '_build'
            (build / 'src' / 'release').mkdir(parents=True)
            output = root / 'custom-target'
            for name, _, _, _ in BINARIES:
                for directory, filename in [('pgo-profiles', 'stale.profraw'),
                                             ('bolt-profiles/' + name, 'stale.fdata')]:
                    stale = output / 'pgo-bolt' / host / name / directory / filename
                    stale.parent.mkdir(parents=True, exist_ok=True)
                    stale.write_text('stale')
            log = root / 'training.log'
            env = dict(os.environ, PATH=str(tools) + ':' + os.environ['PATH'],
                       CARGO_TARGET_DIR=str(output), TEST_HOST=host, TEST_FAIL=failure,
                       TEST_LOG=str(log), TEST_NVTOP=NVTOP, MC_NVTOP_TARBALL=str(nvtop),
                       MC_PGO_TRAIN=str(trainer),
                       RUSTFLAGS='-Ctarget-cpu=generic -Clink-arg=-Wl,-z,pack-relative-relocs',
                       TEST_FINI_ARRAY=str(int(arch == 'aarch64' and abi == 'musl')))
            env.pop('CARGO_ENCODED_RUSTFLAGS', None)
            env.update(env_extra or {})
            result = subprocess.run(['sh', str(DRIVER), str(build), target or host],
                                    env=env, cwd=str(cwd or root), capture_output=True, text=True,
                                    timeout=60)
            published = {installed: (build / 'src' / 'release' / installed)
                         for _, _, installed, _ in BINARIES}
            if failure or target or env_extra or cwd is not None:
                self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                for path in published.values():
                    self.assertFalse(path.exists())
            else:
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                for name, _, installed, role in BINARIES:
                    self.assertEqual(published[installed].read_text(), name + '-bolt-optimized')
                    # Stale profiles must be gone before the new ones are written.
                    for stale in (output / 'pgo-bolt' / host / name).glob('*/stale.*'):
                        self.fail('stale profile kept: ' + str(stale))
                self.assertEqual(log.read_text().splitlines(),
                                 [name + ':' + phase + ':' + role
                                  for name, _, _, role in BINARIES
                                  for phase in ['pgo', 'bolt', 'final']])
            return result

    def test_native_architectures_and_custom_directory(self):
        for arch in ['x86_64', 'aarch64']:
            with self.subTest(arch=arch):
                self.run_driver(arch)

    def test_gnu_targets_keep_their_legacy_fini_hook(self):
        for arch in ['x86_64', 'aarch64']:
            with self.subTest(arch=arch):
                self.run_driver(arch, abi='gnu')

    def test_every_executable_requires_profiles_and_final_check(self):
        for name, _, _, _ in BINARIES:
            for phase in ['pgo', 'bolt', 'final']:
                with self.subTest(name=name, phase=phase):
                    result = self.run_driver(failure=name + ':' + phase)
                    if phase != 'final':
                        self.assertIn('no profiles for ' + name, result.stderr)

    def test_cross_target_rejected(self):
        self.assertIn('requires a native build',
                      self.run_driver(target='aarch64-test-linux-musl').stderr)

    def test_foreign_target_rejected(self):
        self.assertIn('requires an x86_64 or aarch64 Linux target',
                      self.run_driver(target='riscv64-test-linux-musl').stderr)

    def test_nvtop_tarball_is_required(self):
        for missing in [{'MC_NVTOP_TARBALL': ''}, {'MC_NVTOP_TARBALL': '/nonexistent'}]:
            with self.subTest(missing=missing):
                self.assertIn('MC_NVTOP_TARBALL',
                              self.run_driver(env_extra=missing).stderr)

    def test_nvtop_tarball_needs_the_upstream_basename(self):
        with tempfile.TemporaryDirectory() as empty:
            for name in ['nvtop.tar.gz', '3d4a953da02bc18886734613bb9f60ff80669de7.tgz',
                         '3d4a953da02bc18886734613bb9f60ff80669de7.tar.gz.noauto']:
                tarball = Path(empty) / name
                tarball.write_bytes(b'x')
                with self.subTest(name=name):
                    result = self.run_driver(env_extra={'MC_NVTOP_TARBALL': str(tarball)})
                    self.assertIn('40-hex-sha', result.stderr)

    def test_source_root_is_required(self):
        with tempfile.TemporaryDirectory() as empty:
            for message in ['Cargo.toml', 'subprojects/magpie/Cargo.toml']:
                with self.subTest(message=message):
                    root = Path(empty) / message
                    (root / 'subprojects').mkdir(parents=True)
                    if message == 'Cargo.toml':
                        (root / 'subprojects' / 'magpie').mkdir()
                        (root / 'subprojects' / 'magpie' / 'Cargo.toml').write_text('')
                    result = self.run_driver(cwd=root)
                    self.assertIn(message, result.stderr)


if __name__ == '__main__':
    unittest.main()
