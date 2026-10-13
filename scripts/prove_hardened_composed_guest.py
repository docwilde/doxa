#!/usr/bin/env python3
"""Credential-free composed ROOT Runtime proof, only in the disposable guest.

The caller runs the real Rust Runtime/client/HookBroker, and ROOT runs the real
installed broker/native gates. Fixture JSON never supplies admission. No
Engine restart, Docker CLI, host Git authority or paid provider is involved.
"""
from __future__ import annotations
import argparse
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import select
import shlex
import signal
import stat
import subprocess
import sys
import tarfile
import time

PROVIDER = r'''#!/usr/bin/env python3
"""Immutable credential-free composed proof fixture; no provider/network API."""
import errno,hashlib,json,os,shlex,socket,stat,subprocess,sys,threading,time
from pathlib import Path
THREAD='composed-thread-20261013'
ROLLOUT=Path('/home/doxa/codex/sessions/rollout-composed.jsonl')
FAMILY=Path(sys.argv[0]).name
WATCH=None
WATCH_DONE=threading.Event()
def emit(op,**fields):print(json.dumps(dict(op=op,**fields),sort_keys=True),flush=True)
def private(path,data):
    path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_TRUNC|os.O_NOFOLLOW,0o600)
    with os.fdopen(fd,'wb') as out:out.write(data);out.flush();os.fsync(out.fileno())
def main():
    global WATCH
    if sys.argv[1:]==['--version']:print('credential-free composed '+FAMILY);return
    if sys.argv[1:]!=['stream']:raise RuntimeError('fixture accepts only stream or --version')
    owner=None
    if FAMILY=='codex':
        owner=socket.socket(fileno=int(os.environ['DOXA_CODEX_OWNER_FD']))
        owner.sendall(b'DOXA_PROVIDER_OWNER_V1\n')
        if owner.recv(1)!=b'G':raise RuntimeError('protected owner refused')
        lines=[{'timestamp':'2026-10-13T00:00:00Z','type':'session_meta','payload':{'id':THREAD,'cwd':'/workspace'}},
               {'timestamp':'2026-10-13T00:00:01Z','type':'event_msg','payload':{'type':'user_message','message':'credential-free fixture'}}]
        private(ROLLOUT,b''.join(json.dumps(row,sort_keys=True).encode()+b'\n' for row in lines))
    settings=None
    if FAMILY=='claude':
        path=Path(os.environ['CLAUDE_CONFIG_DIR'])/'settings.json'
        if str(path)!='/home/doxa/claude/settings.json':raise RuntimeError('fixed actual Claude home required')
        data=path.read_bytes();meta=path.lstat()
        if not stat.S_ISREG(meta.st_mode) or meta.st_nlink!=1:raise RuntimeError('actual settings single-link regular file required')
        settings={'sha256':hashlib.sha256(data).hexdigest(),'bytes':len(data),'uid':meta.st_uid,'mode':stat.S_IMODE(meta.st_mode),
                  'marker':json.loads(data)['fixture_marker']}
    emit('ready',family=FAMILY,pid=os.getpid(),uid=os.getuid(),owner_handshake=owner is not None,settings=settings)
    for line in sys.stdin:
        if len(line)>65536:raise RuntimeError('fixture input bound')
        request=json.loads(line);op=request['op']
        if op=='exit':emit(op);return
        if FAMILY!='codex':raise RuntimeError('Claude fixture allows exit only')
        if op=='mutate':
            with ROLLOUT.open('ab') as out:out.write(b'{"type":"event_msg","payload":{"message":"mutation"}}\n');out.flush();os.fsync(out.fileno())
            emit(op,size=ROLLOUT.stat().st_size)
        elif op=='hook':
            argv=shlex.split(request['command'])
            if len(argv)!=4 or argv[:3]!=['/usr/local/bin/doxa-isolation-worker','hook','/run/doxa/session/hook.sock'] or not re_cap(argv[3]):
                raise RuntimeError('fixture hook command scope')
            event={'hook_event_name':'PreCompact','trigger':'manual','session_id':THREAD,'transcript_path':str(ROLLOUT)}
            result=subprocess.run(argv,input=json.dumps(event).encode(),stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=90)
            if len(result.stdout)>65536 or len(result.stderr)>65536:raise RuntimeError('fixture hook response bound')
            emit(op,result=json.loads(result.stdout),returncode=result.returncode,stderr_bytes=len(result.stderr))
        elif op=='poison-git':
            config=Path('/workspace/.git/config')
            with config.open('a') as out:
                out.write('\n[filter "proof"]\nclean = /bin/sh -c "touch /workspace/git-filter-marker; cat"\n')
                out.write('smudge = /bin/sh -c "touch /workspace/git-filter-marker; cat"\n')
                out.write('process = /bin/sh -c "touch /workspace/git-filter-marker; cat"\n')
                out.write('[diff "proof"]\ncommand = /bin/sh -c "touch /workspace/git-diff-marker; exit 0"\n')
                out.flush();os.fsync(out.fileno())
            private(Path('/workspace/filtered.txt'),b'changed with untrusted filter config\n')
            emit(op,poisoned=True)
        elif op=='markers':
            emit(op,filter_marker=Path('/workspace/git-filter-marker').exists(),diff_marker=Path('/workspace/git-diff-marker').exists(),
                 plain=Path('/workspace/plain.txt').read_text())
        elif op=='watch-git-view':
            def watch():
                global WATCH
                deadline=time.monotonic()+30
                while time.monotonic()<deadline:
                    try:views=list(Path('/run/doxa/session/git-safe').iterdir())
                    except FileNotFoundError:views=[]
                    for view in views:
                        if not view.is_dir() or not (view/'config').exists():continue
                        try:
                            fd=os.open(view/'config',os.O_WRONLY|os.O_APPEND|os.O_NOFOLLOW)
                            os.write(fd,b'worker mutation');os.close(fd);code=None
                        except FileNotFoundError:continue
                        except OSError as cause:code=cause.errno
                        WATCH={'errno':code,'generation':view.name};WATCH_DONE.set();return
                    time.sleep(.005)
                WATCH_DONE.set()
            threading.Thread(target=watch,daemon=True).start();emit(op,started=True)
        elif op=='write-git-view':
            WATCH_DONE.wait(35)
            if WATCH is None:raise RuntimeError('no active published ROOT Git view observed')
            emit(op,**WATCH)
        else:raise RuntimeError('unknown fixture operation')
def re_cap(value):return len(value)==48 and all(char in '0123456789abcdef' for char in value)
if __name__=='__main__':main()
'''

def require(condition, reason):
    if not condition: raise RuntimeError(reason)

def sha_file(path):
    digest=hashlib.sha256()
    with Path(path).open('rb') as source:
        while chunk:=source.read(65536):digest.update(chunk)
    return digest.hexdigest()

def write_new(path,data,mode=0o600,uid=0,gid=0):
    fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW|os.O_CLOEXEC,mode)
    with os.fdopen(fd,'wb') as target:
        os.fchmod(target.fileno(),mode);os.fchown(target.fileno(),uid,gid)
        target.write(data);target.flush();os.fsync(target.fileno())

def process_facts(pid):
    fields=Path(f'/proc/{pid}/stat').read_text().rsplit(')',1)[1].split()
    return {'pid':pid,'ppid':int(fields[1]),'group':int(fields[2]),'session':int(fields[3]),
            'start':int(fields[19]),'uid':Path(f'/proc/{pid}').stat().st_uid}

class OwnedProcesses:
    """Retain kernel pidfds for our launcher and observed descendants only."""
    def __init__(self,child):
        self.child=child;self.pins={};self.sockets={}
        self.pin(child.pid);self.capture()
    def pin(self,pid):
        before=process_facts(pid);fd=os.pidfd_open(pid)
        try:
            require(process_facts(pid)==before,'owned process identity changed while pinning')
            self.pins[pid]=(fd,before)
        except BaseException:os.close(fd);raise
    def live(self):
        result=[]
        for pid,(fd,facts) in self.pins.items():
            if select.select([fd],[],[],0)[0]:continue
            actual=process_facts(pid)
            require(actual['start']==facts['start'],'owned pidfd/starttime identity changed')
            result.append(pid)
        return result
    def capture(self):
        ancestors=set(self.live())
        rows={}
        for path in Path('/proc').iterdir():
            if not path.name.isdecimal():continue
            try:rows[int(path.name)]=process_facts(int(path.name))
            except (FileNotFoundError,ProcessLookupError):pass
        while True:
            added={pid for pid,row in rows.items() if row['ppid'] in ancestors and pid not in self.pins}
            if not added:break
            for pid in added:
                try:self.pin(pid);ancestors.add(pid)
                except (FileNotFoundError,ProcessLookupError):rows.pop(pid,None)
            require(len(self.pins)<=512,'owned proof process census exceeded bound')
        inodes=set()
        for pid in self.live():
            try:
                for fd in Path(f'/proc/{pid}/fd').iterdir():
                    try:target=os.readlink(fd)
                    except FileNotFoundError:continue
                    match=re.fullmatch(r'socket:\[([0-9]+)\]',target)
                    if match:inodes.add(match[1])
            except FileNotFoundError:pass
        for line in Path('/proc/net/unix').read_text().splitlines()[1:]:
            fields=line.split(maxsplit=7)
            if len(fields)==8 and fields[6] in inodes:self.sockets[fields[7]]=fields[6]
    def terminate(self):
        self.capture()
        # pidfd_send_signal never targets a recycled numeric PID/group.
        for pid in self.live():signal.pidfd_send_signal(self.pins[pid][0],signal.SIGTERM)
        deadline=time.monotonic()+3
        while self.live() and time.monotonic()<deadline:
            self.capture();time.sleep(.02)
        for pid in self.live():signal.pidfd_send_signal(self.pins[pid][0],signal.SIGKILL)
        self.child.wait(timeout=3)
        deadline=time.monotonic()+3
        while self.live() and time.monotonic()<deadline:time.sleep(.02)
        require(not self.live(),'owned proof process survived bounded pidfd teardown')
    def receipt(self):
        return {'processes':[facts for _,facts in self.pins.values()],'live_processes':self.live(),'socket_paths':sorted(self.sockets)}
    def close(self):
        for fd,_ in self.pins.values():os.close(fd)

def run(argv,timeout=60,environment=None,observe=None,census=None,uid=None):
    credentials={} if uid is None else {'user':uid,'group':uid,'extra_groups':[]}
    child=subprocess.Popen(argv,env=environment,stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.PIPE,start_new_session=True,**credentials)
    owned=OwnedProcesses(child);deadline=time.monotonic()+timeout
    try:
        while True:
            owned.capture()
            if observe is not None:observe()
            require(time.monotonic()<deadline,'owned proof command exceeded deadline: '+str(argv[0]))
            try:stdout,stderr=child.communicate(timeout=min(.1,max(.001,deadline-time.monotonic())));break
            except subprocess.TimeoutExpired:continue
        require(len(stdout)<=256*1024 and len(stderr)<=256*1024,'bounded proof command output')
        require(child.returncode==0,'proof command failed: '+str(argv[0])+': '+stderr.decode(errors='replace')[:4096])
        return stdout
    finally:
        owned.terminate()
        if census is not None:census.append(owned.receipt())
        owned.close()

def image_build(module,engine,args):
    entries=[('composed-provider.py',PROVIDER.encode(),0o755),('doxa-isolation-worker',args.worker.read_bytes(),0o755)]
    package={}
    for name in ['codex','codex-app-server','codex-code-mode-host','codex-code-mode-host-payload','receipt.json']:
        content=(json.dumps({'scope':'credential-free-composed-fixture','artifact':name},sort_keys=True)+'\n').encode()
        entries.append(('package/'+name,content,0o444));package['/opt/doxa/codex-provider/'+name]=hashlib.sha256(content).hexdigest()
    support=json.loads((args.git_support/'manifest.json').read_text())
    require(set(support)=={'/usr/bin/git','/usr/lib/x86_64-linux-gnu/libpcre2-8.so.0','/usr/lib/x86_64-linux-gnu/libz.so.1'},'fixed Git dependency inventory')
    for index,(target,digest) in enumerate(sorted(support.items())):
        source=args.git_support/('file'+str(index));module.controlled(source)
        require(sha_file(source)==digest,'immutable staged Git input differs')
        entries.append(('git/file'+str(index),source.read_bytes(),0o755))
    dockerfile=f'FROM {args.base_image}\nCOPY composed-provider.py /usr/local/bin/claude\nCOPY composed-provider.py /usr/local/bin/codex\nCOPY doxa-isolation-worker /usr/local/bin/doxa-isolation-worker\nCOPY package/ /opt/doxa/codex-provider/\n'
    for index,target in enumerate(sorted(support)):dockerfile+=f'COPY git/file{index} {target}\n'
    dockerfile+='LABEL org.doxa.composed-proof=credential-free\n'
    entries.insert(0,('Dockerfile',dockerfile.encode(),0o644))
    archive=io.BytesIO()
    with tarfile.open(fileobj=archive,mode='w') as tar:
        for name,data,mode in entries:
            entry=tarfile.TarInfo(name);entry.size,entry.mode,entry.uid,entry.gid,entry.mtime=len(data),mode,0,0,0
            tar.addfile(entry,io.BytesIO(data))
    data=archive.getvalue();deadline=module.Deadline(120);channel=engine.connect(deadline)
    try:
        route='/build?t=doxa-composed-proof%3A20261013&dockerfile=Dockerfile&rm=1&forcerm=1&pull=0&networkmode=none'
        channel.send(f'POST /v{engine.config["api_version"]}{route} HTTP/1.1\r\nHost: docker\r\nContent-Type: application/x-tar\r\nContent-Length: {len(data)}\r\nConnection: close\r\n\r\n'.encode()+data)
        status,_,body=module.response(channel,2*1024*1024);require(status==200,'typed Engine image build refused')
        output=body.read();write_new(args.output/'image-build.ndjson',output)
        image=None
        for line in output.splitlines():
            row=json.loads(line);require(not row.get('error'),'credential-free image build failed')
            if 'aux' in row:image=row['aux'].get('ID',image)
        if image is None:
            _,info=engine.request(module.Deadline(10),'GET','/images/doxa-composed-proof:20261013/json');image=info.get('Id')
        require(re.fullmatch(r'sha256:[0-9a-f]{64}',image or ''),'actual immutable image identity absent')
        write_new(args.output/'fixture-provider.py',PROVIDER.encode(),0o444)
        write_new(args.output/'Dockerfile',dockerfile.encode(),0o444)
        return image,hashlib.sha256(PROVIDER.encode()).hexdigest(),package,support
    finally:channel.sock.close()

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--ack-disposable',action='store_true')
    parser.add_argument('--broker-source',required=True,type=Path)
    parser.add_argument('--client',required=True,type=Path)
    parser.add_argument('--git-support',required=True,type=Path)
    parser.add_argument('--worker',type=Path,default=Path('/root/isolation-proof/doxa-isolation-worker-current'))
    parser.add_argument('--gateway',type=Path,default=Path('/usr/libexec/doxa/doxa-hardened-gateway-isolation-proof'))
    parser.add_argument('--worker-sha256',required=True)
    parser.add_argument('--gateway-sha256',required=True)
    parser.add_argument('--session-id',default='composed-runtime-r3-20261013')
    parser.add_argument('--project-id',type=int,default=5010)
    parser.add_argument('--base-image',default='sha256:5eb3f63be16d9bd9d0794e52feb02534ca6c1d581d535c8d3adb3df012aa2eaa')
    parser.add_argument('--output',required=True,type=Path)
    args=parser.parse_args()
    require(args.ack_disposable and os.geteuid()==0 and os.uname().nodename=='doxa-proof','explicit disposable ROOT guest required')
    require('VERSION_ID="26.04"' in Path('/etc/os-release').read_text(),'Ubuntu26.04 disposable fixture only')
    require(run(['systemd-detect-virt','--vm']).strip()==b'kvm','KVM disposable fixture only')
    require(args.output.is_absolute() and args.output.is_relative_to('/root/isolation-proof') and not args.output.exists(),'fresh private evidence directory required')
    args.output.mkdir(mode=0o700)
    spec=importlib.util.spec_from_file_location('reviewed_composed_root_broker',args.broker_source)
    require(spec is not None and spec.loader is not None,'reviewed broker source missing')
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    for path in [args.worker,args.gateway,args.client,args.broker_source,args.git_support/'manifest.json']:module.controlled(path)
    require(sha_file(args.worker)==args.worker_sha256 and sha_file(args.gateway)==args.gateway_sha256,'current reviewed worker/native binaries differ')
    sid=args.session_id
    require(re.fullmatch(r'composed-runtime-r[3-9][0-9]*-20261013',sid) and 5010<=args.project_id<6000,'fresh reserved composed scope required')
    root=Path('/srv/doxa-quota-proof')/sid;state=Path('/run/doxa/hardened')/sid
    caller=Path('/home/doxa')/('.'+sid);installed=Path('/usr/libexec/doxa')/(sid+'-broker.py')
    client=Path('/usr/libexec/doxa')/(sid+'-client')
    require(not root.exists() and not state.exists() and not caller.exists() and not installed.exists() and not client.exists(),'proof must not adopt existing session/artifacts')
    write_new(installed,args.broker_source.read_bytes(),0o444);write_new(client,args.client.read_bytes(),0o755)
    state.mkdir(mode=0o755,parents=True);os.chmod(state,0o755)
    caller.mkdir(mode=0o700);os.chown(caller,1000,1000)
    for name in ['state','source','home']:
        path=caller/name;path.mkdir(mode=0o700);os.chown(path,1000,1000)
    for name,data in [('plain.txt',b'before\n'),('filtered.txt',b'safe initial fixture\n'),('.gitattributes',b'filtered.txt filter=proof diff=proof\n')]:
        write_new(caller/'source'/name,data,0o600,1000,1000)
    env={'PATH':'/usr/bin:/bin','HOME':str(caller/'home'),'DOXA_HOME':str(caller/'state'),'LANG':'C','XDG_RUNTIME_DIR':'/run/user/1000',
         'GIT_CONFIG_NOSYSTEM':'1','GIT_CONFIG_GLOBAL':'/dev/null','GIT_TERMINAL_PROMPT':'0'}
    for argv in [['git','init','-q',str(caller/'source')],['git','-C',str(caller/'source'),'add','.'],
                 ['git','-C',str(caller/'source'),'-c','user.name=Disposable Fixture','-c','user.email=fixture@example.invalid','commit','-qm','test: add credential-free fixture']]:
        run(argv,environment=env,uid=1000)
    runtime=Path('/run/user/1000')
    if not runtime.exists():runtime.mkdir(mode=0o700);os.chown(runtime,1000,1000)
    require(runtime.stat().st_uid==1000 and stat.S_IMODE(runtime.stat().st_mode)==0o700,'caller runtime directory must be private')
    engine_config={'uid':1001,'gid':1001,'socket':'/run/user/1001/doxa-proof/docker.sock','exe_path':'/usr/bin/dockerd',
        'exe_sha256':sha_file('/usr/bin/dockerd'),'config_path':'/etc/doxa-proof/engine-daemon.json',
        'config_sha256':sha_file('/etc/doxa-proof/engine-daemon.json'),'api_version':'1.51'}
    policy={'version':1,'session_id':sid,'caller_uid':1000,'engine':engine_config,'root':str(root),'image':args.base_image,
        'worker':{'path':module.WORKER,'sha256':args.worker_sha256},'providers':{
            'claude':{'path':'/usr/local/bin/claude','sha256':'0'*64,'artifacts':{'/usr/local/bin/claude':'0'*64},'environment_keys':[],'egress':['example.com'],'proxy_port':3128}},
        'authority':{'path':str(args.gateway),'sha256':args.gateway_sha256},'broker':{'path':str(installed),'sha256':sha_file(installed)},
        'limits':{'memory_bytes':512*1024*1024,'cpu_quota':100000,'cpu_period':100000,'pids':128,'disk_bytes':64*1024*1024,
            'output_bytes':1024*1024,'stdin_bytes':1024*1024,'deadline_seconds':300,'operation_seconds':30}}
    module.validate_policy(policy);engine=module.Engine(policy)
    connection=engine.connect(module.Deadline(10));connection.sock.close();engine.live()
    report={'scope':'actual-composed-root-runtime-fixture','session_id':sid,'project_id':args.project_id,'paid_calls':0,'live_provider_verified':False,
        'worker_sha256':args.worker_sha256,'gateway_sha256':args.gateway_sha256,'broker_sha256':sha_file(installed),'client_sha256':sha_file(client),
        'engine_pid_before':engine.pid,'engine_socket_before':engine.socket_identity,'engine_pins':engine_config,'cleanup':{}}
    broker=None;broker_owned=None;log=None;owned_cids=set();owned_cgroups=set();commands=[];failure=None
    def observe_scope():
        broker_owned.capture()
        private=state/'manifest.json'
        if private.exists():
            row=module.strict_json(module.read_regular(private,root=True))
            require(row.get('session_id')==sid and row.get('policy_sha256')==report['receipt']['policy_sha256'],'owned Root manifest scope changed')
            cid=row.get('cid');require(re.fullmatch(r'[0-9a-f]{64}',cid or ''),'owned Root manifest CID invalid')
            owned_cids.add(cid)
            try:
                for line in Path('/proc/'+str(row['host_init_pid'])+'/cgroup').read_text().splitlines():
                    if line.startswith('0::/'):
                        relative=line[4:];require('..' not in Path(relative).parts and cid in relative,'container cgroup scope differs')
                        owned_cgroups.add('/sys/fs/cgroup/'+relative)
            except FileNotFoundError:pass
    try:
        image,provider_sha,package,git_support=image_build(module,engine,args)
        policy['image']=image;policy['providers']['claude']['sha256']=provider_sha;policy['providers']['claude']['artifacts']['/usr/local/bin/claude']=provider_sha
        policy['providers']['codex']={'path':'/usr/local/bin/codex','sha256':provider_sha,'artifacts':dict(package,**{'/usr/local/bin/codex':provider_sha}),
            'environment_keys':[],'egress':['example.com'],'proxy_port':3129}
        module.validate_policy(policy);report.update(image=image,provider_sha256=provider_sha,git_support_sha256=git_support)
        provision=Path('/root/isolation-proof/quota-bin/doxa-quota-provision-family')
        run([str(provision),'create',sid,str(root),'1000','1001','1001',str(args.project_id),'67108864',image,args.worker_sha256])
        source=args.output/'source-policy.json';write_new(source,json.dumps(policy,sort_keys=True).encode()+b'\n')
        policy_dir=Path('/etc/doxa/hardened');policy_dir.mkdir(mode=0o755,parents=True)
        destination=policy_dir/(sid+'.json');require(not destination.exists(),'installed policy must be fresh')
        python=str(Path(sys.executable).resolve())
        receipt=json.loads(run([python,'-I',str(installed),'install','--source',str(source),'--destination',str(destination)]))
        report['receipt']=receipt
        log=(args.output/'root-broker.log').open('xb')
        broker=subprocess.Popen([python,'-I',str(installed),'serve','--policy',str(destination)],stdin=subprocess.DEVNULL,
            stdout=log,stderr=log,env={'PATH':'/usr/bin:/bin','LANG':'C'},start_new_session=True)
        broker_owned=OwnedProcesses(broker)
        deadline=time.monotonic()+15
        while not (state/'broker-instance.json').exists():
            broker_owned.capture()
            require(broker.poll() is None and time.monotonic()<deadline,'actual ROOT broker startup failed or timed out');time.sleep(.02)
        outcome=run([str(client),str(caller/'state'),str(caller/'source'),sid,str(caller/'client-report.json')],
            timeout=360,environment=env,observe=observe_scope,census=commands,uid=1000)
        report['client_stdout']=outcome.decode();report['client']=json.loads((caller/'client-report.json').read_text())
        require(report['client']['runtime_prepared'] and report['client']['resumed_and_stopped'],'actual Runtime proof incomplete')
        for cid in [report['client']['cid'],report['client']['resumed_cid']]:
            require(re.fullmatch(r'[0-9a-f]{64}',cid),'actual Runtime container identity invalid');owned_cids.add(cid)
            engine.request(module.Deadline(10),'GET',f'/containers/{cid}/json',statuses=(404,))
            report['cleanup'][cid]='404'
        engine.live();require(engine.pid==report['engine_pid_before'] and engine.socket_identity==tuple(report['engine_socket_before']),'shared Engine changed during proof')
        report['cases_completed']=True
    except BaseException as cause:
        failure=cause;report['error']=str(cause)
    finally:
        cleanup_errors=[]
        if broker is not None and broker.poll() is None:
            try:
                reply=json.loads(run([str(Path(sys.executable).resolve()),'-I',str(installed),'client','--session-id',sid,'--op','stop'],environment=env,observe=observe_scope,census=commands,uid=1000))
                require(reply.get('status') in {'stopped','unprepared'} and reply.get('session_id')==sid and reply.get('policy_sha256')==report['receipt']['policy_sha256'],'Root cleanup stop reply scope differs')
                report['cleanup']['stop_status']=reply['status']
            except BaseException as cause:cleanup_errors.append('stop: '+str(cause))
        if broker_owned is not None:
            try:
                broker_owned.terminate();report['cleanup']['broker_processes']=broker_owned.receipt()
            except BaseException as cause:cleanup_errors.append('broker processes: '+str(cause))
        if log is not None:log.close()
        try:
            private=state/'manifest.json'
            if private.exists():
                row=module.strict_json(module.read_regular(private,root=True));cid=row.get('cid')
                require(row.get('session_id')==sid and row.get('policy_sha256')==report.get('receipt',{}).get('policy_sha256') and re.fullmatch(r'[0-9a-f]{64}',cid or ''),'cleanup Rootmanifest scope differs')
                owned_cids.add(cid)
            # Recovery can delete only exact labelled CIDs and makes the
            # receipt fail. It cannot turn failed Runtime cleanup into PASS.
            for cid in sorted(owned_cids):
                status,info=engine.request(module.Deadline(10),'GET',f'/containers/{cid}/json',statuses=(200,404))
                if status==200:
                    labels=info.get('Config',{}).get('Labels',{})
                    require(labels.get('io.doxa.root-session')==sid and labels.get('io.doxa.root-policy')==report.get('receipt',{}).get('policy_sha256'),
                            'cleanup container label/policy differs')
                    cleanup_errors.append('Runtime left a container requiring diagnostic removal: '+cid)
                    engine.request(module.Deadline(10),'DELETE',f'/containers/{cid}?force=1&v=1',statuses=(204,404))
                engine.request(module.Deadline(10),'GET',f'/containers/{cid}/json',statuses=(404,));report['cleanup'][cid]='404'
            engine.request(module.Deadline(10),'GET','/containers/doxa-hardened-'+sid+'/json',statuses=(404,))
            _,all_containers=engine.request(module.Deadline(10),'GET','/containers/json?all=1')
            require(not [row for row in all_containers if row.get('Labels',{}).get('io.doxa.root-session')==sid],'owned container census is nonzero')
            deadline=time.monotonic()+3
            while any(Path(path).exists() for path in owned_cgroups) and time.monotonic()<deadline:time.sleep(.02)
            remaining_cgroups=[path for path in owned_cgroups if Path(path).exists()]
            sockets=set(broker_owned.sockets if broker_owned is not None else ())
            for row in commands:sockets.update(row['socket_paths'])
            # Engine endpoint is shared, and is checked separately below.
            sockets.discard(engine_config['socket'])
            kernel_paths={row.split(maxsplit=7)[7] for row in Path('/proc/net/unix').read_text().splitlines()[1:] if len(row.split(maxsplit=7))==8}
            live_sockets=sorted(sockets & kernel_paths)
            scoped_socket_files=[str(p) for base in [state,root/'broker'] if base.exists() for p in base.rglob('*') if stat.S_ISSOCK(p.lstat().st_mode)]
            remaining_processes=[]
            exact_programs={str(client).encode(),str(installed).encode(),str(args.gateway).encode()}
            for path in Path('/proc').iterdir():
                if not path.name.isdecimal() or int(path.name)==os.getpid():continue
                try:
                    argv=(path/'cmdline').read_bytes().split(b'\0')
                    if exact_programs.intersection(argv):remaining_processes.append(process_facts(int(path.name)))
                except (FileNotFoundError,ProcessLookupError):pass
            engine.live();require(engine.pid==report['engine_pid_before'] and engine.socket_identity==tuple(report['engine_socket_before']),'shared Engine changed during cleanup')
            report.update(engine_pid_after=engine.pid,engine_socket_after=engine.socket_identity)
            report['cleanup'].update(command_processes=commands,cgroups=sorted(owned_cgroups),remaining_cgroups=remaining_cgroups,
                live_sockets=live_sockets,scoped_socket_files=scoped_socket_files,remaining_processes=remaining_processes,container_census=0)
            require(not remaining_cgroups and not live_sockets and not scoped_socket_files and not remaining_processes,'owned process/cgroup/socket cleanup census is nonzero')
        except BaseException as cause:cleanup_errors.append('scope census: '+str(cause))
        if broker_owned is not None:broker_owned.close()
        if cleanup_errors:
            report['cleanup']['errors']=cleanup_errors
            if failure is None:failure=RuntimeError('; '.join(cleanup_errors))
        report['status']='passed' if failure is None and report.get('cases_completed') is True else 'failed'
        write_new(args.output/'report.json',json.dumps(report,sort_keys=True,indent=2).encode()+b'\n')
    if failure is not None:raise failure

if __name__=='__main__':main()
