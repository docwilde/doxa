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
import shlex
import signal
import stat
import subprocess
import sys
import tarfile
import time

PROVIDER = r'''#!/usr/bin/env python3
"""Immutable credential-free composed proof fixture; no provider/network API."""
import errno,json,os,shlex,socket,subprocess,sys
from pathlib import Path
THREAD='composed-thread-20261013'
ROLLOUT=Path('/home/doxa/codex/sessions/rollout-composed.jsonl')
FAMILY=Path(sys.argv[0]).name
def emit(op,**fields):print(json.dumps(dict(op=op,**fields),sort_keys=True),flush=True)
def private(path,data):
    path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_TRUNC|os.O_NOFOLLOW,0o600)
    with os.fdopen(fd,'wb') as out:out.write(data);out.flush();os.fsync(out.fileno())
def main():
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
    emit('ready',family=FAMILY,pid=os.getpid(),uid=os.getuid(),owner_handshake=owner is not None)
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
        elif op=='write-git-view':
            views=list(Path('/run/doxa/session/git-safe').iterdir())
            if len(views)!=1:raise RuntimeError('fixture requires current one ROOT Git view')
            try:
                fd=os.open(views[0]/'config',os.O_WRONLY|os.O_APPEND|os.O_NOFOLLOW);os.write(fd,b'worker mutation');os.close(fd);code=None
            except OSError as cause:code=cause.errno
            emit(op,errno=code)
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

def run(argv,timeout=60,environment=None):
    result=subprocess.run(argv,env=environment,stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=timeout)
    require(len(result.stdout)<=256*1024 and len(result.stderr)<=256*1024,'bounded proof command output')
    require(result.returncode==0,'proof command failed: '+str(argv[0])+': '+result.stderr.decode(errors='replace')[:4096])
    return result.stdout

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
    sid='composed-runtime-20261013';root=Path('/srv/doxa-quota-proof')/sid;state=Path('/run/doxa/hardened')/sid
    caller=Path('/home/doxa/.composed-proof-20261013');installed=Path('/usr/libexec/doxa/composed-root-broker-20261013.py')
    client=Path('/usr/libexec/doxa/hardened-composed-client-20261013')
    require(not root.exists() and not state.exists() and not caller.exists() and not installed.exists() and not client.exists(),'proof must not adopt existing session/artifacts')
    write_new(installed,args.broker_source.read_bytes(),0o444);write_new(client,args.client.read_bytes(),0o755)
    state.mkdir(mode=0o755,parents=True);os.chmod(state,0o755)
    caller.mkdir(mode=0o700);os.chown(caller,1000,1000)
    for name in ['state','source','home']:
        path=caller/name;path.mkdir(mode=0o700);os.chown(path,1000,1000)
    for name,data in [('plain.txt',b'before\n'),('filtered.txt',b'safe initial fixture\n'),('.gitattributes',b'filtered.txt filter=proof diff=proof\n')]:
        write_new(caller/'source'/name,data,0o600,1000,1000)
    env={'PATH':'/usr/bin:/bin','HOME':str(caller/'home'),'DOXA_HOME':str(caller/'state'),'LANG':'C','XDG_RUNTIME_DIR':'/run/user/1000'}
    for argv in [['git','init','-q',str(caller/'source')],['git','-C',str(caller/'source'),'add','.'],
                 ['git','-C',str(caller/'source'),'-c','user.name=Disposable Fixture','-c','user.email=fixture@example.invalid','commit','-qm','credential-free fixture']]:
        run(['runuser','-u','doxa','--',*argv],environment=env)
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
    report={'scope':'actual-composed-root-runtime-fixture','session_id':sid,'project_id':5007,'paid_calls':0,'live_provider_verified':False,
        'worker_sha256':args.worker_sha256,'gateway_sha256':args.gateway_sha256,'broker_sha256':sha_file(installed),'client_sha256':sha_file(client),
        'engine_pid_before':engine.pid,'engine_socket_before':engine.socket_identity,'cleanup':{}}
    broker=None;log=None;owned_cids=[]
    try:
        image,provider_sha,package,git_support=image_build(module,engine,args)
        policy['image']=image;policy['providers']['claude']['sha256']=provider_sha;policy['providers']['claude']['artifacts']['/usr/local/bin/claude']=provider_sha
        policy['providers']['codex']={'path':'/usr/local/bin/codex','sha256':provider_sha,'artifacts':dict(package,**{'/usr/local/bin/codex':provider_sha}),
            'environment_keys':[],'egress':['example.com'],'proxy_port':3129}
        module.validate_policy(policy);report.update(image=image,provider_sha256=provider_sha,git_support_sha256=git_support)
        provision=Path('/root/isolation-proof/quota-bin/doxa-quota-provision-family')
        run([str(provision),'create',sid,str(root),'1000','1001','1001','5007','67108864',image,args.worker_sha256])
        source=args.output/'source-policy.json';write_new(source,json.dumps(policy,sort_keys=True).encode()+b'\n')
        policy_dir=Path('/etc/doxa/hardened');policy_dir.mkdir(mode=0o755,parents=True)
        destination=policy_dir/(sid+'.json');require(not destination.exists(),'installed policy must be fresh')
        python=str(Path(sys.executable).resolve())
        receipt=json.loads(run([python,'-I',str(installed),'install','--source',str(source),'--destination',str(destination)]))
        report['receipt']=receipt
        log=(args.output/'root-broker.log').open('xb')
        broker=subprocess.Popen([python,'-I',str(installed),'serve','--policy',str(destination)],stdin=subprocess.DEVNULL,
            stdout=log,stderr=log,env={'PATH':'/usr/bin:/bin','LANG':'C'},start_new_session=True)
        deadline=time.monotonic()+15
        while not (state/'broker-instance.json').exists():
            require(broker.poll() is None and time.monotonic()<deadline,'actual ROOT broker startup failed or timed out');time.sleep(.02)
        outcome=run(['runuser','-u','doxa','--',str(client),str(caller/'state'),str(caller/'source'),sid,str(caller/'client-report.json')],
            timeout=360,environment=env)
        report['client_stdout']=outcome.decode();report['client']=json.loads((caller/'client-report.json').read_text())
        require(report['client']['runtime_prepared'] and report['client']['resumed_and_stopped'],'actual Runtime proof incomplete')
        for cid in [report['client']['cid'],report['client']['resumed_cid']]:
            require(re.fullmatch(r'[0-9a-f]{64}',cid),'actual Runtime container identity invalid');owned_cids.append(cid)
            engine.request(module.Deadline(10),'GET',f'/containers/{cid}/json',statuses=(404,))
            report['cleanup'][cid]='404'
        engine.live();require(engine.pid==report['engine_pid_before'] and engine.socket_identity==tuple(report['engine_socket_before']),'shared Engine changed during proof')
        report['status']='passed'
    except BaseException as cause:
        report['status']='failed';report['error']=str(cause);raise
    finally:
        if broker is not None and broker.poll() is None:
            try:
                run(['runuser','-u','doxa','--',str(Path(sys.executable).resolve()),'-I',str(installed),'client','--session-id',sid,'--op','stop'],environment=env)
            except BaseException as cause:report['cleanup']['stop_error']=str(cause)
            os.killpg(broker.pid,signal.SIGTERM)
            try:broker.wait(timeout=5)
            except subprocess.TimeoutExpired:os.killpg(broker.pid,signal.SIGKILL);broker.wait(timeout=5)
        if log is not None:log.close()
        private=state/'manifest.json'
        # Diagnostic recovery only touches our manifest's exact labelled CID.
        # It never starts/accepts a worker after a refused native gate.
        if private.exists():
            row=json.loads(private.read_text());cid=row.get('cid')
            if isinstance(cid,str) and re.fullmatch(r'[0-9a-f]{64}',cid) and row.get('session_id')==sid:
                status,info=engine.request(module.Deadline(10),'GET',f'/containers/{cid}/json',statuses=(200,404))
                if status==200:
                    labels=info.get('Config',{}).get('Labels',{})
                    require(labels.get('io.doxa.root-session')==sid and labels.get('io.doxa.root-policy')==report.get('receipt',{}).get('policy_sha256'),
                            'cleanup container label/policy differs')
                    engine.request(module.Deadline(10),'DELETE',f'/containers/{cid}?force=1',statuses=(204,))
                engine.request(module.Deadline(10),'GET',f'/containers/{cid}/json',statuses=(404,));report['cleanup'][cid]='404'
        write_new(args.output/'report.json',json.dumps(report,sort_keys=True,indent=2).encode()+b'\n')

if __name__=='__main__':main()
