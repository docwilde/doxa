#!/usr/bin/env python3
"""Prepare repair repositories and grade only in bounded rootless offline Docker.

No provider calls, model execution, routing defaults or supervision gate changes.
Oracle answers/reference repairs stay in the operator checkout and are never
mounted in the candidate container. Every score is a fresh snapshot-bound run.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import selectors
import signal
import stat
import subprocess
import time
import uuid

ROOT=Path(__file__).resolve().parent
MAX_FILE=64*1024
MAX_OUTPUT=64*1024
MAX_SOURCE=256*1024

def require(ok,message):
    if not ok:raise ValueError(message)

def digest(raw):return hashlib.sha256(raw).hexdigest()
def encode(value):return json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False).encode()

def parse(raw):
    def pairs(items):
        result={}
        for key,value in items:
            require(key not in result,'duplicate JSON field');result[key]=value
        return result
    def constant(_):raise ValueError('nonfinite JSON')
    return json.loads(raw,object_pairs_hook=pairs,parse_constant=constant)

def read(path,limit=MAX_FILE,*,repository_source=False):
    fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
    try:
        before=os.fstat(fd)
        # The invoked repository's code/fixtures share its source trust. Private
        # plans/candidates remain stricter; a group-writable checkout is not a
        # reason to accept group-writable external control files.
        denied_mode=0o002 if repository_source else 0o022
        require(stat.S_ISREG(before.st_mode) and before.st_uid==os.getuid() and before.st_nlink==1 and not before.st_mode&denied_mode and before.st_size<=limit,'unsafe bounded input')
        with os.fdopen(fd,'rb',closefd=False) as stream:raw=stream.read(limit+1)
        after=os.fstat(fd);named=Path(path).lstat()
        identity=lambda s:(s.st_dev,s.st_ino,s.st_size,s.st_mtime_ns)
        require(len(raw)<=limit and identity(before)==identity(after)==identity(named),'input changed while reading')
        return raw
    finally:os.close(fd)

def repo_read(name,limit=MAX_FILE):
    require(Path(name).name==name,'repository source must be a direct named file')
    return read(ROOT/name,limit,repository_source=True)

def private_directory(path):
    path=Path(path)
    require(path.is_absolute() and path.resolve()==path and path.is_dir(),'canonical directory required')
    info=path.stat();require(info.st_uid==os.getuid() and not info.st_mode&0o077,'owner-private directory required')
    require(not any(c in str(path) for c in ',\n\r'),'unsupported mount path')

def create(path,value):
    raw=value if isinstance(value,bytes) else encode(value)+b'\n'
    fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
    with os.fdopen(fd,'wb') as stream:stream.write(raw);stream.flush();os.fsync(stream.fileno())
    return digest(raw)

def corpus():
    value=parse(repo_read('corpus.json',2*1024*1024))
    require(value['version']==1 and len(value['tasks'])==18,'corpus identity')
    return value

def task(id):
    rows=[t for t in corpus()['tasks'] if t['id']==id]
    require(len(rows)==1,'unknown task');return rows[0]

def router_config(fallback):
    mappings=parse(repo_read('model-mappings.json'))
    candidates=[{**row,'supports_tools':False} for row in mappings['candidates']]
    require(fallback in {row['id'] for row in candidates},'fallback must be an explicit candidate')
    return {'version':1,'jev_model':'jev-1.13.0','criteria_version':'representative-repairs-v1',
        'candidates':candidates,'fallback_id':fallback,'confidence_threshold':.75,
        'max_calls':100,'max_spend_usd_micros':100000,'max_input_bytes':24576,'deadline_ms':12000}

def readme(row):
    paths=', '.join(row['editable_paths'])
    return (f"# {row['title']}\n\nDifficulty: {row['difficulty']} (predeclared scope; not a measured model ranking).\n\n"
        f"{row['contract']}\n\nEditable files: {paths}. Preserve the public API. "
        "Use only Python standard libraries. You may replace code within those files; no new files or dependencies. "
        "Do not read credentials, use network, run external commands or access files outside the supplied private state root. "
        "The task is tested by a separate offline black-box grader. Public smoke examples are in public-cases.json. "
        "Return/commit an actual code repair, not a prose answer. Do not change README or public cases.\n").encode()

def prepare(id,state,reference=False):
    state=Path(state).absolute();require(not state.exists(),'state path already exists')
    private_directory(state.parent);row=task(id)
    state.mkdir(mode=0o700);project=state/'agent';project.mkdir(mode=0o700)
    immutable={'README.md':readme(row),'public-cases.json':encode(row['public_cases'])+b'\n'}
    for name,raw in immutable.items():create(project/name,raw)
    for name,source in row['reference' if reference else 'starter'].items():create(project/name,source.encode())
    env={'PATH':'/usr/bin:/bin','HOME':str(state),'TMPDIR':str(state),'GIT_CONFIG_NOSYSTEM':'1','GIT_CONFIG_GLOBAL':'/dev/null'}
    for argv in (['git','init','-q'],['git','add','.'],['git','-c','user.name=DOXA benchmark','-c','user.email=benchmark@localhost','-c','core.hooksPath=/dev/null','commit','-qm','test: seed repair task']):
        result=subprocess.run(argv,cwd=project,env=env,capture_output=True,timeout=10)
        require(result.returncode==0,'task repository initialization failed')
    plan={'schema':'doxa-repair-task-plan-v1','task_id':id,'difficulty':row['difficulty'],'family':row['family'],'split':row['split'],
        'corpus_sha256':digest(repo_read('corpus.json',2*1024*1024)),'driver_sha256':digest(repo_read('driver.py')),
        'editable_paths':row['editable_paths'],'immutable_sha256':{k:digest(v) for k,v in immutable.items()},
        'initial_source_sha256':{name:digest(source.encode()) for name,source in row['reference' if reference else 'starter'].items()},
        'trusted_reference_self_check':reference,'provider_calls':0,'human_labels':False}
    create(state/'plan.json',plan)
    return {'state':str(state),'agent_project':str(project),'plan_sha256':digest(read(state/'plan.json')),'reference_self_check':reference,'provider_calls':0}

def snapshot(state,target):
    state=Path(state);private_directory(state);plan=parse(read(state/'plan.json'));row=task(plan['task_id'])
    require(plan['corpus_sha256']==digest(repo_read('corpus.json',2*1024*1024)) and plan['driver_sha256']==digest(repo_read('driver.py')),'frozen suite changed')
    require(plan['editable_paths']==row['editable_paths'],'task path scope changed')
    project=state/'agent';private_directory(project)
    expected=set(row['editable_paths'])|set(plan['immutable_sha256'])
    actual=set()
    for path in project.rglob('*'):
        relative=path.relative_to(project)
        if '.git' in relative.parts:continue
        require(not path.is_symlink(),'candidate symlink refused')
        if path.is_file():actual.add(str(relative))
        elif not path.is_dir():raise ValueError('candidate special path refused')
    require(actual==expected,'candidate added/removed paths')
    target.mkdir(mode=0o700);inventory={};total=0
    for name in sorted(expected):
        raw=read(project/name);total+=len(raw);require(total<=MAX_SOURCE,'candidate source bound')
        if name in plan['immutable_sha256']:require(digest(raw)==plan['immutable_sha256'][name],'immutable task content changed')
        create(target/name,raw);inventory[name]=digest(raw)
    return plan,inventory

def docker_args(host,image,name,source,kind,driver=None):
    require(re.fullmatch(r'sha256:[0-9a-f]{64}',image) is not None,'an immutable Docker image ID is required')
    require(host.startswith('unix:///') and not any(c in host for c in '\n\r'),'a local rootless Docker Unix socket is required')
    return ['/usr/bin/docker','--host',host,'run','--rm','--pull=never','-i','--name',name,
        '--network=none','--read-only','--cap-drop=ALL','--security-opt=no-new-privileges:true',
        '--user=0:0','--ipc=private','--cgroupns=private','--memory=268435456','--memory-swap=268435456',
        '--cpus=1','--pids-limit=64','--tmpfs=/run/benchmark:rw,nosuid,nodev,noexec,size=33554432,mode=1777',
        '--mount',f'type=bind,src={source},dst=/workspace,readonly',
        '--mount',f'type=bind,src={driver or ROOT/"driver.py"},dst=/driver.py,readonly',
        '--env','HOME=/run/benchmark','--env','TMPDIR=/run/benchmark','--workdir=/workspace',
        '--entrypoint=/usr/bin/python3',image,'-I','-B','/driver.py',kind]

def cleanup(host,name):
    env={'PATH':'/usr/bin:/bin'}
    subprocess.run(['/usr/bin/docker','--host',host,'rm','-f',name],env=env,capture_output=True,timeout=5)
    check=subprocess.run(['/usr/bin/docker','--host',host,'ps','-a','--filter','name=^/'+name+'$','--format','{{.ID}}'],env=env,capture_output=True,timeout=5)
    require(check.returncode==0 and not check.stdout.strip(),'offline container cleanup unconfirmed')

def invoke(host,image,source,kind,input,driver=None):
    name='doxa-repair-'+uuid.uuid4().hex
    argv=docker_args(host,image,name,source,kind,driver);start=time.monotonic()
    child=None;failure=None;output=[bytearray(),bytearray()]
    try:
        child=subprocess.Popen(argv,env={'PATH':'/usr/bin:/bin'},stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,start_new_session=True)
        child.stdin.write(encode(input));child.stdin.close()
        selector=selectors.DefaultSelector()
        for i,pipe in enumerate((child.stdout,child.stderr)):
            os.set_blocking(pipe.fileno(),False);selector.register(pipe,selectors.EVENT_READ,i)
        while selector.get_map():
            if time.monotonic()-start>30:failure='timeout';break
            for key,_ in selector.select(.05):
                raw=os.read(key.fileobj.fileno(),4096)
                if not raw:selector.unregister(key.fileobj);continue
                output[key.data].extend(raw)
                if sum(map(len,output))>MAX_OUTPUT:failure='output_limit';break
            if failure:break
        selector.close()
        if failure is None:
            try:child.wait(timeout=2)
            except subprocess.TimeoutExpired:failure='timeout'
        if failure is None and child.returncode!=0:failure='candidate_process_failed'
        if failure is None:
            try:value=parse(bytes(output[0]))
            except (ValueError,UnicodeError):failure='invalid_output'
        return {'observed':failure is None,'output':value if failure is None else None,'failure':failure,
            'duration_ms':round((time.monotonic()-start)*1000),'output_sha256':digest(bytes(output[0])),'output_bytes':len(output[0])}
    finally:
        if child is not None:
            if child.poll() is None:
                try:os.killpg(child.pid,signal.SIGKILL)
                except ProcessLookupError:pass
            child.wait(timeout=3)
            for pipe in (child.stdout,child.stderr):pipe.close()
        cleanup(host,name)

def equal(a,b):
    if type(a) is not type(b):return False
    if isinstance(a,dict):return set(a)==set(b) and all(equal(a[k],b[k]) for k in a)
    if isinstance(a,list):return len(a)==len(b) and all(equal(x,y) for x,y in zip(a,b))
    return a==b

def grade(state,host,image):
    state=Path(state).resolve(strict=True);private_directory(state)
    docker_args(host,image,'doxa-preflight',state,'pure')
    # CLI metadata only: no source execution, no credentials and no implicit pull.
    info=subprocess.run(['/usr/bin/docker','--host',host,'info','--format','{{json .SecurityOptions}}'],env={'PATH':'/usr/bin:/bin'},capture_output=True,timeout=10)
    require(info.returncode==0 and 'name=rootless' in json.loads(info.stdout),'Docker Engine must report rootless mode')
    inspected=subprocess.run(['/usr/bin/docker','--host',host,'image','inspect',image,'--format','{{.Id}}'],env={'PATH':'/usr/bin:/bin'},capture_output=True,timeout=10)
    require(inspected.returncode==0 and inspected.stdout.decode().strip()==image,'immutable local image unavailable')
    run=state/('grade-'+uuid.uuid4().hex);run.mkdir(mode=0o700)
    plan,inventory=snapshot(state,run/'source');row=task(plan['task_id'])
    driver=repo_read('driver.py');require(digest(driver)==plan['driver_sha256'],'driver changed during source capture')
    create(run/'driver.py',driver)
    create(run/'claim.json',{'single_use':True,'source_sha256':digest(encode(inventory)),'plan_sha256':digest(read(state/'plan.json')),'image':image,'network':'none','provider_calls':0})
    observations=[]
    for split in ('public_cases','evaluator_cases'):
        for i,case in enumerate(row[split]):
            result=invoke(host,image,run/'source',row['driver'],case['input'],run/'driver.py')
            passed=result['observed'] and equal(result['output'],case['expected'])
            # Preserve output only in the private operator grade directory.
            create(run/f'{split}-{i}.json',result)
            observations.append({'set':split,'index':i,'passed':passed,'failure':result['failure'],'duration_ms':result['duration_ms']})
    # Recheck the copied source after every contained run; the original may be edited
    # later for a new, separately scored revision, but a grade always binds this copy.
    require({name:digest(read(run/'source'/name)) for name in inventory}==inventory and digest(read(run/'driver.py'))==plan['driver_sha256'],'scored snapshot or driver changed')
    report={'schema':'doxa-repair-grade-v1','task_id':row['id'],'difficulty':row['difficulty'],'family':row['family'],'split':row['split'],
        'passed':all(r['passed'] for r in observations),'tests':observations,'source_sha256':digest(encode(inventory)),
        'corpus_sha256':plan['corpus_sha256'],'driver_sha256':plan['driver_sha256'],
        'image':image,'docker_host':'local_rootless_unix_socket','network':'none','source_readonly':True,
        'oracle_answers_mounted':False,'provider_credentials_passed':False,'host_candidate_execution':False,
        'trusted_reference_self_check':plan['trusted_reference_self_check'],'model_quality_validated':False,'human_labels':False,
        'result_directory':str(run)}
    create(run/'report.json',report)
    return report

def main():
    parser=argparse.ArgumentParser(description=__doc__);sub=parser.add_subparsers(dest='action',required=True)
    sub.add_parser('list')
    p=sub.add_parser('router-config');p.add_argument('--fallback',required=True,choices=('flash-direct','flash-thinking','pro-thinking'))
    p=sub.add_parser('prepare');p.add_argument('task');p.add_argument('state',type=Path);p.add_argument('--trusted-reference',action='store_true')
    p=sub.add_parser('grade');p.add_argument('state',type=Path);p.add_argument('--docker-host',required=True);p.add_argument('--image',required=True)
    args=parser.parse_args()
    if args.action=='list':result=[{k:t[k] for k in ('id','title','difficulty','family','split','editable_paths')} for t in corpus()['tasks']]
    elif args.action=='router-config':result=router_config(args.fallback)
    elif args.action=='prepare':result=prepare(args.task,args.state,args.trusted_reference)
    else:result=grade(args.state,args.docker_host,args.image)
    print(json.dumps(result,indent=2))

if __name__=='__main__':
    try:main()
    except (OSError,ValueError,subprocess.SubprocessError) as error:
        # No raw child stderr or provider/host configuration is included.
        raise SystemExit('repair benchmark refused: '+str(error))
