#!/usr/bin/env python3
"""Fixed read-only exec-clean helper; no signer state/FD is passed here.

The root broker executes this through a pinned nsenter and Python, enters one
held analyzer user-namespace FD, retains the host mount/PID namespaces, then
hashes a single held /proc/PID/exe FD. It never executes the analyzer file.
"""
import hashlib
import json
import os
import sys


def main():
    if len(sys.argv)!=5: raise RuntimeError('fixed reader arguments required')
    pid,userns,mountns,pidns=map(int,sys.argv[1:])
    if pid<=0: raise RuntimeError('invalid analyzer PID')
    namespaces={name:os.stat('/proc/self/ns/'+name).st_ino for name in ('user','mnt','pid')}
    if namespaces!={'user':userns,'mnt':mountns,'pid':pidns}:
        raise RuntimeError('reader namespace identity differs')
    descriptor=os.open(f'/proc/{pid}/exe',os.O_RDONLY|os.O_CLOEXEC|os.O_NONBLOCK)
    try:
        before=os.fstat(descriptor)
        if before.st_size>256*1024*1024:raise RuntimeError('analyzer executable size bound')
        hasher=hashlib.sha256();total=0
        while chunk:=os.read(descriptor,65536):
            total+=len(chunk)
            if total>256*1024*1024:raise RuntimeError('analyzer executable read bound')
            hasher.update(chunk)
        after=os.fstat(descriptor)
        fields=('st_dev','st_ino','st_size','st_mtime_ns','st_ctime_ns','st_mode')
        if any(getattr(before,name)!=getattr(after,name)for name in fields)or total!=before.st_size:
            raise RuntimeError('held analyzer executable changed during hashing')
        # A live PID can exec a different file while the held FD remains
        # unchanged. Compare the current proc-exe mapping with the held file.
        current=os.open(f'/proc/{pid}/exe',os.O_RDONLY|os.O_CLOEXEC|os.O_NONBLOCK)
        try:
            mapped=os.fstat(current)
            if any(getattr(before,name)!=getattr(mapped,name)for name in fields):
                raise RuntimeError('analyzer executable mapping changed during hashing')
        finally:os.close(current)
        print(json.dumps({'sha256':hasher.hexdigest(),'dev':before.st_dev,'ino':before.st_ino,
            'size':before.st_size,'mtime_ns':before.st_mtime_ns,'ctime_ns':before.st_ctime_ns,
            'mode':before.st_mode,'namespaces':namespaces},sort_keys=True))
    finally:os.close(descriptor)


if __name__=='__main__':main()
