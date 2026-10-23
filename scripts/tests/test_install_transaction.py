"""Outer installer rollback with actual protected publication and local build stand-ins."""
import importlib.util,json,os,subprocess,tempfile,textwrap,unittest
from pathlib import Path
TREE=Path(__file__).resolve().parents[2]

class InstallTransactionTests(unittest.TestCase):
    def exercise(self, failure, expected_sha=None):
        directory=tempfile.TemporaryDirectory(prefix='install-transaction-')
        self.addCleanup(directory.cleanup)
        root=Path(directory.name)
        root.chmod(0o700);tools=root/'tools';tools.mkdir();home=root/'doxa-home';home.mkdir(mode=0o700);bins=root/'bin';bins.mkdir(mode=0o700)
        spec=importlib.util.spec_from_file_location('installer',TREE/'scripts/install_codex_protected.py');module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        server=root/'server';server.write_text('#!/usr/bin/python3\nimport json,sys\nq=json.loads(sys.stdin.readline());print(json.dumps({"id":q["id"],"result":{"userAgent":'+repr(module.AGENT_PREFIX+'fixture)')+'}}),flush=True);sys.stdin.read()\n');server.chmod(0o700)
        launcher=root/'launcher';launcher.write_bytes(b'fixture dispatcher');launcher.chmod(0o700);helper=root/'helper';helper.write_bytes(b'fixture helper');helper.chmod(0o700)
        provider=home/'providers';result=module.install(server,provider,Path('/usr/bin/true'),launcher,helper)
        # Seed one complete reviewed historical tuple. Mixing today's source
        # with an older patch must fail before reaching either injected fault.
        old_source,old_patch,old_provider=next(identity for identity in module.REVIEWED_INSTALLATIONS
            if identity[1]==module.LEGACY_PATCH_SHA256)
        legacy=provider/old_provider;result.parent.rename(legacy);(provider/'codex-current').unlink()
        receipt=json.loads((legacy/'receipt.json').read_text())
        receipt.update(source_commit=old_source,patch_sha256=old_patch,code_mode_host_source_commit=old_source,toolchain='1.95.0')
        (legacy/'receipt.json').write_text(json.dumps(receipt))
        self.assertEqual(module.receipt_provider(receipt),old_provider)
        module.verify_installed(legacy,receipt)
        provider_before={path.name:path.read_bytes() for path in legacy.iterdir()}
        (bins/'doxa-rs').write_bytes(b'old frontend untouched')
        def tool(name,text):
            p=tools/name;p.write_text(textwrap.dedent(text));p.chmod(0o700)
        tool('git','''\
        #!/usr/bin/python3
        import os,sys,shutil
        from pathlib import Path
        args=sys.argv[1:];checkout=Path(args[1]);action=args[2]
        if action=='checkout':
         for rel in ['rust/doxa-tui/Cargo.toml','rust/doxa-daemon/Cargo.toml','rust/doxa-remote/Cargo.toml','rust/doxa-isolation/Cargo.toml','rust/doxa-router/Cargo.toml','Cargo.lock','scripts/install_codex_protected.py']:
          dest=checkout/rel;dest.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(Path(os.environ['AUDIT_TREE'])/rel,dest)
        if action=='rev-parse': print('6519076ccf02392e8cf04dfd0b5f3ba3a27b7016')
        ''')
        tool('rustc','#!/bin/sh\nprintf "host: audit-fixture\\n"\n')
        tool('codex','#!/bin/sh\nexit 0\n')
        tool('cargo','''\
        #!/usr/bin/python3
        import os
        from pathlib import Path
        Path(os.environ['AUDIT_CARGO_MARKER']).write_text('cargo ran')
        out=Path(os.environ['CARGO_TARGET_DIR'])/'audit-fixture/release';out.mkdir(parents=True,exist_ok=True)
        for name in ['doxa-rs','doxa-plugin-worker','doxa-daemon-rs','doxa-remote','doxa-isolation-worker','lore-rs','doxa-router-eval','doxa-codex-protected']:
         p=out/name;p.write_text('#!/bin/sh\\nexit 0\\n');p.chmod(0o700)
        ''')
        tool('python3','''\
        #!/usr/bin/python3
        import os,sys,importlib.util
        from pathlib import Path
        root=Path(os.environ['AUDIT_ROOT']);spec=importlib.util.spec_from_file_location('installer',Path(os.environ['AUDIT_TREE'])/'scripts/install_codex_protected.py');m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
        server=root/'server';helper=root/'helper';launcher=root/'launcher'
        verified={'source_commit':m.SOURCE,'patch_sha256':m.PATCH_SHA256,'profile':'dev-small','toolchain':'1.99.1','binary_sha256':m.digest(server),'code_mode_host_sha256':m.digest(helper)}
        if '--rollback' in sys.argv:
         m.rollback_publication(Path(sys.argv[sys.argv.index('--rollback-receipt')+1]))
        else:
         m.install(server,root/'doxa-home/providers',Path('/usr/bin/true'),launcher,helper,verified=verified,rollback_receipt=Path(sys.argv[sys.argv.index('--rollback-receipt')+1]))
        ''')
        tool('cp','''\
        #!/bin/sh
        case "$AUDIT_FAILURE:$*" in stage:*.doxa-install.*/doxa-rs) echo 'injected ENOSPC staging failure' >&2; exit 1;; esac
        exec /usr/bin/cp "$@"
        ''')
        cargo_marker=root/'cargo-called'
        env=dict(os.environ,TMPDIR=str(root),PATH=f'{tools}:/usr/bin:/bin',AUDIT_ROOT=str(root),AUDIT_TREE=str(TREE),AUDIT_CARGO_MARKER=str(cargo_marker),DOXA_HOME=str(home),DOXA_RUST_BIN_DIR=str(bins),DOXA_INSTALL_CACHE_DIR=str(root/'cache'),DOXA_INSTALL_CODEX_PROTECTED='1',DOXA_NO_LAUNCHER='1')
        if expected_sha is not None: env['DOXA_RUST_EXPECTED_SHA']=expected_sha
        tool('mv','''\
        #!/bin/sh
        case "$AUDIT_FAILURE:$*" in handoff:*.doxa-install.*/doxa-plugin-worker*) echo 'injected handoff failure' >&2; exit 1;; esac
        exec /usr/bin/mv "$@"
        ''')
        env['AUDIT_FAILURE']=failure
        before=os.readlink(provider/'codex-current') if (provider/'codex-current').is_symlink() else None
        run=subprocess.run(['/bin/sh',str(TREE/'scripts/install.sh'),'HEAD'],env=env,capture_output=True,text=True,timeout=15)
        self.assertNotEqual(run.returncode,0,run.stdout+run.stderr)
        if expected_sha is None:
            self.assertIn({'stage':'injected ENOSPC staging failure','handoff':'injected handoff failure'}[failure],run.stderr)
        else:
            self.assertIn('does not match DOXA_RUST_EXPECTED_SHA',run.stderr)
            self.assertFalse(cargo_marker.exists(),run.stdout+run.stderr)
        self.assertEqual((bins/'doxa-rs').read_bytes(),b'old frontend untouched')
        after=os.readlink(provider/'codex-current') if (provider/'codex-current').is_symlink() else None
        self.assertEqual(before,after,run.stdout+run.stderr)
        self.assertEqual(module.active_installation(provider),legacy)
        self.assertEqual(provider_before,{path.name:path.read_bytes() for path in legacy.iterdir()})
        self.assertFalse((bins/'doxa-plugin-worker').exists())
    def test_provider_rolls_back_after_staging_failure(self): self.exercise('stage')
    def test_provider_and_frontend_roll_back_after_handoff_failure(self): self.exercise('handoff')
    def test_caller_sha_mismatch_stops_before_cargo(self): self.exercise('none',expected_sha='0'*40)

if __name__=='__main__': unittest.main()
