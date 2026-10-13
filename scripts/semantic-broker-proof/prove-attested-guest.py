#!/usr/bin/env python3
"""Explicit disposable-guest acceptance; never run on an owner's live host.

Requires provision-attested.sh's reviewed guest installation. Positive queries
use the unprivileged installed client and pinned production broker. Negative
fault injection imports the same broker without loading its signing key and
uses the actual distinct-UID Engine, containers and analyzer. It never signs
or promotes a fault-injected receipt. No shared Engine restart/config changes.
"""
import argparse
from contextlib import ExitStack
import importlib.util
import json
import os
import pathlib
import resource
import signal
import socket
import subprocess
import time
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from unittest.mock import patch


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=pathlib.Path,required=True)
    parser.add_argument('--repetitions',type=int,default=5)
    args=parser.parse_args()
    if os.geteuid()!=0 or not 1<=args.repetitions<=10:raise RuntimeError('disposable guest root required')
    spec=importlib.util.spec_from_file_location('broker','/usr/local/lib/doxa-semantic/attested_broker.py')
    b=importlib.util.module_from_spec(spec);spec.loader.exec_module(b)
    config_path=pathlib.Path('/var/lib/doxa-semantic/config.json')
    config=json.loads(config_path.read_text());pins=json.loads(pathlib.Path('/var/lib/doxa-semantic/operator-pins.json').read_text())
    config.update(config_sha256=b.sha_file(config_path),_config_path=str(config_path),_broker_sha256=b.sha_file(b.__file__))
    assert config['config_sha256']==pins['config_sha256'] and config['_broker_sha256']==pins['broker_sha256']
    b.validate_config(config)
    def expired(*_):raise RuntimeError('proof transaction deadline')
    signal.signal(signal.SIGALRM,expired)
    root='/home/doxa/semantic-source/plain'
    client=['runuser','-u','doxa','--','/usr/local/bin/doxa-codegraph-proof','--semantic-broker',
        '--root',root,'--socket',config['broker_socket'],'--pins','/var/lib/doxa-semantic/operator-pins.json',
        '--broker-key',pins['public_key_hex'],'--file','src/lib.rs','--edge','0','--candidate','0']
    results=[]
    args.output.parent.mkdir(parents=True,exist_ok=True)
    def record(name,passed,**facts):
        row={'case':name,'passed':passed,**facts};results.append(row)
        args.output.write_text(json.dumps({'scope':'disposable real Engine/analyzer; negatives never signed',
            'pins':pins,'results':results},sort_keys=True,indent=2)+'\n')
        print(json.dumps(row,sort_keys=True),flush=True)
        if not passed:raise RuntimeError('acceptance failed: '+name)
    def inspect_absent(cid):
        e=b.Engine(config)
        try:return e.request('GET','/containers/'+cid+'/json',acceptable=(404,))[0]==404
        finally:e.close()
    for repetition in range(args.repetitions):
        started=time.monotonic();run=subprocess.run(client,stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=70)
        answer=json.loads(run.stdout) if run.returncode==0 else {}
        receipt=answer.get('receipt',{})
        passed=run.returncode==0 and answer.get('binding')=='analyzer_resolved' and receipt.get('cleanup_removed') is True
        if passed:passed=inspect_absent(receipt['container_id']) and receipt['analyzer_settings']==b.ANALYZER_SETTINGS
        record('positive-'+str(repetition+1),passed,elapsed_ms=(time.monotonic()-started)*1000,
            receipt=receipt,stderr=run.stderr.decode(errors='replace')[:1024])
    base={'protocol':b.PROTOCOL,'root':root,'source_input_sha256':receipt['source_input_sha256'],'request':receipt['request']}
    original_engine=b.Engine
    def negative(name,engine_class=None,patches=(),extra=None,cleanup=True):
        challenge={**base,'nonce':os.urandom(32).hex()};local=dict(config)
        if extra:local.update(extra)
        started=time.monotonic()
        with ExitStack() as stack:
            if engine_class:stack.enter_context(patch.object(b,'Engine',engine_class))
            for replacement in patches:stack.enter_context(replacement)
            with b.deadline(50):outcome=b.transaction(local,challenge,b.compact(challenge))
        removed=inspect_absent(outcome['container_id']) if cleanup and outcome.get('container_id') else None
        passed=outcome['status']=='unknown' and outcome['cleanup_removed'] is cleanup
        if cleanup and outcome.get('container_id'):passed=passed and removed
        record(name,passed,elapsed_ms=(time.monotonic()-started)*1000,receipt=outcome,independent_absent=removed)
        return outcome
    class Drift(original_engine):
        def request(self,method,path,body=None,acceptable=(200,201,204,304,404)):
            if path.startswith('/containers/create?'):body['HostConfig']['PidsLimit']=128
            return super().request(method,path,body,acceptable)
    negative('actual-pids-policy-drift',Drift)
    class KernelLimitDrift(original_engine):
        def request(self,method,path,body=None,acceptable=(200,201,204,304,404)):
            code,value=super().request(method,path,body,acceptable)
            if path.endswith('/start'):
                cid=path.split('/')[2]
                _,started=super().request('GET','/containers/'+cid+'/json',acceptable=(200,))
                resource.prlimit(b.host_pid(self,started['State']['Pid']),resource.RLIMIT_NOFILE,(32,32))
            return code,value
    negative('actual-kernel-nofile-drift-with-matching-engine-metadata',KernelLimitDrift)
    immutable={}
    class MountDenial(original_engine):
        def request(self,method,path,body=None,acceptable=(200,201,204,304,404)):
            if path.startswith('/containers/create?'):self.immutable=body['HostConfig']['Mounts'][0]['Source']
            code,value=super().request(method,path,body,acceptable)
            if path.endswith('/start'):
                copied=pathlib.Path(self.immutable)/'src/lib.rs'
                client_write=subprocess.run(['runuser','-u','doxa','--','python3','-I','-c',f'open({str(copied)!r},"ab").write(b"x")'],capture_output=True,timeout=3)
                cid=path.split('/')[2]
                analyzer_write=subprocess.run(['runuser','-u','engine','--','env','XDG_RUNTIME_DIR=/run/user/1001',
                    'docker','--host','unix://'+config['engine_socket'],'exec',cid,'sh','-c','printf x >> '+root+'/src/lib.rs'],capture_output=True,timeout=3)
                immutable.update(client_status=client_write.returncode,container_status=analyzer_write.returncode,
                    uid=copied.stat().st_uid,mode=oct(copied.stat().st_mode&0o777))
                if client_write.returncode==0 or analyzer_write.returncode==0:raise AssertionError('immutable source write succeeded')
                raise RuntimeError('proof stopped after actual immutable source denials')
            return code,value
    negative('actual-immutable-source-write-denial',MountDenial)
    record('immutable-source-owner-and-writes',immutable['uid']==0 and immutable['mode']=='0o444' and
        immutable['client_status']!=0 and immutable['container_status']!=0,**immutable)
    class Lost(original_engine):
        def request(self,method,path,body=None,acceptable=(200,201,204,304,404)):
            code,value=super().request(method,path,body,acceptable)
            if path.startswith('/containers/create?'):raise TimeoutError('injected lost reply AFTER actual creation')
            return code,value
    negative('actual-lost-create-response',Lost)
    class Malformed(original_engine):
        def request(self,method,path,body=None,acceptable=(200,201,204,304,404)):
            code,value=super().request(method,path,body,acceptable)
            return (code,{'Id':''}) if path.startswith('/containers/create?') else (code,value)
    negative('actual-empty-create-response',Malformed)
    # The attached FD remains an actual pinned daemon FD, but the disposable
    # fault wrapper rewrites its pre-inspect URL to a different actual CID.
    # Production's exact-CID check must reject that reply before upgrade/LSP.
    class OtherContainer(original_engine):
        def request(self,method,path,body=None,acceptable=(200,201,204,304,404)):
            if path.startswith('/containers/create?'):
                other={**body,'Labels':{}}
                _,made=super().request('POST','/containers/create?name=doxa-semantic-other-'+str(os.getpid()),other,(201,))
                self.other_cid=made['Id']
            return super().request(method,path,body,acceptable)
        def connect(self):
            connected=super().connect()
            if not getattr(self,'swap',False):return connected
            other=self.other_cid;self.swap=False
            class Swapped:
                def __getattr__(self,name):return getattr(connected,name)
                def sendall(self,data):
                    if data.startswith(b'GET '):
                        original=data.split(b' ')[1];data=data.replace(original,('/v1.51/containers/'+other+'/json').encode(),1)
                    return connected.sendall(data)
            return Swapped()
        def attach(self,cid):
            self.swap=True
            try:return super().attach(cid)
            finally:super().request('DELETE','/containers/'+self.other_cid+'?force=1&v=1',acceptable=(204,404))
    negative('actual-same-daemon-fd-wrong-container-id',OtherContainer)
    negative('actual-reader-pin-drift',extra={'reader_sha256':'00'*32})
    # These reviewed negative helper programs run through the REAL nsenter
    # transition, never load a signer, and can only cause an unsigned refusal.
    bad_reader=pathlib.Path('/usr/local/lib/doxa-semantic/proof-bad-reader.py')
    actual_children=[];original_popen=b.subprocess.Popen
    def tracking_popen(*args,**kwargs):
        child=original_popen(*args,**kwargs);actual_children.append(child);return child
    try:
        for name,program in [('output','import sys;sys.stdout.write("x"*4097);sys.stdout.flush()'),
                             ('timeout','import time;time.sleep(30)'),
                             ('namespace','print(\'{"sha256":"'+config['analyzer_sha256']+'","namespaces":{"user":0}}\')')]:
            bad_reader.write_text(program+'\n');bad_reader.chmod(0o644);actual_children.clear()
            negative('actual-reader-'+name,patches=(patch.object(b.subprocess,'Popen',tracking_popen),),
                extra={'reader_path':str(bad_reader),'reader_sha256':b.sha_file(bad_reader)})
            record('actual-reader-'+name+'-reaped',len(actual_children)==1 and actual_children[0].poll() is not None,
                child_status=actual_children[0].poll() if actual_children else None)
        bad_reader.write_text('import time;time.sleep(30)\n');actual_children.clear();cancel=[False]
        def cancel_popen(*args,**kwargs):child=tracking_popen(*args,**kwargs);cancel[0]=True;return child
        negative('actual-reader-cancel',patches=(patch.object(b.subprocess,'Popen',cancel_popen),),
            extra={'reader_path':str(bad_reader),'reader_sha256':b.sha_file(bad_reader),'_cancel':lambda:cancel[0]})
        record('actual-reader-cancel-reaped',len(actual_children)==1 and actual_children[0].poll() is not None,
            child_status=actual_children[0].poll() if actual_children else None)
    finally:bad_reader.unlink(missing_ok=True)
    original_lsp=b.Lsp
    class Busy(original_lsp):
        def send(self,value):
            super().send(value)
            if value.get('id')==3:
                body=b.compact({'jsonrpc':'2.0','method':'experimental/serverStatus','params':{'health':'ok','quiescent':False}})
                self.pending.extend(b'Content-Length: '+str(len(body)).encode()+b'\r\n\r\n'+body)
    negative('actual-analyzer-busy-notification',patches=(patch.object(b,'Lsp',Busy),))
    class ShutdownError(original_lsp):
        def send(self,value):
            super().send(value)
            if value.get('id')==4:
                body=b.compact({'jsonrpc':'2.0','id':4,'error':{'code':-32603,'message':'injected shutdown error'}})
                self.pending.extend(b'Content-Length: '+str(len(body)).encode()+b'\r\n\r\n'+body)
    negative('actual-analyzer-shutdown-error',patches=(patch.object(b,'Lsp',ShutdownError),))
    attached_client,caller=socket.socketpair()
    class Cancelled(original_lsp):
        def send(self,value):
            super().send(value)
            if value.get('method')=='initialized':caller.close()
    try:
        negative('actual-analyzer-client-disconnect',patches=(patch.object(b,'Lsp',Cancelled),),
            extra={'_client':attached_client,'_cancel':lambda:b.client_cancelled(attached_client)})
    finally:attached_client.close();caller.close()
    class Stopped(original_engine):
        def attach(self,cid):
            stream=super().attach(cid)
            _,info=self.request('GET','/containers/'+cid+'/json',acceptable=(200,))
            os.kill(b.host_pid(self,info['State']['Pid']),signal.SIGSTOP)
            return stream
    class ShortDeadline(original_lsp):
        def __init__(self,*args):super().__init__(*args);self.deadline=time.monotonic()+.2
    negative('actual-stopped-analyzer-deadline',Stopped,(patch.object(b,'Lsp',ShortDeadline),))
    class NoDelete(original_engine):
        def request(self,method,path,body=None,acceptable=(200,201,204,304,404)):
            if method=='DELETE':raise RuntimeError('injected actual-container cleanup refusal')
            return super().request(method,path,body,acceptable)
    leaked=negative('actual-cleanup-refusal',NoDelete,(patch.object(b,'Lsp',Busy),),cleanup=False)
    admin=original_engine(config)
    try:admin.request('DELETE','/containers/'+leaked['container_id']+'?force=1&v=1',acceptable=(204,404))
    finally:admin.close()
    record('exact-admin-cleanup-after-refusal',inspect_absent(leaked['container_id']),container_id=leaked['container_id'])
    # No shared socket mutation: alias the real socket, pin its identity, then
    # replace ONLY the private alias and require drift refusal.
    # Unix sockets cannot be hard-linked across the Engine's runtime tmpfs.
    alias=pathlib.Path(config['engine_socket']).parent/('codegraph-alias-'+str(os.getpid())+'.sock')
    alias.unlink(missing_ok=True);os.link(config['engine_socket'],alias)
    e=original_engine({**config,'engine_socket':str(alias)});replacement=None
    try:
        e.connect().close();alias.unlink();replacement=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM);replacement.bind(str(alias))
        try:e.live();error=None
        except Exception as failed:error=str(failed)
        record('actual-held-engine-socket-replaced',error is not None,error=error)
    finally:
        e.close()
        if replacement:replacement.close()
        alias.unlink(missing_ok=True)
    # Unprivileged adversary cannot read the signer, replace code/socket,
    # borrow a signer FD or connect directly to the reviewed Engine.
    broker_pid=int(subprocess.check_output(['systemctl','show','--property=MainPID','--value','doxa-semantic-proof.service']))
    for name,program in [
        ('signer-key-read',f'open({config["signing_key"]!r},"rb").read()'),
        ('broker-code-write',f'open({b.__file__!r},"ab").write(b"x")'),
        ('broker-socket-replace',f'import os;os.unlink({config["broker_socket"]!r})'),
        ('signer-fd-borrow',f'import os;os.listdir("/proc/{broker_pid}/fd")'),
        ('direct-engine-connect',f'import socket;s=socket.socket(socket.AF_UNIX);s.connect({config["engine_socket"]!r})')]:
        attempt=subprocess.run(['runuser','-u','doxa','--','python3','-I','-c',program],stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=3)
        record('actual-doxa-'+name,attempt.returncode!=0,exit_status=attempt.returncode,
            stderr=attempt.stderr.decode(errors='replace')[-512:])
    def fake_socket(uid,path,reply):
        program='''import os,socket,struct,sys
p=sys.argv[1];s=socket.socket(socket.AF_UNIX);s.bind(p);s.listen(1);print("READY",flush=True)
c,_=s.accept()
def exact(n):
    out=b""
    while len(out)<n:
        part=c.recv(n-len(out))
        if not part:return out
        out+=part
    return out
n=struct.unpack(">I",exact(4))[0];exact(n);body=bytes.fromhex(sys.argv[2]);c.sendall(struct.pack(">I",len(body))+body);c.close();s.close();os.unlink(p)
'''
        path.unlink(missing_ok=True)
        child=subprocess.Popen(['runuser','-u',uid,'--','python3','-I','-u','-c',program,str(path),reply.hex()],
            stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.PIPE,start_new_session=True)
        with b.deadline(3):assert child.stdout.readline()==b'READY\n'
        return child
    # Same Engine UID/socket ownership alone cannot impersonate reviewed dockerd.
    fake=pathlib.Path('/home/engine/semantic-fake.sock');process=fake_socket('engine',fake,b'{}')
    impostor=original_engine({**config,'engine_socket':str(fake)})
    try:
        try:impostor.connect();error=None
        except Exception as failed:error=str(failed)
        record('actual-same-engine-uid-fake-daemon',error is not None and 'fake Engine executable' in error,error=error)
    finally:
        impostor.close()
        if process.poll() is None:os.killpg(process.pid,signal.SIGKILL)
        process.wait();process.stdout.close();process.stderr.close();fake.unlink(missing_ok=True)
    forged=b.compact({'payload_hex':b.compact({'status':'attested'}).hex(),'signature_hex':'00'*64})
    fake=pathlib.Path('/home/doxa/t/semantic-fake-broker.sock')
    def rejected_fake(name,body,reason):
        child=fake_socket('doxa',fake,body)
        command=client.copy();command[command.index('--socket')+1]=str(fake)
        try:
            run=subprocess.run(command,capture_output=True,timeout=70);child.wait(timeout=3)
            error=run.stderr.decode(errors='replace')
            record(name,run.returncode!=0 and reason in error,exit_status=run.returncode,stderr=error[:1024])
        finally:
            if child.poll() is None:os.killpg(child.pid,signal.SIGKILL);child.wait()
            child.stdout.close();child.stderr.close();fake.unlink(missing_ok=True)
    rejected_fake('actual-doxa-fake-broker-signature',forged,'operator trust anchor')
    raw_challenge=b.compact({**base,'nonce':os.urandom(32).hex()})
    capture='''import socket,struct,sys
s=socket.socket(socket.AF_UNIX);s.connect(sys.argv[1]);body=sys.stdin.buffer.read();s.sendall(struct.pack(">I",len(body))+body)
def exact(n):
    out=b""
    while len(out)<n:out+=s.recv(n-len(out))
    return out
size=struct.unpack(">I",exact(4))[0];sys.stdout.buffer.write(exact(size));s.close()
'''
    signed=subprocess.run(['runuser','-u','doxa','--','python3','-I','-c',capture,config['broker_socket']],
        input=raw_challenge,capture_output=True,timeout=60,check=True).stdout
    envelope=json.loads(signed);payload=bytes.fromhex(envelope['payload_hex'])
    Ed25519PublicKey.from_public_bytes(bytes.fromhex(pins['public_key_hex'])).verify(bytes.fromhex(envelope['signature_hex']),payload)
    captured=json.loads(payload);assert captured['status']=='attested' and inspect_absent(captured['container_id'])
    args.output.with_name('signed-envelope.json').write_bytes(signed+b'\n')
    args.output.with_name('signed-challenge.json').write_bytes(raw_challenge+b'\n')
    rejected_fake('actual-real-signed-receipt-replay',signed,'differs from pin: nonce')
    relay='''import os,socket,struct,sys
p=sys.argv[1];s=socket.socket(socket.AF_UNIX);s.bind(p);s.listen(1);print("READY",flush=True);c,_=s.accept()
def exact(sock,n):
    out=b""
    while len(out)<n:
        part=sock.recv(n-len(out))
        if not part:raise RuntimeError("early EOF")
        out+=part
    return out
header=exact(c,4);body=exact(c,struct.unpack(">I",header)[0]);up=socket.socket(socket.AF_UNIX);up.connect(sys.argv[2]);up.sendall(header+body)
header=exact(up,4);body=exact(up,struct.unpack(">I",header)[0]);up.close();c.sendall(header+body+b"x");c.close();s.close();os.unlink(p)
'''
    fake.unlink(missing_ok=True)
    child=subprocess.Popen(['runuser','-u','doxa','--','python3','-I','-u','-c',relay,str(fake),config['broker_socket']],
        stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.PIPE,start_new_session=True)
    with b.deadline(3):assert child.stdout.readline()==b'READY\n'
    try:
        command=client.copy();command[command.index('--socket')+1]=str(fake)
        late=subprocess.run(command,capture_output=True,timeout=70);child.wait(timeout=3)
        record('actual-fresh-signed-receipt-trailing-byte',late.returncode!=0 and
            b'trailing bytes after signed broker receipt' in late.stderr,exit_status=late.returncode,
            stderr=late.stderr.decode(errors='replace')[:1024])
    finally:
        if child.poll() is None:os.killpg(child.pid,signal.SIGKILL);child.wait()
        child.stdout.close();child.stderr.close();fake.unlink(missing_ok=True)
    override=pathlib.Path(root)/'.cargo/config.toml';override.parent.mkdir();override.write_text('[build]\nrustc-wrapper="/project/unreviewed-command"\n')
    try:
        refused=subprocess.run(client,capture_output=True,timeout=70)
        record('actual-project-config-override',refused.returncode!=0,exit_status=refused.returncode,
            stderr=refused.stderr.decode(errors='replace')[:1024])
    finally:override.unlink();override.parent.rmdir()
    snapshots=list(pathlib.Path(config['snapshot_parent']).iterdir())
    record('no-retained-snapshots',not snapshots,count=len(snapshots))


if __name__=='__main__':main()
