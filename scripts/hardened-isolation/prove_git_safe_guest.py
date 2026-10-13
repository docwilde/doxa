#!/usr/bin/env python3
"""Executed ROOT-generated Git metadata/filter regression, private guest only.

This tests actual Git as UID1001. It installs no quota policy, contacts no Engine,
starts no provider and makes no hardened-admission claim. Every fixture byte is
under its unique /srv/git-safe-proof-PID directory and is removed afterward.
"""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import threading

spec=importlib.util.spec_from_file_location("root_broker",Path(__file__).with_name("root_broker.py"))
b=importlib.util.module_from_spec(spec);spec.loader.exec_module(b)


def main():
    b.require(os.geteuid()==0,"guest Git proof requires ROOT")
    root=Path("/srv")/("git-safe-proof-"+str(os.getpid()))
    root.mkdir(mode=0o750);os.chown(root,0,1001)
    workspace=root/"checkout";workspace.mkdir(mode=0o770);os.chown(workspace,1001,1001)
    mount=root/"broker";mount.mkdir(mode=0o750);os.chown(mount,0,1001)
    home=root/"home";home.mkdir(mode=0o770);os.chown(home,1001,1001)
    p={"session_id":root.name,"root":str(root),"engine":{"uid":1001,"gid":1001},"limits":{"disk_bytes":128*1024*1024}}
    runtime=Path(b.policy_paths(p)["state_dir"]);runtime.mkdir(mode=0o755)
    base={"PATH":"/usr/bin:/bin","LANG":"C","HOME":str(home),"TMPDIR":str(home),
          "GIT_CONFIG_NOSYSTEM":"1","GIT_CONFIG_GLOBAL":"/dev/null",
          "GIT_AUTHOR_NAME":"fixture","GIT_AUTHOR_EMAIL":"fixture@example.invalid",
          "GIT_COMMITTER_NAME":"fixture","GIT_COMMITTER_EMAIL":"fixture@example.invalid"}
    def drop():os.setgroups([1001]);os.setgid(1001);os.setuid(1001)
    def git(args,input=None,safe=None,expected=0):
        env=dict(base)
        if safe is not None:
            env.update(b.git_environment(str(safe)));env["GIT_WORK_TREE"]=str(workspace)
            args=b.git_arguments(args)
            # The production path is exactly /workspace. This isolated host-Git
            # proof uses its private canonical worktree with the same ROOT trust.
            index=args.index("safe.directory=/workspace");args[index]="safe.directory="+str(workspace)
        result=subprocess.run(["/usr/bin/git",*args],cwd=workspace,env=env,input=input,
                              stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=8,preexec_fn=drop)
        if expected is not None:b.require(result.returncode==expected,"Git fixture failed: "+result.stderr.decode()[:256])
        return result
    def file(name,data):
        path=workspace/name;path.write_bytes(data);os.chown(path,1001,1001);path.chmod(0o600)
    def markers():return sorted(path.name for path in workspace.glob("marker-*"))
    def clear():
        for path in workspace.glob("marker-*"):path.unlink()
    try:
        git(["init"]);file("tracked.txt",b"one\n");file(".gitattributes",b"tracked.txt filter=escape\n")
        git(["add","."]);git(["commit","-m","fixture"])
        for key in ("clean","smudge"):
            git(["config","filter.escape."+key,"touch "+str(workspace/("marker-"+key))+"; cat"])
        file("tracked.txt",b"two\n")
        git(["diff","--no-color"]);unsafe_clean=markers();b.require("marker-clean" in unsafe_clean,"clean filter fixture did not execute");clear()
        patch=b"diff --git a/tracked.txt b/tracked.txt\n--- a/tracked.txt\n+++ b/tracked.txt\n@@ -1 +1 @@\n-two\n+three\n"
        git(["apply","-"],patch);unsafe_apply=markers()
        b.require({"marker-clean","marker-smudge"}<=set(unsafe_apply),"apply clean/smudge fixture did not execute");clear()
        file("tracked.txt",b"two\n")
        git(["config","filter.escape.process","touch "+str(workspace/"marker-process")+"; exit 1"])
        unsafe_process=git(["diff","--no-color"],expected=None)
        b.require(unsafe_process.returncode!=0 and markers()==["marker-process"],"process filter fixture did not execute");clear()
        source_index=b.sha_file(workspace/".git/index")
        copying=threading.Event();continue_build=threading.Event();build_done=threading.Event()
        built=[];build_errors=[];observations=[];original_write=os.write
        def slow_write(fd,data):
            if bytes(data).startswith(b"[core]"):
                copying.set();b.require(continue_build.wait(5),"guest slow-copy barrier timed out")
            return original_write(fd,data)
        def build():
            try:built.append(b.build_git_view(p,b.Deadline(8)))
            except BaseException as error:build_errors.append(error)
            finally:build_done.set()
        def audit():
            with b.git_metadata_lock(p,b.Deadline(2),exclusive=False):
                safe=mount/"git-safe";rows=list(safe.iterdir()) if safe.exists() else []
                for generation in rows:
                    b.require((generation/"config").is_file() and (generation/"info/attributes").read_bytes()==b"","audit saw partial metadata")
                    b.require(all(path.stat().st_mode&0o777==(0o550 if path.is_dir() else 0o440) for path in [generation,*generation.rglob("*")]),"audit saw mutable metadata")
                observations.append(len(rows))
        os.write=slow_write;producer=threading.Thread(target=build);producer.start()
        try:
            b.require(copying.wait(5),"private Git copy never reached barrier")
            for _ in range(10):audit()
            b.require(observations==[0]*10,"partial Git generation was visible to actual shared auditor")
            continue_build.set();b.require(build_done.wait(5),"atomic Git builder timed out");producer.join(1)
            b.require(not build_errors,"atomic Git build failed: "+str(build_errors))
            view,worker_view=built[0];audit();b.require(observations[-1]==1,"complete Git generation missing")
        finally:continue_build.set();producer.join(5);os.write=original_write
        tested=[]
        for args in (["diff","--no-color"],["status","--porcelain"],["diff","--cached","HEAD"],
                     ["ls-files","--cached"],["rev-parse","--verify","HEAD"],["merge-base","HEAD","HEAD"],
                     ["apply","--check","-"],["apply","-"]):
            git(args,patch if args[0]=="apply" else None,safe=view);tested.append(args)
            b.require(not markers(),"ROOT-generated Git metadata allowed an executable filter")
        b.require((workspace/"tracked.txt").read_bytes()==b"three\n","safe ordinary apply did not update worktree")
        b.require(b.sha_file(workspace/".git/index")==source_index,"safe Git mutated original index")
        infos=[view,*view.rglob("*")]
        b.require(all(path.lstat().st_uid==0 and path.lstat().st_gid==1001 and path.lstat().st_mode&0o777==(0o550 if path.is_dir() else 0o440) for path in infos),"ROOT Git metadata owner/mode differs")
        b.require(all(path.is_dir() or path.stat().st_nlink==1 for path in infos),"ROOT Git metadata hardlink")
        metadata_bytes=sum(path.stat().st_size for path in infos if path.is_file())
        denied=[]
        for args in (["apply","--index","-"],["apply","--cached","-"]):
            try:b.git_arguments(args)
            except b.Refused:denied.append(args)
        b.require(len(denied)==2,"snapshot index mutations admitted")
        retired=threading.Event();retirement_errors=[]
        def retire():
            try:b.retire_git_view(p,view,b.Deadline(2));retired.set()
            except BaseException as error:retirement_errors.append(error)
        with b.git_metadata_lock(p,b.Deadline(2),exclusive=False):
            held=os.open(view,os.O_RDONLY|os.O_DIRECTORY);identity=os.fstat(held)
            remover=threading.Thread(target=retire);remover.start()
            b.require(not retired.wait(.05) and view.stat().st_ino==identity.st_ino and (view/"config").exists(),"retirement invalidated held shared auditor")
            os.close(held)
        b.require(retired.wait(2),"retirement did not complete after shared audit released");remover.join(1)
        b.require(not retirement_errors,"retirement failed: "+str(retirement_errors));audit()
        b.require(observations[-1]==0 and not view.exists(),"retired generation remained published")
        lock=(runtime/"git-metadata.lock").stat()
        b.require(lock.st_uid==0 and lock.st_gid==0 and lock.st_nlink==1 and lock.st_mode&0o777==0o600,"actual ROOT metadata lock differs")
        result={"scope":"actual-root-readonly-git-metadata-and-filter-regression","admissible_as_hardened":False,
                "root_uid":os.geteuid(),"git_uid":1001,"git_version":git(["--version"]).stdout.decode().strip(),
                "unsafe_clean_markers":unsafe_clean,"unsafe_apply_markers":unsafe_apply,"unsafe_process_exit":unsafe_process.returncode,
                "unsafe_process_marker":"marker-process","safe_commands":tested,"safe_markers":markers(),"original_index_unchanged":True,
                "worktree_apply_changed_bytes":True,"root_metadata_readonly_single_link":True,"metadata_entries":len(infos),
                "metadata_bytes":metadata_bytes,"denied_index_mutations":denied,
                "shared_audit_generation_counts":observations,"held_reader_blocks_retirement":True,"root_metadata_lock_valid":True,
                "no_engine_api_calls":True,"no_quota_installation":True,"no_provider_calls":True,"worker_view":worker_view}
        print(json.dumps(result,sort_keys=True,indent=2))
    finally:shutil.rmtree(root);shutil.rmtree(runtime)


if __name__=="__main__":main()
