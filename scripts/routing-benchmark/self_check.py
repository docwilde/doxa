#!/usr/bin/env python3
"""Explicit nonbillable Docker validation of trusted fixtures and buggy starters.

Never imports a fixture module on the host. Each grade uses a new rootless
network-none container. This is corpus validation, not model-quality evidence.
"""
import argparse
import json
import os
from pathlib import Path
import benchmark as b

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('output',type=Path);p.add_argument('--docker-host',required=True);p.add_argument('--image',required=True)
    args=p.parse_args();root=args.output.absolute();b.private_directory(root.parent)
    root.mkdir(mode=0o700);rows=[]
    for row in b.corpus()['tasks']:
        item={'task_id':row['id']}
        for arm in ('starter','reference'):
            state=root/(row['id']+'-'+arm);b.prepare(row['id'],state,reference=arm=='reference')
            report=b.grade(state,args.docker_host,args.image)
            item[arm]={'passed':report['passed'],'result_directory':report['result_directory']}
        item['valid']=not item['starter']['passed'] and item['reference']['passed']
        rows.append(item);print(json.dumps(item),flush=True)
    report={'schema':'doxa-repair-corpus-self-check-v1','tasks':rows,'valid':all(row['valid'] for row in rows),
        'model_calls':0,'model_quality_validated':False,'candidate_code_executed_on_host':False,'image':args.image}
    b.create(root/'summary.json',report)
    if not report['valid']:raise SystemExit(1)

if __name__=='__main__':main()
