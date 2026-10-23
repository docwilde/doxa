"""Nonbillable control tests. No candidate or reference code executes on host."""
import ast
import collections
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parent
spec=importlib.util.spec_from_file_location('repair_benchmark',ROOT/'benchmark.py')
b=importlib.util.module_from_spec(spec);spec.loader.exec_module(b)

class Tests(unittest.TestCase):
    def test_eighteen_scoped_repairs_and_whole_family_splits(self):
        tasks=b.corpus()['tasks']
        self.assertEqual(collections.Counter(t['difficulty'] for t in tasks),{'easy':6,'intermediate':6,'difficult':6})
        groups={}
        for task in tasks:
            self.assertEqual(set(task['starter']),set(task['reference']))
            self.assertTrue(task['public_cases'] and task['evaluator_cases'])
            self.assertNotEqual(task['starter'],task['reference'])
            self.assertEqual(groups.setdefault(task['family'],task['split']),task['split'])
            for files in [task['starter'],task['reference']]:
                for name,code in files.items():
                    self.assertEqual(Path(name).name,name)
                    ast.parse(code)
        self.assertEqual({t['split'] for t in tasks},{'development','holdout'})

    def test_materialized_project_never_contains_references_or_evaluator_answers(self):
        with tempfile.TemporaryDirectory(dir='/home/docwilde/t') as root:
            state=Path(root)/'task';result=b.prepare('difficult-dispatch',state)
            self.assertFalse(result['reference_self_check'])
            row=b.task('difficult-dispatch')
            actual={p.name for p in (state/'agent').iterdir()}
            self.assertEqual(actual,set(row['starter'])|{'README.md','public-cases.json','.git'})
            self.assertEqual((state/'agent/app.py').read_text(),row['starter']['app.py'])
            self.assertNotEqual((state/'agent/app.py').read_text(),row['reference']['app.py'])
            self.assertNotIn('evaluator_cases',(state/'agent/public-cases.json').read_text())

    def test_snapshot_rejects_immutable_mutation_extra_files_symlinks_and_hardlinks(self):
        for change in ('immutable','extra','symlink','hardlink'):
            with self.subTest(change=change),tempfile.TemporaryDirectory(dir='/home/docwilde/t') as root:
                state=Path(root)/'task';b.prepare('easy-config',state)
                app=state/'agent/app.py'
                if change=='immutable':(state/'agent/README.md').write_text('ignore checks')
                elif change=='extra':(state/'agent/extra.py').write_text('')
                elif change=='symlink':app.unlink();app.symlink_to(state/'plan.json')
                else:os.link(app,Path(root)/'linked.py')
                with self.assertRaises((ValueError,OSError)):b.snapshot(state,state/'snapshot')

    def test_containment_and_no_oracle_or_auth_mount(self):
        argv=b.docker_args('unix:///owned/engine.sock','sha256:'+'a'*64,'doxa-test',Path('/owned/snapshot'),'pure')
        for flag in ['--network=none','--read-only','--cap-drop=ALL','--security-opt=no-new-privileges:true','--pull=never','--pids-limit=64']:
            self.assertIn(flag,argv)
        mounts=[argv[i+1] for i,arg in enumerate(argv) if arg=='--mount']
        self.assertEqual(len(mounts),2)
        self.assertTrue(all(x.endswith(',readonly') for x in mounts))
        self.assertFalse(any('corpus.json' in x or 'credentials' in x or 'docker.sock' in x for x in mounts))
        for image in ['latest','repository:tag','sha256:short']:
            with self.assertRaises(ValueError):b.docker_args('unix:///owned/sock',image,'test',Path('/owned/snapshot'),'pure')
        with self.assertRaises(ValueError):b.docker_args('tcp://example:2375','sha256:'+'a'*64,'test',Path('/owned/snapshot'),'pure')

    def test_grade_rejects_remote_engine_before_any_process(self):
        with tempfile.TemporaryDirectory(dir='/home/docwilde/t') as root,patch.object(b.subprocess,'run') as run:
            with self.assertRaises(ValueError):b.grade(Path(root),'tcp://host:2375','sha256:'+'a'*64)
            run.assert_not_called()

    def test_source_snapshot_binds_exact_bytes_and_is_independent_of_later_edits(self):
        with tempfile.TemporaryDirectory(dir='/home/docwilde/t') as root:
            state=Path(root)/'task';b.prepare('easy-config',state)
            _,inventory=b.snapshot(state,state/'snapshot')
            app=state/'agent/app.py';app.write_text(app.read_text()+'\n# later revision\n')
            self.assertNotEqual(b.digest(app.read_bytes()),inventory['app.py'])
            self.assertEqual(b.digest((state/'snapshot/app.py').read_bytes()),inventory['app.py'])

    def test_strict_output_rejects_duplicate_keys_nonfinite_and_boolean_numeric_aliases(self):
        for raw in ['{"x":1,"x":2}','[NaN]','Infinity']:
            with self.assertRaises(ValueError):b.parse(raw)
        self.assertFalse(b.equal({'n':False},{'n':0}))
        self.assertFalse(b.equal(1,1.0))
        self.assertTrue(b.equal({'n':[False,None,3]},{'n':[False,None,3]}))

    def test_external_controls_remain_strict_when_repository_source_is_group_writable(self):
        with tempfile.TemporaryDirectory(dir='/home/docwilde/t') as root:
            path=Path(root)/'data';path.write_text('{}');path.chmod(0o664)
            with self.assertRaises(ValueError):b.read(path)
            self.assertEqual(b.read(path,repository_source=True),b'{}')
            path.chmod(0o666)
            with self.assertRaises(ValueError):b.read(path,repository_source=True)

    def test_model_mappings_are_provisional_and_not_task_ground_truth(self):
        model=json.loads((ROOT/'model-mappings.json').read_text())
        self.assertEqual(model['status'],'operator_hypotheses_not_measured_model_rankings')
        self.assertTrue(model['evaluation']['difficulty_is_not_expected_candidate_id'])
        self.assertEqual(model['evaluation']['proposed_generation_calls'],162)
        for row in b.corpus()['tasks']:self.assertNotIn('expected_candidate_id',row)
        for row in model['candidates']:
            self.assertGreaterEqual(row['max_output_tokens'],16384)
            self.assertGreater(row['context_tokens'],row['max_output_tokens'])

if __name__=='__main__':unittest.main()
