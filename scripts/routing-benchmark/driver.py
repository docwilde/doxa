#!/usr/bin/env python3
"""Trusted container-side protocol driver. Contains no oracle answers.

Candidate imports happen only inside the offline, credential-free container.
The host grader compares JSON values outside that container. A candidate cannot
pass merely by printing unittest success or returning process exit zero.
"""
import concurrent.futures
import importlib
import json
import os
from pathlib import Path
import subprocess
import sys
import threading

ROOT='/run/benchmark/state'

def load_app():
    sys.path.insert(0,'/workspace')
    return importlib.import_module('app')

def call(function,value):
    before=json.dumps(value,sort_keys=True,allow_nan=False)
    try:
        result=function(value)
        # Error objects are reserved for the adapter's exception representation.
        # Returning such an object is not evidence that the API raised ValueError.
        if type(result) is dict and set(result)=={'error'}:result={'error':'returned_error_object'}
    except ValueError:result={'error':'ValueError'}
    except (KeyError,TypeError) as error:result={'error':type(error).__name__}
    if json.dumps(value,sort_keys=True,allow_nan=False)!=before:return {'error':'input_mutated'}
    return result

def stateful(value):
    service=load_app().Service(ROOT)
    return [call(service.apply,operation) for operation in value['ops']]

def main():
    kind=sys.argv[1];value=json.load(sys.stdin)
    os.makedirs(ROOT,exist_ok=True)
    if kind=='pure':result=call(load_app().handle,value)
    elif kind in ('stateful','phase'):result=stateful(value)
    elif kind=='restart':
        result=[]
        for phase in value['phases']:
            env={k:os.environ[k] for k in ('PATH','HOME','TMPDIR','LANG') if k in os.environ}
            if phase.get('crash_stage'):env['BENCHMARK_CRASH_STAGE']=phase['crash_stage']
            child=subprocess.run([sys.executable,'-I','-B',str(Path(__file__).resolve()),'phase'],
                input=json.dumps({'ops':phase['ops']}),capture_output=True,text=True,
                timeout=10,env=env)
            if child.returncode==86:result.append({'crashed':True})
            elif child.returncode==0:result.append(json.loads(child.stdout))
            else:raise RuntimeError('candidate phase failed')
    elif kind=='parallel':
        app=load_app();service=app.Service(ROOT)
        setup=[call(service.apply,operation) for operation in value['setup']]
        operations=value['parallel']
        if not 1<=len(operations)<=24:raise ValueError('parallel bound')
        barrier=threading.Barrier(len(operations))
        def apply(operation):
            barrier.wait(timeout=5)
            return call(service.apply,operation)
        with concurrent.futures.ThreadPoolExecutor(max_workers=len(operations)) as pool:
            replies=list(pool.map(apply,operations))
        # Reopen the service to check durable state rather than an in-memory cache.
        probe=call(app.Service(ROOT).apply,value['probe'])
        count=sum(reply=='accepted' for reply in replies)
        result={'setup':setup,'accepted':count,'rejected':len(replies)-count,'probe':probe}
    else:raise ValueError('unknown driver')
    print(json.dumps(result,separators=(',',':'),allow_nan=False))

if __name__=='__main__':main()
