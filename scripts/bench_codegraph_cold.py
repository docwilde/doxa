#!/usr/bin/env python3
"""Linux source-page cold-read benchmark, no system-wide cache drop.

Use only a trusted immutable fixture owned by the current user. Every regular
Rust/Python input is fsynced, POSIX_FADV_DONTNEED evicted, and independently
measured with mincore before each end-to-end CLI query. Git metadata, binaries
and directory caches remain uncontrolled. Do not call this full-system cold.
"""
import argparse
import ctypes
import hashlib
import json
import math
import mmap
import os
import pathlib
import statistics
import subprocess
import time


def residency(path):
    size=path.stat().st_size
    if not size: return (0,0)
    fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW)
    libc=ctypes.CDLL(None,use_errno=True)
    libc.mmap.restype=ctypes.c_void_p
    address=libc.mmap(None,size,0,mmap.MAP_SHARED,fd,0)
    try:
        if address==ctypes.c_void_p(-1).value: raise OSError(ctypes.get_errno(),'mmap')
        pages=math.ceil(size/os.sysconf('SC_PAGE_SIZE'))
        vector=(ctypes.c_ubyte*pages)()
        if libc.mincore(ctypes.c_void_p(address),ctypes.c_size_t(size),vector):
            raise OSError(ctypes.get_errno(),'mincore')
        return sum(value&1 for value in vector),pages
    finally:
        libc.munmap(ctypes.c_void_p(address),ctypes.c_size_t(size));os.close(fd)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--binary',required=True,type=pathlib.Path)
    parser.add_argument('--root',required=True,type=pathlib.Path)
    parser.add_argument('--runs',default=5,type=int)
    args=parser.parse_args()
    if not 1<=args.runs<=10: parser.error('runs1..10')
    root=args.root.resolve(strict=True)
    paths=subprocess.check_output(['git','-C',str(root),'ls-files','--cached','--others','--exclude-standard','-z'],timeout=10)
    sources=[root/os.fsdecode(name) for name in paths.split(b'\0') if name and pathlib.Path(os.fsdecode(name)).suffix in ('.rs','.py')]
    if not sources or len(sources)>20000: parser.error('fixture needs1..20000 sources')
    for path in sources:
        if path.is_symlink() or path.stat().st_uid!=os.getuid(): parser.error('use owned regular fixture sources')
    before={str(path.relative_to(root)):hashlib.sha256(path.read_bytes()).hexdigest() for path in sources}
    samples=[]
    for _ in range(args.runs):
        for path in sources:
            fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW)
            try:
                os.fsync(fd);os.posix_fadvise(fd,0,0,os.POSIX_FADV_DONTNEED)
            finally: os.close(fd)
        counts=[residency(path) for path in sources]
        resident=sum(item[0] for item in counts);pages=sum(item[1] for item in counts)
        if resident: raise RuntimeError(f'source eviction incomplete: {resident}/{pages} pages remain')
        started=time.perf_counter()
        result=subprocess.run([str(args.binary.resolve()),'--root',str(root),'symbol','__cold_fixture_missing__'],capture_output=True,timeout=30,check=True)
        elapsed=(time.perf_counter()-started)*1000
        if len(result.stdout)>65536: raise RuntimeError('reply over64KiB')
        answer=json.loads(result.stdout)
        if answer['rows'] or answer['scan_input_sha256'] is None or answer['python_scan_input_sha256'] is None:
            raise RuntimeError('query does not have complete no-hit source inventory')
        samples.append({'elapsed_ms':elapsed,'resident_pages_before':resident,'pages':pages,
            'parsed_rust_files':answer['coverage']['parsed_rust_files'],
            'parsed_python_files':answer['coverage']['parsed_python_files']})
    after={str(path.relative_to(root)):hashlib.sha256(path.read_bytes()).hexdigest() for path in sources}
    if before!=after: raise RuntimeError('fixture source changed')
    times=sorted(row['elapsed_ms'] for row in samples)
    print(json.dumps({'benchmark':'source_page_cold_cli_scan','root':str(root),
        'controls':'all source pages evicted and mincore=0 before every scan; other caches uncontrolled',
        'source_inventory_sha256':hashlib.sha256(json.dumps(before,sort_keys=True).encode()).hexdigest(),
        'samples':samples,'p50_ms':statistics.median(times),'p95_ms':times[math.ceil(.95*len(times))-1]},indent=2))

if __name__=='__main__': main()
