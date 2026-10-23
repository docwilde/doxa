#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Offline safety cases; fixtures are not upstream build or live-CI evidence."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import urllib.error
from unittest.mock import patch

import publish
import update
import verify


class UpdateTests(unittest.TestCase):
    @staticmethod
    def tags(*names):
        return "".join("a"*40 + "\trefs/tags/" + name + "\n" for name in names).encode()

    def test_more_than_1000_historical_prereleases_and_current_pin_need_no_release_api(self):
        raw = self.tags("rust-v0.163.0", *[f"rust-v0.164.0-alpha.{n}" for n in range(1501)])
        with tempfile.TemporaryDirectory() as directory:
            candidate = Path(directory) / "candidate"
            with patch.object(update, "baseline", return_value={"version":"0.163.0"}), \
                    patch.object(update, "remote_tags", return_value=raw), patch.object(update, "api") as api:
                self.assertEqual({"action":"none"}, update.prepare(Path(directory), candidate))
                api.assert_not_called()
                self.assertFalse(candidate.exists())

    def test_discovery_skips_unpublished_tags_drafts_prereleases_and_selects_oldest_eligible(self):
        names = ["rust-v0.168.0", "rust-v0.166.0", "rust-v0.165.0", "rust-v0.164.0", "rust-v0.167.0"]
        replies = [urllib.error.HTTPError("fixture",404,"not released",{},None),
                   dict(tag_name="rust-v0.165.0",draft=True,prerelease=False),
                   dict(tag_name="rust-v0.166.0",draft=False,prerelease=True),
                   dict(tag_name="rust-v0.167.0",draft=False,prerelease=False)]
        with patch.object(update,"remote_tags",return_value=self.tags(*names)), \
                patch.object(update,"api",side_effect=replies) as api:
            self.assertEqual("rust-v0.167.0",update.discover("0.163.0"))
            self.assertEqual([update.UPSTREAM+"/releases/tags/"+tag for tag in sorted(names)[0:4]],
                             [call.args[0] for call in api.call_args_list])

    def test_multiple_published_releases_choose_oldest_without_creation_date_order(self):
        def published(url):
            return dict(tag_name=url.rsplit("/",1)[1],draft=False,prerelease=False,
                        created_at="2099-01-01T00:00:00Z")
        with patch.object(update,"remote_tags",return_value=self.tags("rust-v0.170.0","rust-v0.164.0","rust-v0.165.0")), \
                patch.object(update,"api",side_effect=published) as api:
            self.assertEqual("rust-v0.164.0",update.discover("0.163.0"))
            self.assertEqual(1,api.call_count)

    def test_unpublished_stable_tags_alone_do_not_propose_an_update(self):
        with patch.object(update,"remote_tags",return_value=self.tags("rust-v0.164.0")), \
                patch.object(update,"api",side_effect=urllib.error.HTTPError("fixture",404,"absent",{},None)):
            self.assertIsNone(update.discover("0.163.0"))

    def test_tag_parser_ignores_nonstable_and_rejects_bad_duplicate_or_oversize_inventory(self):
        raw=self.tags("rust-v0.164.0-rc.1","sdk-v9.0.0","unrelated-über","rust-v0.0164.0","rust-v0.164.0","rust-v0.163.0")
        self.assertEqual(["rust-v0.164.0"],update.newer_tags(raw,"0.163.0"))
        for invalid in (b"not-a-ref\n",self.tags("rust-v0.164.0","rust-v0.164.0")):
            with self.assertRaises(ValueError):
                update.newer_tags(invalid,"0.163.0")
        with patch.object(update,"MAX_TAG_BYTES",8), self.assertRaises(ValueError):
            update.newer_tags(raw,"0.163.0")
        with patch.object(update,"MAX_REMOTE_TAGS",1), self.assertRaises(ValueError):
            update.newer_tags(raw,"0.163.0")

    def test_candidate_check_limit_and_metadata_or_api_errors_refuse_without_skip(self):
        names=["rust-v0.164.0","rust-v0.165.0"]
        with patch.object(update,"remote_tags",return_value=self.tags(*names)), \
                patch.object(update,"MAX_RELEASE_CANDIDATES",1), \
                patch.object(update,"api",side_effect=urllib.error.HTTPError("fixture",404,"absent",{},None)) as api:
            with self.assertRaises(ValueError):
                update.discover("0.163.0")
            self.assertEqual(1,api.call_count)
        with patch.object(update,"remote_tags",return_value=self.tags(names[0])), \
                patch.object(update,"api",return_value=dict(tag_name=names[1],draft=False,prerelease=False)):
            with self.assertRaises(ValueError):
                update.discover("0.163.0")
        with patch.object(update,"remote_tags",return_value=self.tags(names[0])), \
                patch.object(update,"api",side_effect=urllib.error.HTTPError("fixture",403,"refused",{},None)):
            with self.assertRaises(urllib.error.HTTPError):
                update.discover("0.163.0")

    def test_remote_inventory_uses_fixed_official_repo_and_stops_before_byte_overflow(self):
        real_popen=subprocess.Popen
        def fixture(args,**kw):
            self.assertEqual(["git","ls-remote","--refs","--tags",update.UPSTREAM_GIT],args)
            return real_popen([sys.executable,"-c","import sys; sys.stdout.buffer.write(b'x'*65536)"],**kw)
        with patch.object(update.subprocess,"Popen",side_effect=fixture), \
                patch.object(update,"MAX_TAG_BYTES",32):
            with self.assertRaises(ValueError):
                update.remote_tags()

    def test_discovery_only_published_stable_and_does_not_skip_versions(self):
        def release(tag, **kw):
            return dict(tag_name=tag, draft=False, prerelease=False, **kw)
        rows = [release("rust-v0.165.0"), release("rust-v0.164.0"),
                {"tag_name": "rust-v0.166.0", "draft": False, "prerelease": True},
                release("rust-v0.164.1-rc.1"), release("sdk-v9.0.0")]
        self.assertEqual("rust-v0.164.0", update.choose(rows, "0.163.0")["tag_name"])
        self.assertIsNone(update.choose(rows, "0.165.0"))

    def test_annotated_source_is_peeled_and_malformed_ref_refuses(self):
        released = dict(tag_name="rust-v0.164.0", draft=False, prerelease=False)
        replies = [released, {"object": {"type": "tag", "sha": "a"*40}},
                   {"object": {"type": "commit", "sha": "b"*40}}]
        with patch.object(update, "api", side_effect=replies):
            self.assertEqual("b"*40, update.official_release("rust-v0.164.0")[1])
        with self.assertRaises(ValueError):
            update.official_release("rust-v0.164.0-rc.1")
        with patch.object(update, "api", side_effect=[released, {"object": {"type": "tree", "sha": "a"*40}}]):
            with self.assertRaises(ValueError):
                update.official_release("rust-v0.164.0")

    def test_version_carry_changes_metadata_only(self):
        raw = (b'+version = "0.163.0"\n'
               b'+let agent = format!("doxa_codex_rs/0.163.0 {}", get_codex_user_agent());\n'
               b'+if !approved { return Stopped; }\n')
        revised = update.patch_version(raw, "0.163.0", "0.164.0")
        self.assertIn(b'+if !approved { return Stopped; }\n', revised)
        self.assertNotIn(b'0.163.0', revised)
        with self.assertRaises(ValueError):
            update.patch_version(raw+b'+if version == "0.163.0" { allow(); }\n', "0.163.0", "0.164.0")
        formatted=(b'+version = "0.163.0"\n'
                   b'+            "doxa_codex_rs/0.163.0 (doxa-precompact-fail-closed-v1; doxa-midturn-auto-v1; {})",\n')
        self.assertIn(b'doxa_codex_rs/0.164.0',update.patch_version(formatted,'0.163.0','0.164.0'))

    def test_duplicate_candidate_json_is_rejected(self):
        with self.assertRaises(ValueError):
            update.load('{"files":{},"files":{"evil":"yes"}}')

    def test_symlink_candidate_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root/'target').write_text('{}')
            (root/'candidate').symlink_to('target')
            with self.assertRaises(OSError):
                update.read(root/'candidate')

    def test_unsupported_version_requires_maintenance(self):
        self.assertEqual('2.0.0-beta.65', update.next_version('2.0.0-beta.64'))
        self.assertEqual('2.1.4', update.next_version('2.1.3'))
        for value in ('2.0.0-alpha.1','main','2.0.0-beta.64+unknown'):
            with self.assertRaises(ValueError):
                update.next_version(value)

    def test_artifact_patch_cannot_define_its_own_authority(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            supplied=root/'incoming'; supplied.mkdir()
            expected=dict(pin={'tag':'rust-v0.164.0'},release_date='2026-10-20',files={'a.py':'safe'})
            tampered=dict(expected,files={'a.py':'evil'})
            (supplied/'candidate.json').write_bytes(update.encode(tampered))
            with patch.object(update,'verify_gate_receipt',return_value=b'fixture gate'), patch.object(update,'prepare',return_value=expected), patch.object(update,'materialize') as materialize:
                with self.assertRaises(ValueError):
                    update.validate_artifact(root,supplied,root/'reconstructed')
                materialize.assert_not_called()

    def test_correct_artifact_is_materialized_only_after_byte_comparison(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); incoming=root/'incoming'; recreated=root/'recreated'
            raw=b'approved fixture\n'
            metadata=dict(pin={'tag':'rust-v0.164.0'},release_date='2026-10-20',files={'a.py':update.digest(raw)})
            for place in (incoming,recreated):
                (place/'files').mkdir(parents=True)
                (place/'files/a.py').write_bytes(raw)
            (incoming/'candidate.json').write_bytes(update.encode(metadata))
            with patch.object(update,'verify_gate_receipt',return_value=b'fixture gate'), patch.object(update,'prepare',return_value=metadata), patch.object(update,'materialize',return_value='ok') as apply:
                self.assertEqual('ok',update.validate_artifact(root,incoming,recreated))
                apply.assert_called_once_with(root,recreated)
            (incoming/'files/a.py').write_bytes(b'tampered same name')
            with patch.object(update,'verify_gate_receipt',return_value=b'fixture gate'), patch.object(update,'prepare',return_value=metadata), patch.object(update,'materialize') as apply:
                with self.assertRaises(ValueError):
                    update.validate_artifact(root,incoming,recreated)
                apply.assert_not_called()

    def test_pin_rotation_and_package_release_keep_legacy_and_test_fixtures(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            files={
                update.RETAINED_CONTRACTS: '{"schema":1,"contract":"doxa-precompact-fail-closed-v1","contracts":[]}\n',
                'scripts/install_codex_protected.py': 'SOURCE = "'+('a'*40)+'"\nPREVIOUS_SOURCE = "'+('b'*40)+'"\nPREVIOUS_PROVIDER = "codex-0.162.0-precompact-v1"\nPREVIOUS_PATCHES = {"'+('c'*64)+'"}\nMAX_REVIEWED_INSTALLATIONS = 256\nREVIEWED_INSTALLATIONS = ()\n',
                'rust/doxa-tui/Cargo.toml': '[package]\nname = "doxa-tui"\nversion = "2.0.0-beta.64"\n',
                'rust/doxa-tui/src/launch.rs': 'legacy = "codex-0.163.0-precompact-v1";\n',
                'scripts/codex-update/test_update.py': 'synthetic_version = "0.163.0"\n',
                'Cargo.lock': 'version = 4\n\n[[package]]\nname = "doxa-tui"\nversion = "2.0.0-beta.64"\n',
                'CHANGELOG.md': '# Changelog\n\n## 2.0.0-beta.64 — 2026-10-20\n\nOld.\n',
            }
            for name,raw in files.items():
                p=root/name; p.parent.mkdir(parents=True,exist_ok=True); p.write_text(raw)
            subprocess.run(['git','init','-q',str(root)],check=True)
            subprocess.run(['git','-C',str(root),'add','.'],check=True)
            old={'source_commit':'a'*40,'patch_sha256':'d'*64,'version':'0.163.0','provider':'codex-0.163.0-precompact-v1','contract':'doxa-precompact-fail-closed-v1'}
            new={'source_commit':'e'*40,'patch_sha256':'f'*64,'version':'0.164.0','provider':'codex-0.164.0-precompact-v1'}
            revised,version,notes=update.replacement_files(root,old,new,b'carried fixture','2026-10-20')
            self.assertEqual('2.0.0-beta.65',version)
            installer=revised['scripts/install_codex_protected.py'].decode()
            self.assertIn("PREVIOUS_SOURCE = '"+'a'*40+"'",installer)
            self.assertIn("PREVIOUS_PROVIDER = 'codex-0.163.0-precompact-v1'",installer)
            self.assertIn(repr(('b'*40,'c'*64,'codex-0.162.0-precompact-v1')),installer)
            self.assertIn(repr(('a'*40,'d'*64,'codex-0.163.0-precompact-v1')),installer)
            self.assertIn('e'*40,installer)
            self.assertEqual([{'version':'0.163.0','source_commit':'a'*40,'patch_sha256':'d'*64}],update.load(revised[update.RETAINED_CONTRACTS])["contracts"])
            self.assertNotIn('rust/doxa-tui/src/launch.rs',revised)
            self.assertNotIn('scripts/codex-update/test_update.py',revised)
            self.assertIn(b'2.0.0-beta.65',revised['Cargo.lock'])
            self.assertTrue(notes.startswith('## 2.0.0-beta.65 — 2026-10-20\n'))

    def test_installer_history_never_drops_entries_at_capacity_or_accepts_conflicting_triples(self):
        old={'source_commit':'a'*40,'patch_sha256':'b'*64,'provider':'codex-0.163.0-precompact-v1'}
        prefix=('MAX_REVIEWED_INSTALLATIONS = 256\nPREVIOUS_SOURCE = "'+'c'*40+'"\n'
                'PREVIOUS_PROVIDER = "codex-0.162.0-precompact-v1"\nPREVIOUS_PATCHES = {"'+'d'*64+'"}\n')
        history=tuple((f'{i:040x}',f'{i:064x}','codex-0.161.0-precompact-v1') for i in range(256))
        with self.assertRaisesRegex(ValueError,'full'):
            update.retained_installations((prefix+'REVIEWED_INSTALLATIONS = '+repr(history)).encode(),old)
        conflict=((old['source_commit'],old['patch_sha256'],'codex-0.160.0-precompact-v1'),)
        with self.assertRaisesRegex(ValueError,'conflicting'):
            update.retained_installations((prefix+'REVIEWED_INSTALLATIONS = '+repr(conflict)).encode(),old)

    def test_scoped_pin_rewrite_preserves_prefix_and_historical_markers(self):
        old={'version':'0.163.0','tag':'rust-v0.163.0','provider':'codex-0.163.0-precompact-v1','source_commit':'a'*40,'patch_sha256':'b'*64}
        new=dict(old,version='0.164.0',tag='rust-v0.164.0',provider='codex-0.164.0-precompact-v1',patch_sha256='c'*64)
        raw=(b'SOURCE_TAG = "rust-v0.163.0"\n'
             b'PATCH_SHA256 = "'+b'b'*64+b'"\n'
             b'AGENT_PREFIX = "doxa_codex_rs/0.163.01 (prefix fixture)"\n'
             b'HISTORICAL_RELEASE = "0.163.0"\n'
             b'# SOURCE_TAG = "rust-v0.163.0" historical observation\n')
        revised=update.rewrite_pins('scripts/install_codex_protected.py',raw,old,new)
        self.assertIn(b'SOURCE_TAG = "rust-v0.164.0"',revised)
        self.assertIn(b'PATCH_SHA256 = "'+b'c'*64+b'"',revised)
        self.assertIn(b'doxa_codex_rs/0.163.01',revised)
        self.assertIn(b'HISTORICAL_RELEASE = "0.163.0"',revised)
        self.assertIn(b'# SOURCE_TAG = "rust-v0.163.0" historical observation',revised)
        self.assertEqual(raw,update.rewrite_pins('tests/fixtures/historical_provider.py',raw,old,new))

    def test_contract_rotation_changes_only_current_identity_and_retains_reviewed_resume_tuple(self):
        old={'version':'0.164.0','source_commit':'a'*40,'patch_sha256':'b'*64}
        new={'version':'0.165.0','source_commit':'c'*40,'patch_sha256':'d'*64}
        raw=(f'pub const SUPPORTED_VERSION: &str = "{old["version"]}";\n'
             f'pub const SUPPORTED_SOURCE: &str = "{old["source_commit"]}";\n'
             f'pub const SUPPORTED_PATCH: &str = "{old["patch_sha256"]}";\n'
             'const RETAINED_VERSION: &str = "0.163.0";\n'
             'const RETAINED_SOURCE: &str = "83086188ae0614bd882818ff1aa23f3194962012";\n'
             'const RETAINED_PATCH: &str = "3a7b8c09b1f95bc523611202005aa71e6ba14d23f26a3833249487385e013e9c";\n').encode()
        revised=update.rewrite_pins('rust/doxa-engines/src/codex_compact.rs',raw,old,new)
        for field in ('version','source_commit','patch_sha256'):
            self.assertIn(new[field].encode(),revised)
            self.assertNotIn(old[field].encode(),revised)
        self.assertEqual(raw.split(b'const RETAINED_VERSION:')[1],revised.split(b'const RETAINED_VERSION:')[1])

    def test_two_successive_updates_preserve_all_previously_current_contracts(self):
        raw=update.read(update.ROOT/update.RETAINED_CONTRACTS)
        original=update.load(raw)["contracts"]
        old=update.baseline(update.ROOT)
        first=update.retained_contracts(raw,old)
        next_pin=dict(old,version="0.165.0",source_commit="c"*40,patch_sha256="d"*64)
        second=update.retained_contracts(first,next_pin)
        rows={row["version"]:row for row in update.load(second)["contracts"]}
        for identity in original+[old,next_pin]:
            self.assertEqual({key:identity[key] for key in ("version","source_commit","patch_sha256")},rows[identity["version"]])
        self.assertIn("0.163.0",rows);self.assertIn("0.164.0",rows);self.assertIn("0.165.0",rows)
        self.assertEqual(second,update.retained_contracts(second,next_pin))

    def test_contract_history_rejects_conflicts_unknown_fields_duplicates_and_overflow(self):
        old=update.baseline(update.ROOT)
        raw=update.retained_contracts(update.read(update.ROOT/update.RETAINED_CONTRACTS),old)
        valid=update.load(raw)
        conflict=dict(old,patch_sha256="f"*64)
        with self.assertRaisesRegex(ValueError,"conflicts"):
            update.retained_contracts(raw,conflict)
        for damage in ([],dict(valid,contracts=valid["contracts"]*2),dict(valid,unreviewed=True),
                       dict(valid,contracts=[{"version":"0.1.0","source_commit":"a"*40,"patch_sha256":"b"*64,"extra":True}])):
            with self.assertRaises(ValueError):
                update.retained_contracts(update.encode(damage),old)
        full=dict(valid,contracts=[{"version":f"0.1.{index}","source_commit":"a"*40,"patch_sha256":"b"*64} for index in range(256)])
        with self.assertRaisesRegex(ValueError,"full"):
            update.retained_contracts(update.encode(full),old)

    def test_new_or_changed_upstream_safety_paths_require_semantic_review(self):
        rows=['M\tcodex-rs/core/src/session/brand_new_inference.rs',
              'A\tcodex-rs/new-module/src/innocent_name.rs',
              'M\tcodex-rs/hooks/src/events/compact.rs','M\tREADME.md']
        self.assertEqual(sorted(row.split('\t')[1] for row in rows[:3]),update.sensitive_changes(rows))
        self.assertEqual([],update.sensitive_changes(['M\tREADME.md','M\tcodex-rs/tui/src/colors.rs']))

    def test_guard_crates_inventory_and_inherited_memory_scope(self):
        mapping=dict((name,package) for package,name in verify.GUARD_TESTS)
        self.assertEqual('codex-hooks',mapping['doxa_required_hook_rejects_infrastructure_and_invalid_output'])
        for name,package in mapping.items():
            command=verify.guard_command('cargo',package,name)
            self.assertEqual(package,command[command.index('-p')+1])
            self.assertEqual([name],verify.checked_inventory(name+': test\n',name))
            with self.assertRaises(ValueError):
                verify.checked_inventory('0 tests, 0 benchmarks\n',name)
        prefix=['systemd-run','--user','--scope','-q','-p','MemoryMax=8G','-p','MemorySwapMax=0']
        self.assertEqual(['cargo','build'],verify.inherited_scope(prefix+['cargo','build']))
        with self.assertRaises(ValueError):
            verify.inherited_scope(['systemd-run','--scope','cargo','build'])
        with self.assertRaises(ValueError):
            verify.inherited_scope([part.replace('MemoryMax=8G','MemoryMax=12G') for part in prefix]+['cargo','build'])

    def test_exact_candidate_receipt_refuses_swap_at_same_base_and_job(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            pin={'source_commit':'a'*40,'patch_sha256':'b'*64}
            meta={'pin':pin,'base_sha':'c'*40,'verification_run_id':'100','files':{}}
            raw=update.encode(meta); (root/'candidate.json').write_bytes(raw)
            proof=dict(schema=1,compiled_gates='passed',paid_requests=0,candidate_sha256=update.digest(raw),
                pin_sha256=update.digest(update.encode(pin)),source_commit=pin['source_commit'],patch_sha256=pin['patch_sha256'],
                base_sha=meta['base_sha'],verification_run_id='100',memory_max_bytes=8*1024**3,memory_swap_max_bytes=0,
                artifacts=dict(pin))
            proven=update.encode(proof); (root/'compiled-gates.json').write_bytes(proven)
            self.assertEqual(proven,update.verify_gate_receipt(root,meta,update.digest(raw),update.digest(proven)))
            changed=dict(meta,pin=dict(pin,source_commit='d'*40))
            changed_raw=update.encode(changed); (root/'candidate.json').write_bytes(changed_raw)
            with self.assertRaises(ValueError):
                update.verify_gate_receipt(root,changed,update.digest(changed_raw),update.digest(proven))

    def test_candidate_source_change_is_detected_after_compiled_work(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); candidate=root/'candidate'; (candidate/'files').mkdir(parents=True)
            pin={'source_commit':'a'*40,'patch_sha256':'b'*64}
            metadata={'pin':pin,'base_sha':'c'*40,'verification_run_id':'100','files':{'payload.py':update.digest(b'before')}}
            (candidate/'candidate.json').write_bytes(update.encode(metadata))
            (root/'payload.py').write_bytes(b'before'); (candidate/'files/payload.py').write_bytes(b'before')
            with patch.object(update,'baseline',return_value=pin):
                verify.candidate_binding(root,candidate)
                (root/'payload.py').write_bytes(b'changed after tests')
                with self.assertRaises(ValueError):
                    verify.candidate_binding(root,candidate)

    def test_whitespace_exception_covers_only_the_literal_patch(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); patchfile=root/'scripts/codex-protected/precompact.patch'
            patchfile.parent.mkdir(parents=True); patchfile.write_text('baseline\n')
            other=root/'ordinary.py'; other.write_text('baseline\n')
            subprocess.run(['git','init','-q',str(root)],check=True)
            subprocess.run(['git','-C',str(root),'add','.'],check=True)
            patchfile.write_text('baseline\n \n')
            publish.whitespace_check(root)
            other.write_text('baseline\nbad \n')
            with self.assertRaises(subprocess.CalledProcessError):
                publish.whitespace_check(root)


class FakeGitHub:
    def __init__(self, values):
        self.values=values; self.writes=[]

    def call(self,path,data=None,method=None):
        if method:
            self.writes.append((path,data,method))
        value=self.values[path]
        if isinstance(value,Exception):
            raise value
        return value


class PublicationTests(unittest.TestCase):
    def test_own_pr_and_exact_head_required(self):
        row=dict(user={'login':'github-actions[bot]'},base={'ref':'main'},
                 head={'ref':'automation/codex-protected-0.164.0','repo':{'full_name':'docwilde/doxa'},'sha':'a'*40})
        publish.require_own_pr(row,'a'*40,row['head']['ref'])
        for key in ('sha','ref'):
            changed=json.loads(json.dumps(row)); changed['head'][key]='changed'
            with self.assertRaises(ValueError):
                publish.require_own_pr(changed,'a'*40,row['head']['ref'])

    def test_dispatch_success_does_not_satisfy_real_pr_gate(self):
        head='a'*40; branch='automation/codex-protected-0.164.0'
        row=dict(id=1,head_sha=head,head_branch=branch,event='workflow_dispatch',path='.github/workflows/rust-ci.yml',status='completed',conclusion='success')
        api=FakeGitHub({'/actions/runs?head_sha='+head+'&per_page=100':{'workflow_runs':[row]}})
        self.assertFalse(publish.real_pr_ci(api,head,branch,'.github/workflows/rust-ci.yml',{'rust','macos'}))
        self.assertEqual([],api.writes)

    def test_real_pr_gate_requires_all_platform_jobs(self):
        head='a'*40; branch='automation/codex-protected-0.164.0'
        row=dict(id=1,head_sha=head,head_branch=branch,event='pull_request',path='.github/workflows/rust-ci.yml',status='completed',conclusion='success')
        api=FakeGitHub({'/actions/runs?head_sha='+head+'&per_page=100':{'workflow_runs':[row]},
                       '/actions/runs/1/jobs?per_page=100':{'jobs':[{'name':'rust','conclusion':'success'}]}})
        with self.assertRaises(ValueError):
            publish.real_pr_ci(api,head,branch,'.github/workflows/rust-ci.yml',{'rust','macos'})
        api.values['/actions/runs/1/jobs?per_page=100']['jobs'].append({'name':'macos','conclusion':'success'})
        self.assertTrue(publish.real_pr_ci(api,head,branch,'.github/workflows/rust-ci.yml',{'rust','macos'}))

    def test_missing_guard_job_and_wrong_workflow_cannot_claim_compiled_proof(self):
        meta={'verification_run_id':'12','base_sha':'a'*40}
        api=FakeGitHub({'/actions/runs/12':{'head_sha':'a'*40,'path':'.github/workflows/codex-update.yml','event':'schedule'},
                       '/actions/runs/12/jobs?per_page=100':{'jobs':[{'name':'Verify protected candidate','conclusion':'failure'}]}})
        with self.assertRaises(ValueError):
            publish.require_compiled_receipt(api,meta)
        api.values['/actions/runs/12/jobs?per_page=100']['jobs'][0]['conclusion']='success'
        publish.require_compiled_receipt(api,meta)
        api.values['/actions/runs/12']['path']='.github/workflows/other.yml'
        with self.assertRaises(ValueError):
            publish.require_compiled_receipt(api,meta)

    def test_ci_approval_refusal_is_a_block_without_other_writes(self):
        head='a'*40; branch='automation/codex-protected-0.164.0'
        row=dict(id=9,head_sha=head,head_branch=branch,event='pull_request',path='.github/workflows/rust-ci.yml',status='completed',conclusion='action_required')
        api=FakeGitHub({'/actions/runs?head_sha='+head+'&per_page=100':{'workflow_runs':[row]},
                       '/actions/runs/9/approve':PermissionError('platform approval refused')})
        with self.assertRaises(PermissionError):
            publish.real_pr_ci(api,head,branch,'.github/workflows/rust-ci.yml',{'rust','macos'})
        self.assertEqual(['/actions/runs/9/approve'],[row[0] for row in api.writes])

    def test_missing_everyone_or_expected_app_protection_refuses(self):
        checks={'enforcement_level':'everyone','checks':[{'context':'rust','app_id':15368}]}
        policy={'protected':True,'protection':{'required_status_checks':checks}}
        api=FakeGitHub({'/branches/main':policy})
        publish.require_protection(api)
        checks['checks'][0]['app_id']=999
        with self.assertRaises(ValueError):
            publish.require_protection(api)
        checks['checks'][0]['app_id']=15368
        checks['enforcement_level']='non_admins'
        with self.assertRaises(ValueError):
            publish.require_protection(api)

    def test_wrong_tag_target_or_unannotated_tag_never_overwritten(self):
        for obj in ({'type':'commit','sha':'b'*40},{'type':'tag','sha':'c'*40}):
            api=FakeGitHub({'/git/ref/tags/v2.0.0-beta.65':{'object':obj},
                           '/git/tags/'+'c'*40:{'tag':'v2.0.0-beta.65','object':{'type':'commit','sha':'b'*40},'message':'wrong'}})
            with self.assertRaises(ValueError):
                publish.tag_release(api,'2.0.0-beta.65','a'*40,'notes')
            self.assertEqual([],api.writes)

    def test_existing_correct_tag_and_release_are_idempotent(self):
        version='2.0.0-beta.65'; title='DOXA '+version+' — protected Codex update'
        api=FakeGitHub({'/git/ref/tags/v'+version:{'object':{'type':'tag','sha':'c'*40}},
                       '/git/tags/'+'c'*40:{'tag':'v'+version,'object':{'type':'commit','sha':'a'*40},'message':title+'\n'},
                       '/releases/tags/v'+version:{'tag_name':'v'+version,'name':title,'body':'notes','draft':False,'prerelease':True}})
        publish.tag_release(api,version,'a'*40,'notes')
        self.assertEqual([],api.writes)


if __name__=='__main__':
    unittest.main()
