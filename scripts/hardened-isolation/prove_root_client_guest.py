#!/usr/bin/env python3
"""Actual UID1000 client -> ROOT broker proof, disposable guest only.

Creates one describe-only broker/policy under its unique proof scope. No Engine
API, container, provider, quota installation or workstation service is touched.
"""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

spec = importlib.util.spec_from_file_location("root_broker",Path(__file__).with_name("root_broker.py"))
b=importlib.util.module_from_spec(spec);spec.loader.exec_module(b)


def run_proof(sid):
    b.require(os.geteuid()==0,"guest client proof requires ROOT")
    script=Path(__file__).with_name("root_broker.py").resolve()
    runtime=Path("/run/doxa/hardened")/sid
    runtime.mkdir(mode=0o755)
    policy_dir=Path("/etc/doxa/root-client-proof")
    policy_dir.mkdir(mode=0o755,exist_ok=True)
    source=policy_dir/(sid+".source.json"); installed=policy_dir/(sid+".json")
    h="1"*64
    p={"version":1,"session_id":sid,"caller_uid":1000,
       "engine":{"uid":1001,"gid":1001,"socket":"/run/user/1001/doxa-proof/docker.sock","exe_path":"/usr/bin/dockerd",
                 "exe_sha256":b.sha_file("/usr/bin/dockerd"),"config_path":"/etc/doxa-proof/engine-daemon.json",
                 "config_sha256":b.sha(b.read_regular("/etc/doxa-proof/engine-daemon.json")),"api_version":"1.51"},
       "root":"/srv/doxa/"+sid,"image":"sha256:"+h,"worker":{"path":b.WORKER,"sha256":h},
       "providers":{"claude":{"path":"/usr/local/bin/claude","sha256":h,"artifacts":{"/usr/local/bin/claude":h},
                                  "egress":["api.anthropic.com"],"proxy_port":3128,"environment_keys":[]}},
       "authority":{"path":str(Path("/usr/bin/false").resolve()),"sha256":b.sha_file(Path("/usr/bin/false").resolve())},
       "broker":{"path":str(script),"sha256":b.sha_file(script)},
       "limits":{"memory_bytes":1073741824,"cpu_quota":100000,"cpu_period":100000,"pids":64,"disk_bytes":1073741824,
                 "output_bytes":1048576,"stdin_bytes":1048576,"deadline_seconds":10,"operation_seconds":10}}
    source.write_bytes(b.compact(p));source.chmod(0o644)
    b.install(source,installed)
    server=subprocess.Popen([sys.executable,"-I",str(script),"serve","--policy",str(installed)],
                            stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE,
                            env={"PATH":"/usr/bin:/bin","LANG":"C"})
    try:
        end=time.monotonic()+5
        while not (runtime/"broker-instance.json").exists():
            b.require(time.monotonic()<end and server.poll() is None,"ROOT proof broker failed to publish instance")
            time.sleep(.02)
        own_instance=json.loads((runtime/"broker-instance.json").read_bytes())
        rd,wr=os.pipe();child=os.fork()
        if child==0:
            os.close(rd);os.setgroups([]);os.setgid(1000);os.setuid(1000)
            result={"client_uid":os.getuid(),"root_broker_pid":server.pid,"root_instance_pid":own_instance["pid"]}
            try:
                with open(f"/proc/{server.pid}/exe","rb") as f:f.read(1)
                result["root_proc_exe_errno"]=None
            except OSError as e:result["root_proc_exe_errno"]=e.errno
            result["root_proc_stat_start_time"]=b.process_start_time(server.pid)
            result["root_instance_start_time"]=own_instance["start_time"]
            b.require(result["root_proc_stat_start_time"]==result["root_instance_start_time"],"actual UID1000 ROOT start time mismatch")
            client=subprocess.run([sys.executable,"-I",str(script),"client","--session-id",sid,"--op","describe"],
                                  stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=5,
                                  env={"PATH":"/usr/bin:/bin","LANG":"C"})
            result["client_exit"]=client.returncode
            result["reply"]=json.loads(client.stdout) if client.returncode==0 else None
            result["diagnostic"]=client.stderr.decode()[:256] if client.returncode else None
            os.write(wr,b.compact(result));os.close(wr);os._exit(0)
        os.close(wr);data=bytearray()
        while chunk:=os.read(rd,65536):data.extend(chunk)
        os.close(rd);os.waitpid(child,0)
        result=json.loads(data)
        b.require(result["client_exit"]==0,"actual UID1000 broker client failed: "+str(result["diagnostic"]))
        b.require(result["reply"]["status"]=="unprepared" and result["reply"]["cid"] is None,"describe-only proof unexpectedly prepared")
        result.update(scope="actual-unprivileged-client-root-instance-attestation",admissible_as_hardened=False,
                      root_owned_instance=True,fresh_root_nonce_challenge=True,no_engine_api_calls=True)
        print(json.dumps(result,sort_keys=True,indent=2))
    finally:
        server.terminate()
        try:server.wait(timeout=5)
        except subprocess.TimeoutExpired:server.kill();server.wait(timeout=2)
        if server.stderr:server.stderr.close()


def main():
    sid="root-client-proof-"+str(os.getpid())
    try:run_proof(sid)
    finally:
        runtime=Path("/run/doxa/hardened")/sid
        if runtime.exists():shutil.rmtree(runtime)
        for suffix in (".source.json",".json"):
            (Path("/etc/doxa/root-client-proof")/(sid+suffix)).unlink(missing_ok=True)


if __name__=="__main__":main()
