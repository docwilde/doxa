"""Read-only native diagnostic fixtures; no providers or existing LORE store."""
import builtins
import json
from pathlib import Path
import sys

import pytest

from doxa import doctor, native_lore, settings, version
from doxa import config


@pytest.fixture
def native_diagnostics(tmp_path, monkeypatch):
    root = tmp_path / 'selected-native-store'
    projects = tmp_path / 'projects'
    log = tmp_path / 'requests'
    binary = tmp_path / 'lore-rs'
    metadata = {'root':str(root),'projects_dir':str(projects),'version':'0.61.0-fixture','disabled_stages':['review']}
    binary.write_text(f'#!{sys.executable}\n' + f'''import json, sys
metadata = {metadata!r}
print(json.dumps({{"type":"hello","proto":1,"capabilities":["scrub","snapshot","runtime_config_v1","store_status_v1"]}}),flush=True)
for line in sys.stdin:
    req = json.loads(line)
    with open({str(log)!r},'a') as file: file.write(json.dumps(req)+'\\n')
    value = dict(metadata) if req['op']=='runtime_config_v1' else {{'root':metadata['root'],'version':metadata['version'],'active_beliefs':7}}
    print(json.dumps({{"type":"reply","id":req['id'],"ok":True,"value":value}}),flush=True)
''')
    binary.chmod(0o700)
    monkeypatch.setenv('HOME',str(tmp_path))
    monkeypatch.setenv('LORE_ROOT',str(tmp_path/'env-must-not-be-the-answer'))
    monkeypatch.setenv('DOXA_HOME',str(tmp_path/'doxa'))
    monkeypatch.setenv('DOXA_LORE_RS',str(binary))
    config.invalidate()
    processes = []
    base = native_lore.Carrier
    class RecordingCarrier(base):
        def __init__(self, *, timeout):
            assert timeout == 1.0
            super().__init__(timeout=.2)
        def close(self):
            if self.process is not None:
                processes.append(self.process)
            super().close()
    monkeypatch.setattr(native_lore,'Carrier',RecordingCarrier)
    real_import = builtins.__import__
    def no_python_lore(name,*args,**kwargs):
        assert name != 'lore_core' and not name.startswith('lore_core.')
        return real_import(name,*args,**kwargs)
    monkeypatch.setattr(builtins,'__import__',no_python_lore)
    yield root, binary, log, processes
    assert not root.exists()
    assert processes and all(process.returncode is not None for process in processes)
    config.invalidate()


def test_doctor_uses_read_only_native_status_and_reaps_short_carrier(native_diagnostics):
    root, binary, log, processes = native_diagnostics
    check = doctor._lore_store_check()
    assert check.status == doctor.STATUS_PASS
    assert check.detail == f'{root} -- 7 active belief(s)'
    assert check.fix == ''
    assert [json.loads(line)['op'] for line in log.read_text().splitlines()] == ['store_status_v1']
    assert len(processes) == 1


def test_about_and_settings_use_native_config_and_actual_executable(native_diagnostics, monkeypatch):
    root, binary, log, processes = native_diagnostics
    monkeypatch.setattr(version,'source_sha',lambda:None)
    monkeypatch.setattr(version,'_dep_version',lambda *_:None)
    assert version.lore_core_version() == '0.61.0-fixture'
    rows = dict(version.about_rows())
    assert rows['lore'] == f'0.61.0-fixture  {root}'
    assert rows['lore from'] == f'native  {binary}'
    lore_setting = next(setting for setting in config.SETTINGS if setting.env == 'LORE_ROOT')
    assert settings.resolved_value(lore_setting) == str(root)
    assert [json.loads(line)['op'] for line in log.read_text().splitlines()] == ['runtime_config_v1'] * 3
    assert len(processes) == 3


class FakeCarrier:
    def __init__(self,value=None,error=None):
        self.value,self.error,self.closed=value,error,False
        self.process=type('Process',(),{'args':['/owned/lore-rs']})()
    def request(self,*args,**kwargs):
        if self.error is not None: raise self.error
        return self.value
    def close(self): self.closed=True


@pytest.mark.parametrize('value', [None,{'root':'relative','version':'1','active_beliefs':2},
    {'root':'/owned','version':'1','active_beliefs':True},
    {'root':'/owned','version':'1','active_beliefs':-1},
    {'root':'/owned','version':'1','active_beliefs':2**64},
    {'root':'/owned\nsecret','version':'1','active_beliefs':2}])
def test_invalid_store_status_is_fixed_failure_and_always_closes(monkeypatch,value):
    client=FakeCarrier(value)
    monkeypatch.setattr(native_lore,'Carrier',lambda **_:client)
    check=doctor._lore_store_check()
    assert check.status == doctor.STATUS_FAIL
    assert check.detail == 'native LORE store unavailable'
    assert client.closed


@pytest.mark.parametrize('field,value', [('root','relative'),('root','/owned\x1b[31m'),
    ('projects_dir',None),('version',''),('version','secret\n'),('disabled_stages',['unrecognized'])])
def test_invalid_native_metadata_never_becomes_an_about_or_settings_guess(monkeypatch,field,value):
    data={'root':'/owned','projects_dir':'/owned/projects','version':'1','disabled_stages':[]}
    data[field]=value
    client=FakeCarrier(data)
    monkeypatch.setattr(native_lore,'Carrier',lambda **_:client)
    assert version.native_lore_info() is None
    assert client.closed
    assert version.lore_core_version() is None
    setting=next(setting for setting in config.SETTINGS if setting.env=='LORE_ROOT')
    assert settings.resolved_value(setting)=='unavailable (native LORE)'


def test_diagnostic_exception_text_never_leaks_or_falls_back_to_python(monkeypatch):
    client=FakeCarrier(error=RuntimeError('provider-token SECRET source data'))
    monkeypatch.setattr(native_lore,'Carrier',lambda **_:client)
    assert doctor._lore_store_check().detail=='native LORE store unavailable'
    assert version.lore_core_version() is None
    rows=dict(version.about_rows())
    assert 'lore' not in rows and 'lore from' not in rows
    assert client.closed
