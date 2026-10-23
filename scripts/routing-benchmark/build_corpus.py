#!/usr/bin/env python3
"""Trusted fixture authoring; code strings are data, never imported or executed."""
import json
from pathlib import Path
from textwrap import dedent

TASKS=[]
RULES='''def integer(value):
    return type(value) is int and value >= 0

def require(ok):
    if not ok:
        raise ValueError("invalid input")
'''
def task(id,level,family,title,contract,source,cases,bug,*,driver='pure',helpers=None):
    files={'app.py':dedent(source).lstrip(),**(helpers or {})}
    filename,old,new=bug
    assert files[filename].count(old)==1,(id,old)
    starter={**files,filename:files[filename].replace(old,new)}
    TASKS.append({'id':id,'difficulty':level,'family':family,'title':title,
        'split':'holdout' if family in {'json-decoding','structured-redaction','approval-evidence','idempotency','crash-checkpoint','session-replay','migration'} else 'development',
        'driver':driver,'contract':contract,'editable_paths':list(files),'starter':starter,
        'reference':files,'public_cases':cases[:1],'evaluator_cases':cases[1:]})
def case(input,expected):return {'input':input,'expected':expected}

task('easy-config','easy','config','Validate listener configuration',
 'Repair handle(config): return {host,port,tls}, defaulting to127.0.0.1,8080,false. Reject unknown keys, nonobject input, empty/nonstring host, non-int port outside1..65535 (including bool), and non-bool tls with ValueError. Do not mutate input.', '''
def handle(value):
    if type(value) is not dict or set(value)-{'host','port','tls'}:
        raise ValueError('invalid config')
    result={'host':'127.0.0.1','port':8080,'tls':False,**value}
    if not isinstance(result['host'],str) or not result['host']:
        raise ValueError('invalid host')
    if type(result['port']) is not int or not 1<=result['port']<=65535:
        raise ValueError('invalid port')
    if type(result['tls']) is not bool:
        raise ValueError('invalid tls')
    return result
''',[case({}, {'host':'127.0.0.1','port':8080,'tls':False}),case({'port':True},{'error':'ValueError'}),case({'port':65536},{'error':'ValueError'}),case({'tls':'yes'},{'error':'ValueError'}),case({'host':''},{'error':'ValueError'}),case({'host':'::1','port':443,'tls':True},{'host':'::1','port':443,'tls':True})],('app.py',"type(result['port']) is not int","not isinstance(result['port'],int)"))

task('easy-path','easy','path-scope','Enforce project path boundaries',
 'Repair handle({root,path}) to return whether an absolute POSIX path is root or a descendant. Normalize repeated separators and dot components; reject any dot-dot component, nonabsolute/nonstring root/path or NUL by returning false. Prefix collisions must be denied. No filesystem access.', '''
import posixpath
def handle(value):
    root,path=value['root'],value['path']
    if any(type(v) is not str or not v.startswith('/') or '\\x00' in v or '..' in v.split('/') for v in (root,path)):
        return False
    root=posixpath.normpath(root);path=posixpath.normpath(path)
    return path==root or path.startswith(root.rstrip('/')+'/')
''',[case({'root':'/project','path':'/project/a'},True),case({'root':'/project','path':'/project-other/a'},False),case({'root':'/p','path':'/p/../secret'},False),case({'root':'/p/.','path':'/p//a'},True),case({'root':'/','path':'/anything'},True),case({'root':'p','path':'/p/a'},False)],('app.py',"path==root or path.startswith(root.rstrip('/')+'/')","path.startswith(root)"))

task('easy-json','easy','json-decoding','Reject ambiguous JSON',
 'Repair handle(text) to decode JSON while rejecting duplicate object keys at every depth and NaN/Infinity/-Infinity with ValueError. Other valid JSON scalar/list/object values must remain intact.', '''
import json
def pairs(items):
    out={}
    for key,value in items:
        if key in out:raise ValueError('duplicate')
        out[key]=value
    return out
def constant(_):raise ValueError('nonfinite')
def handle(text):
    return json.loads(text,object_pairs_hook=pairs,parse_constant=constant)
''',[case('{"x":1}',{'x':1}),case('{"x":1,"x":2}',{'error':'ValueError'}),case('{"outer":{"x":0,"x":1}}',{'error':'ValueError'}),case('[NaN]',{'error':'ValueError'}),case('Infinity',{'error':'ValueError'}),case('[false,null,1.5]',[False,None,1.5])],('app.py',"json.loads(text,object_pairs_hook=pairs,parse_constant=constant)","json.loads(text)"))

task('easy-billing','easy','billing','Round token costs in integer micros',
 'Repair handle({input,output,input_rate,output_rate}) to return ceil((input*input_rate+output*output_rate)/1000000) using exact integers. All four fields are nonnegative ints; bool and negative/noninteger values raise ValueError. Rates are USD micros per million tokens.', '''
def handle(v):
    values=[v[k] for k in ('input','output','input_rate','output_rate')]
    if any(type(x) is not int or x<0 for x in values):raise ValueError('invalid')
    i,o,ir,orr=values
    return (i*ir+o*orr+999999)//1000000
''',[case({'input':1000,'output':0,'input_rate':300000,'output_rate':1200000},300),case({'input':1,'output':0,'input_rate':1,'output_rate':0},1),case({'input':9007199254740993,'output':7,'input_rate':300000,'output_rate':1200000},2702159776422307),case({'input':False,'output':0,'input_rate':1,'output_rate':0},{'error':'ValueError'}),case({'input':-1,'output':0,'input_rate':1,'output_rate':0},{'error':'ValueError'})],('app.py',"(i*ir+o*orr+999999)//1000000","int((i*ir+o*orr)/1000000)"))

task('easy-expiry','easy','expiry','Honor receipt validity boundaries',
 'Repair handle({valid_from,expires_at,now,spent,revoked}) to return a bool. Times must be nonnegative ints, valid_from<expires_at; spent/revoked must be actual bools. Malformed input raises ValueError. A valid receipt is usable exactly when valid_from<=now<expires_at and neither spent nor revoked.', '''
def handle(v):
    a,b,n=(v[k] for k in ('valid_from','expires_at','now'))
    if any(type(x) is not int or x<0 for x in (a,b,n)) or a>=b or any(type(v[k]) is not bool for k in ('spent','revoked')):raise ValueError('invalid')
    return a<=n<b and not v['spent'] and not v['revoked']
''',[case({'valid_from':5,'expires_at':9,'now':5,'spent':False,'revoked':False},True),case({'valid_from':5,'expires_at':9,'now':9,'spent':False,'revoked':False},False),case({'valid_from':5,'expires_at':9,'now':8,'spent':True,'revoked':False},False),case({'valid_from':0,'expires_at':9,'now':False,'spent':False,'revoked':False},{'error':'ValueError'})],('app.py',"a<=n<b","a<=n<=b"))

task('easy-redaction','easy','structured-redaction','Preserve typed JSON while redacting',
 'Repair handle({value,secrets}) to recursively replace each nonempty supplied secret in string values with [redacted]. Preserve JSON structure, bool/int/null and list order. If an object key contains a secret, raise ValueError rather than renaming it. Do not modify input. Secrets are synthetic fixture strings.', '''
def handle(v):
    secrets=v['secrets']
    def visit(x):
        if isinstance(x,str):
            for secret in secrets:
                if secret:x=x.replace(secret,'[redacted]')
            return x
        if isinstance(x,list):return [visit(y) for y in x]
        if isinstance(x,dict):
            if any(s and s in k for k in x for s in secrets):raise ValueError('unsafe key')
            return {k:visit(y) for k,y in x.items()}
        return x
    return visit(v['value'])
''',[case({'value':{'note':'demo-private','n':2},'secrets':['demo-private']},{'note':'[redacted]','n':2}),case({'value':{'nested':['say "demo-private"\nnext',False,None]},'secrets':['demo-private']},{'nested':['say "[redacted]"\nnext',False,None]}),case({'value':{'demo-private-key':'value'},'secrets':['demo-private']},{'error':'ValueError'})],('app.py',"if isinstance(x,list):return [visit(y) for y in x]","if isinstance(x,list):return x"))

task('intermediate-events','intermediate','event-replay','Make event application idempotent',
 'Repair Service.apply(op). configure(run) initializes one run, only once. add requires matching run, nonempty string id and nonnegative integer amount; return {total,seen} with sorted IDs. Repeated identical ID+amount is a no-op; conflicting duplicate, wrong run or malformed operation raises ValueError without changing state. snapshot reports state. Constructor takes an unused private root path.', '''
from rules import integer,require
class Service:
    def __init__(self,root):self.run=None;self.items={}
    def apply(self,v):
        if v['op']=='configure':
            require(self.run is None and type(v['run']) is str and bool(v['run']));self.run=v['run']
        elif v['op']=='add':
            require(self.run is not None and v['run']==self.run and type(v['id']) is str and bool(v['id']) and integer(v['amount']))
            if v['id'] in self.items:require(self.items[v['id']]==v['amount'])
            else:self.items[v['id']]=v['amount']
        elif v['op']!='snapshot':raise ValueError('unknown')
        return {'total':sum(self.items.values()),'seen':sorted(self.items)}
''',[case({'ops':[{'op':'configure','run':'r'},{'op':'add','run':'r','id':'a','amount':3}]},[{'total':0,'seen':[]},{'total':3,'seen':['a']}]),case({'ops':[{'op':'configure','run':'r'},{'op':'add','run':'r','id':'a','amount':3},{'op':'add','run':'r','id':'a','amount':3},{'op':'add','run':'r','id':'a','amount':4},{'op':'snapshot'}]},[{'total':0,'seen':[]},{'total':3,'seen':['a']},{'total':3,'seen':['a']},{'error':'ValueError'},{'total':3,'seen':['a']}]),case({'ops':[{'op':'configure','run':'r'},{'op':'add','run':'other','id':'x','amount':1},{'op':'snapshot'}]},[{'total':0,'seen':[]},{'error':'ValueError'},{'total':0,'seen':[]}])],('app.py',"if v['id'] in self.items:require(self.items[v['id']]==v['amount'])","if v['id'] in self.items:self.items[v['id']]+=v['amount']"),driver='stateful',helpers={'rules.py':RULES})

task('intermediate-graph','intermediate','dependency-graph','Schedule only satisfied dependencies',
 'Repair handle({tasks,completed,failed}). tasks maps IDs to dependency lists. rules.validate rejects unknown dependencies, duplicate edges and cycles; completed/failed must name known nodes and be disjoint. Return alphabetically sorted unfinished/unfailed tasks whose every dependency is completed. A failed prerequisite does not count as completed. Do not mutate input.', '''
from rules import validate
def handle(v):
    graph=v['tasks'];validate(graph)
    complete=set(v['completed']);failed=set(v['failed'])
    if (complete|failed)-set(graph) or complete&failed:raise ValueError('invalid state')
    return sorted(k for k,deps in graph.items() if k not in complete|failed and set(deps)<=complete)
''',[case({'tasks':{'a':[],'b':['a']},'completed':['a'],'failed':[]},['b']),case({'tasks':{'a':[],'b':['a']},'completed':[],'failed':['a']},[]),case({'tasks':{'a':['b'],'b':['a']},'completed':[],'failed':[]},{'error':'ValueError'}),case({'tasks':{'a':['missing']},'completed':[],'failed':[]},{'error':'ValueError'}),case({'tasks':{'a':[],'b':[],'c':['a','b']},'completed':['a'],'failed':[]},['b'])],('app.py',"set(deps)<=complete","set(deps)<=complete|failed"),helpers={'rules.py':'''def validate(graph):
    visited=set();active=set()
    def visit(k):
        if k in active:raise ValueError('cycle')
        if k in visited:return
        active.add(k)
        if len(set(graph[k]))!=len(graph[k]):raise ValueError('duplicate')
        for d in graph[k]:
            if d not in graph:raise ValueError('unknown')
            visit(d)
        active.remove(k);visited.add(k)
    for k in graph:visit(k)
'''})

task('intermediate-budget','intermediate','budget','Retain unknown reservations',
 'Repair Service.apply(op). configure sets positive integer limit once. reserve(id,amount) returns accepted/budget/held: positive integer amount and new string ID, except all new reserves return held after any unknown outcome. settle(id,actual) replaces its reservation with nonnegative integer actual<=reserved once. unknown(id) retains its full hold and blocks new reservations. Duplicate IDs, duplicate settlement and malformed operations raise ValueError without changing state. snapshot returns {held,unknown}.', '''
from rules import integer,require
class Service:
    def __init__(self,root):self.limit=None;self.rows={};self.unknown=False
    def apply(self,v):
        op=v['op']
        if op=='configure':require(self.limit is None and integer(v['limit']) and v['limit']>0);self.limit=v['limit'];return 'configured'
        if op=='snapshot':return {'held':sum(x[0] for x in self.rows.values()),'unknown':self.unknown}
        require(self.limit is not None)
        if op=='reserve':
            require(type(v['id']) is str and bool(v['id']) and v['id'] not in self.rows and integer(v['amount']) and v['amount']>0)
            if self.unknown:return 'held'
            if sum(x[0] for x in self.rows.values())+v['amount']>self.limit:return 'budget'
            self.rows[v['id']]=(v['amount'],'pending');return 'accepted'
        require(v['id'] in self.rows and self.rows[v['id']][1]=='pending')
        if op=='settle':
            require(integer(v['actual']) and v['actual']<=self.rows[v['id']][0]);self.rows[v['id']]=(v['actual'],'settled');return 'settled'
        if op=='unknown':self.unknown=True;return 'held'
        raise ValueError('unknown')
''',[case({'ops':[{'op':'configure','limit':10},{'op':'reserve','id':'a','amount':8},{'op':'snapshot'}]},['configured','accepted',{'held':8,'unknown':False}]),case({'ops':[{'op':'configure','limit':10},{'op':'reserve','id':'a','amount':8},{'op':'unknown','id':'a'},{'op':'reserve','id':'b','amount':1},{'op':'snapshot'}]},['configured','accepted','held','held',{'held':8,'unknown':True}]),case({'ops':[{'op':'configure','limit':10},{'op':'reserve','id':'a','amount':8},{'op':'settle','id':'a','actual':3},{'op':'reserve','id':'b','amount':7},{'op':'snapshot'}]},['configured','accepted','settled','accepted',{'held':10,'unknown':False}])],('app.py',"if op=='unknown':self.unknown=True;return 'held'","if op=='unknown':self.unknown=True;self.rows[v['id']]=(0,'unknown');return 'held'"),driver='stateful',helpers={'rules.py':RULES})

task('intermediate-evidence','intermediate','approval-evidence','Bind release to current evidence',
 'Repair handle({current,receipt,owner_release}). Current and receipt each have nonempty source,handoff,readback,confirmation strings and nonnegative integer turn. A release is true only when both records are valid, every field matches exactly and owner_release is actual true. Otherwise false. No runtime action is performed.', '''
from rules import valid
def handle(v):
    current,receipt=v['current'],v['receipt']
    return v['owner_release'] is True and valid(current) and valid(receipt) and current==receipt
''',[case({'current':{'source':'s1','turn':2,'handoff':'h','readback':'a','confirmation':'c'},'receipt':{'source':'s1','turn':2,'handoff':'h','readback':'a','confirmation':'c'},'owner_release':True},True),case({'current':{'source':'s2','turn':3,'handoff':'h','readback':'a','confirmation':'c'},'receipt':{'source':'s1','turn':2,'handoff':'h','readback':'a','confirmation':'c'},'owner_release':True},False),case({'current':{'source':'s1','turn':2,'handoff':'new','readback':'a','confirmation':'c'},'receipt':{'source':'s1','turn':2,'handoff':'h','readback':'a','confirmation':'c'},'owner_release':True},False),case({'current':{},'receipt':{},'owner_release':True},False)],('app.py',"current==receipt","current['readback']==receipt['readback']"),helpers={'rules.py':'''def valid(v):
    return type(v) is dict and set(v)=={'source','turn','handoff','readback','confirmation'} and type(v['turn']) is int and v['turn']>=0 and all(type(v[k]) is str and bool(v[k]) for k in ('source','handoff','readback','confirmation'))
'''})

task('intermediate-delegation','intermediate','network-policy','Delegate without granting worker internet',
 'Repair handle({worker,supervisor,request}). Actor records have profile and bool internet. Valid profile/internet pairs: docker-offline/false, docker-open/true, native/either. request is provider or web. Provider returns provider_only. A web request is direct if worker online, delegated if only supervisor online, otherwise needs_approval. Return {decision,worker} preserving the exact worker record; malformed profiles/flags/requests raise ValueError. Never infer general internet from provider connectivity.', '''
from rules import validate
def handle(v):
    validate(v['worker']);validate(v['supervisor'])
    worker=dict(v['worker']);request=v['request']
    if request=='provider':decision='provider_only'
    elif request=='web':
        decision='direct' if worker['internet'] else ('delegated' if v['supervisor']['internet'] else 'needs_approval')
    else:raise ValueError('unknown request')
    return {'decision':decision,'worker':worker}
''',[case({'worker':{'profile':'docker-offline','internet':False},'supervisor':{'profile':'docker-offline','internet':False},'request':'provider'},{'decision':'provider_only','worker':{'profile':'docker-offline','internet':False}}),case({'worker':{'profile':'docker-offline','internet':False},'supervisor':{'profile':'docker-open','internet':True},'request':'web'},{'decision':'delegated','worker':{'profile':'docker-offline','internet':False}}),case({'worker':{'profile':'docker-offline','internet':False},'supervisor':{'profile':'docker-offline','internet':False},'request':'web'},{'decision':'needs_approval','worker':{'profile':'docker-offline','internet':False}}),case({'worker':{'profile':'docker-offline','internet':True},'supervisor':{'profile':'native','internet':True},'request':'web'},{'error':'ValueError'})],('app.py',"return {'decision':decision,'worker':worker}","worker['internet']=worker['internet'] or decision=='delegated';return {'decision':decision,'worker':worker}"),helpers={'rules.py':'''def validate(v):
    p=v['profile'];n=v['internet']
    if type(n) is not bool or p not in ('docker-offline','docker-open','native') or (p=='docker-offline' and n) or (p=='docker-open' and not n):raise ValueError('invalid policy')
'''})

task('intermediate-idempotency','intermediate','idempotency','Cache requests without repeating uncertain effects',
 'Repair Service.apply(op). submit(key,payload,outcome) records one effect for new valid key; outcome is ok or unknown. Exact repeated key+payload returns cached ok/held without another effect, regardless of the repeated outcome argument. Changed payload for an existing key raises ValueError. snapshot returns effects and unique requests. unknown retains held state. keys/payload are nonempty strings. Constructor root is unused.', '''
from rules import require
class Service:
    def __init__(self,root):self.rows={};self.effects=0
    def apply(self,v):
        if v['op']=='snapshot':return {'effects':self.effects,'requests':len(self.rows)}
        require(v['op']=='submit' and all(type(v[k]) is str and bool(v[k]) for k in ('key','payload')) and v['outcome'] in ('ok','unknown'))
        key=v['key']
        if key in self.rows:
            require(self.rows[key][0]==v['payload']);return self.rows[key][1]
        self.effects+=1;result='ok' if v['outcome']=='ok' else 'held';self.rows[key]=(v['payload'],result);return result
''',[case({'ops':[{'op':'submit','key':'a','payload':'x','outcome':'ok'},{'op':'snapshot'}]},['ok',{'effects':1,'requests':1}]),case({'ops':[{'op':'submit','key':'a','payload':'x','outcome':'unknown'},{'op':'submit','key':'a','payload':'x','outcome':'ok'},{'op':'snapshot'}]},['held','held',{'effects':1,'requests':1}]),case({'ops':[{'op':'submit','key':'a','payload':'x','outcome':'ok'},{'op':'submit','key':'a','payload':'y','outcome':'ok'},{'op':'snapshot'}]},['ok',{'error':'ValueError'},{'effects':1,'requests':1}])],('app.py',"require(self.rows[key][0]==v['payload']);return self.rows[key][1]","require(self.rows[key][0]==v['payload']);self.effects+=1;return self.rows[key][1]"),driver='stateful',helpers={'rules.py':RULES})

STORAGE='''import json,os,sqlite3
def connect(root):
    os.makedirs(root,exist_ok=True)
    db=sqlite3.connect(os.path.join(root,'state.sqlite'),timeout=5,isolation_level=None)
    db.execute('CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT NOT NULL)')
    return db
def get(db,key,default):
    row=db.execute('SELECT value FROM state WHERE key=?',(key,)).fetchone()
    return json.loads(row[0]) if row else default
def put(db,key,value):
    db.execute('INSERT OR REPLACE INTO state VALUES (?,?)',(key,json.dumps(value,sort_keys=True)))
'''

task('difficult-outbox','difficult','idempotency','Preserve a durable outbox across restart',
 'Repair Service(root).apply(op) with real SQLite persistence. enqueue(id,payload) adds pending once; an exact repeat returns the existing status, conflicting payload raises ValueError. ack(id) changes pending->acked and is idempotent; unknown IDs raise ValueError. pending returns sorted pending IDs. The evaluator starts fresh Python processes against the same private root between phases. IDs/payloads are nonempty strings.', '''
from storage import connect,get,put
class Service:
    def __init__(self,root):self.db=connect(root)
    def apply(self,v):
        self.db.execute('BEGIN IMMEDIATE')
        try:
            rows=get(self.db,'rows',{});op=v['op']
            if op=='pending':result=sorted(k for k,r in rows.items() if r[1]=='pending')
            elif op=='enqueue':
                key,payload=v['id'],v['payload']
                if type(key) is not str or not key or type(payload) is not str or not payload:raise ValueError('invalid')
                if key in rows:
                    if rows[key][0]!=payload:raise ValueError('conflict')
                else:rows[key]=[payload,'pending']
                result=rows[key][1]
            elif op=='ack':
                if v['id'] not in rows:raise ValueError('unknown')
                rows[v['id']][1]='acked';result='acked'
            else:raise ValueError('unknown')
            put(self.db,'rows',rows);self.db.execute('COMMIT');return result
        except BaseException:self.db.execute('ROLLBACK');raise
''',[case({'phases':[{'ops':[{'op':'enqueue','id':'a','payload':'x'},{'op':'pending'}]}]},[['pending',['a']]]),case({'phases':[{'ops':[{'op':'enqueue','id':'a','payload':'x'}]},{'ops':[{'op':'ack','id':'a'}]},{'ops':[{'op':'enqueue','id':'a','payload':'x'},{'op':'pending'}]}]},[['pending'],['acked'],['acked',[]]]),case({'phases':[{'ops':[{'op':'enqueue','id':'a','payload':'x'}]},{'ops':[{'op':'enqueue','id':'a','payload':'y'},{'op':'pending'}]}]},[['pending'],[{'error':'ValueError'},['a']]])],('app.py',"if rows[key][0]!=payload:raise ValueError('conflict')","if rows[key][0]!=payload:raise ValueError('conflict')\n                    rows[key][1]='pending'"),driver='restart',helpers={'storage.py':STORAGE})

ATOMIC='''import json,os
def crash(stage):
    if os.environ.get('BENCHMARK_CRASH_STAGE')==stage:os._exit(86)
def write(root,value):
    target=os.path.join(root,'checkpoint.json');temp=target+'.new'
    with open(temp,'w') as f:
        json.dump(value,f);f.flush();os.fsync(f.fileno())
    crash('before_replace')
    os.replace(temp,target)
    fd=os.open(root,os.O_RDONLY|os.O_DIRECTORY)
    try:os.fsync(fd)
    finally:os.close(fd)
    crash('after_replace')
def read(root):
    try:
        with open(os.path.join(root,'checkpoint.json')) as f:return json.load(f)
    except FileNotFoundError:return {}
'''
task('difficult-checkpoint','difficult','crash-checkpoint','Keep checkpoints atomic across process death',
 'Repair storage.write used by Service.apply({op:write,value}) and read. A write must fsync a separate temporary file, invoke crash(before_replace), atomically replace checkpoint, fsync the directory, then invoke crash(after_replace). The injected crash terminates the actual child process. Before-replace crash preserves the old complete checkpoint; after-replace crash exposes the new complete checkpoint. Ignore orphan temporary files on read. Only standard-library files inside the supplied root may be written.', '''
from storage import write,read
class Service:
    def __init__(self,root):self.root=root
    def apply(self,v):
        if v['op']=='write':write(self.root,v['value']);return 'written'
        if v['op']=='read':return read(self.root)
        raise ValueError('unknown')
''',[case({'phases':[{'ops':[{'op':'write','value':{'n':1}}]},{'ops':[{'op':'read'}]}]},[['written'],[{'n':1}]]),case({'phases':[{'ops':[{'op':'write','value':{'n':1}}]},{'ops':[{'op':'write','value':{'n':2}}],'crash_stage':'before_replace'},{'ops':[{'op':'read'}]}]},[['written'],{'crashed':True},[{'n':1}]]),case({'phases':[{'ops':[{'op':'write','value':{'n':1}}]},{'ops':[{'op':'write','value':{'n':2}}],'crash_stage':'after_replace'},{'ops':[{'op':'read'}]}]},[['written'],{'crashed':True},[{'n':2}]])],('storage.py',"crash('before_replace')\n    os.replace(temp,target)","os.replace(temp,target)\n    crash('before_replace')"),driver='restart',helpers={'storage.py':ATOMIC})

task('difficult-budget','difficult','budget','Make concurrent reservations atomic and durable',
 'Repair Service(root).apply. configure(limit) writes the limit once; reserve(id,amount) atomically accepts a fresh unique ID iff all held+amount<=limit; positive integer amounts only. Return accepted or budget. snapshot returns {held,count}. The evaluator races up to24 real threads on one Service and then constructs a fresh Service to probe persistence. Failed reservations do not consume IDs. Duplicate successful IDs raise ValueError. SQLite transaction reads, checks and writes must be one unit.', '''
import threading
from storage import connect,get,put
class Service:
    def __init__(self,root):self.root=root;self.lock=threading.Lock()
    def apply(self,v):
        with self.lock:
            db=connect(self.root);db.execute('BEGIN IMMEDIATE')
            try:
                rows=get(db,'rows',{});limit=get(db,'limit',None)
                if v['op']=='configure':
                    if limit is not None or type(v['limit']) is not int or v['limit']<=0:raise ValueError('invalid')
                    put(db,'limit',v['limit']);result='configured'
                elif v['op']=='snapshot':result={'held':sum(rows.values()),'count':len(rows)}
                elif v['op']=='reserve':
                    key,n=v['id'],v['amount']
                    if limit is None or type(key) is not str or not key or key in rows or type(n) is not int or n<=0:raise ValueError('invalid')
                    if sum(rows.values())+n>limit:result='budget'
                    else:rows[key]=n;put(db,'rows',rows);result='accepted'
                else:raise ValueError('unknown')
                db.execute('COMMIT');return result
            except BaseException:db.execute('ROLLBACK');raise
            finally:db.close()
''',[case({'setup':[{'op':'configure','limit':10}],'parallel':[{'op':'reserve','id':str(i),'amount':3} for i in range(2)],'probe':{'op':'snapshot'}},{'setup':['configured'],'accepted':2,'rejected':0,'probe':{'held':6,'count':2}}),case({'setup':[{'op':'configure','limit':25}],'parallel':[{'op':'reserve','id':str(i),'amount':3} for i in range(24)],'probe':{'op':'snapshot'}},{'setup':['configured'],'accepted':8,'rejected':16,'probe':{'held':24,'count':8}}),case({'setup':[{'op':'configure','limit':10}],'parallel':[{'op':'reserve','id':'same','amount':3} for _ in range(12)],'probe':{'op':'snapshot'}},{'setup':['configured'],'accepted':1,'rejected':11,'probe':{'held':3,'count':1}})],('app.py',"sum(rows.values())+n>limit","n>limit"),driver='parallel',helpers={'storage.py':STORAGE})

task('difficult-replay','difficult','session-replay','Fence stale session events after restart',
 'Repair Service(root).apply with persisted {incarnation,next,readonly}. snapshot(id,next) installs a nonempty incarnation and nonnegative integer frontier, clearing readonly. event(id,seq) is applied only for current incarnation and exact next seq; older duplicates return duplicate without moving frontier. A forward gap or changed incarnation returns held, sets readonly and blocks all events until a fresh snapshot. Probe returns state. Fresh Python processes reuse the same durable root.', '''
from storage import connect,get,put
class Service:
    def __init__(self,root):self.root=root
    def apply(self,v):
        db=connect(self.root);db.execute('BEGIN IMMEDIATE')
        try:
            s=get(db,'state',{'incarnation':None,'next':0,'readonly':True})
            if v['op']=='snapshot':
                if type(v['id']) is not str or not v['id'] or type(v['next']) is not int or v['next']<0:raise ValueError('invalid')
                s={'incarnation':v['id'],'next':v['next'],'readonly':False};result='ready'
            elif v['op']=='probe':result=dict(s)
            elif v['op']=='event':
                if type(v['seq']) is not int or v['seq']<0:raise ValueError('invalid')
                if s['readonly']:result='held'
                elif v['id']!=s['incarnation'] or v['seq']>s['next']:s['readonly']=True;result='held'
                elif v['seq']<s['next']:result='duplicate'
                else:s['next']+=1;result='applied'
            else:raise ValueError('unknown')
            put(db,'state',s);db.execute('COMMIT');return result
        except BaseException:db.execute('ROLLBACK');raise
        finally:db.close()
''',[case({'phases':[{'ops':[{'op':'snapshot','id':'a','next':0},{'op':'event','id':'a','seq':0}]}]},[['ready','applied']]),case({'phases':[{'ops':[{'op':'snapshot','id':'a','next':0},{'op':'event','id':'a','seq':0}]},{'ops':[{'op':'event','id':'a','seq':0},{'op':'event','id':'a','seq':2},{'op':'event','id':'a','seq':1},{'op':'probe'}]}]},[['ready','applied'],['duplicate','held','held',{'incarnation':'a','next':1,'readonly':True}]]),case({'phases':[{'ops':[{'op':'snapshot','id':'a','next':1}]},{'ops':[{'op':'event','id':'b','seq':1},{'op':'probe'},{'op':'snapshot','id':'b','next':0},{'op':'event','id':'b','seq':0}]}]},[['ready'],['held',{'incarnation':'a','next':1,'readonly':True},'ready','applied']])],('app.py',"v['id']!=s['incarnation'] or v['seq']>s['next']","v['seq']>s['next']"),driver='restart',helpers={'storage.py':STORAGE})

task('difficult-dispatch','difficult','idempotency','Do not replay dispatch after an uncertain crash',
 'Repair Service(root).apply. submit(key,payload,crash=false) must durably claim the exact request BEFORE incrementing the effects counter. A crash=true invokes storage.crash(after_effect), which really kills the process after that increment but before marking success. Reopening a pending claim returns held and never repeats its effect. A settled exact duplicate returns ok; conflicting payload raises ValueError. probe returns {effects,requests}. All persistent fields are in SQLite; no actual network call occurs.', '''
from storage import connect,get,put,crash
class Service:
    def __init__(self,root):self.root=root
    def apply(self,v):
        db=connect(self.root)
        try:
            if v['op']=='probe':return {'effects':get(db,'effects',0),'requests':len(get(db,'rows',{}))}
            if v['op']!='submit' or type(v['key']) is not str or not v['key'] or type(v['payload']) is not str or not v['payload']:raise ValueError('invalid')
            db.execute('BEGIN IMMEDIATE');rows=get(db,'rows',{});key=v['key']
            if key in rows:
                if rows[key][0]!=v['payload']:db.execute('ROLLBACK');raise ValueError('conflict')
                db.execute('COMMIT');return 'ok' if rows[key][1]=='done' else 'held'
            rows[key]=[v['payload'],'pending'];put(db,'rows',rows);db.execute('COMMIT')
            put(db,'effects',get(db,'effects',0)+1)
            if v.get('crash'):crash('after_effect')
            rows[key][1]='done';put(db,'rows',rows);return 'ok'
        finally:db.close()
''',[case({'phases':[{'ops':[{'op':'submit','key':'a','payload':'x'},{'op':'probe'}]}]},[['ok',{'effects':1,'requests':1}]]),case({'phases':[{'ops':[{'op':'submit','key':'a','payload':'x','crash':True}],'crash_stage':'after_effect'},{'ops':[{'op':'submit','key':'a','payload':'x'},{'op':'probe'}]}]},[{'crashed':True},['held',{'effects':1,'requests':1}]]),case({'phases':[{'ops':[{'op':'submit','key':'a','payload':'x'}]},{'ops':[{'op':'submit','key':'a','payload':'y'},{'op':'submit','key':'a','payload':'x'},{'op':'probe'}]}]},[['ok'],[{'error':'ValueError'},'ok',{'effects':1,'requests':1}]])],('app.py',"rows[key]=[v['payload'],'pending'];put(db,'rows',rows);db.execute('COMMIT')","rows[key]=[v['payload'],'pending'];db.execute('COMMIT')"),driver='restart',helpers={'storage.py':STORAGE+"\ndef crash(stage):\n    if os.environ.get('BENCHMARK_CRASH_STAGE')==stage:os._exit(86)\n"})

task('difficult-migration','difficult','migration','Preserve authority through migration failures',
 'Repair Service(root).apply. init(receipts) sets a registry of unique nonempty receipt IDs once and starts docker-offline/source_active=true. migrate(receipt,target,outcome) requires an unspent registry receipt, target docker-offline/docker-open/native, and outcome ready/failed/unknown. Only ready consumes the receipt and changes profile; failed leaves profile/source and receipt intact. Unknown durably marks the receipt pending, keeps source active, and prohibits retrying that receipt even after restart. probe returns profile,source_active,spent,pending. Source must remain active throughout these simulated preparation outcomes; no real process is launched.', '''
from storage import connect,get,put
from policy import validate
class Service:
    def __init__(self,root):self.root=root
    def apply(self,v):
        db=connect(self.root);db.execute('BEGIN IMMEDIATE')
        try:
            s=get(db,'state',None)
            if v['op']=='init':
                if s is not None or type(v['receipts']) is not list or any(type(x) is not str or not x for x in v['receipts']) or len(set(v['receipts']))!=len(v['receipts']):raise ValueError('invalid')
                s={'registry':v['receipts'],'profile':'docker-offline','source_active':True,'spent':[],'pending':[]};result='initialized'
            elif v['op']=='probe':
                if s is None:raise ValueError('uninitialized')
                result={k:s[k] for k in ('profile','source_active','spent','pending')}
            elif v['op']=='migrate':
                validate(s,v)
                if v['outcome']=='ready':s['spent'].append(v['receipt']);s['profile']=v['target'];result='ready'
                elif v['outcome']=='unknown':s['pending'].append(v['receipt']);result='held'
                else:result='rolled_back'
            else:raise ValueError('unknown')
            put(db,'state',s);db.execute('COMMIT');return result
        except BaseException:db.execute('ROLLBACK');raise
        finally:db.close()
''',[case({'phases':[{'ops':[{'op':'init','receipts':['r']},{'op':'migrate','receipt':'r','target':'native','outcome':'ready'},{'op':'probe'}]}]},[['initialized','ready',{'profile':'native','source_active':True,'spent':['r'],'pending':[]}]]),case({'phases':[{'ops':[{'op':'init','receipts':['r']},{'op':'migrate','receipt':'r','target':'native','outcome':'failed'}]},{'ops':[{'op':'probe'},{'op':'migrate','receipt':'r','target':'docker-open','outcome':'ready'}]}]},[['initialized','rolled_back'],[{'profile':'docker-offline','source_active':True,'spent':[],'pending':[]},'ready']]),case({'phases':[{'ops':[{'op':'init','receipts':['r']},{'op':'migrate','receipt':'r','target':'native','outcome':'unknown'}]},{'ops':[{'op':'migrate','receipt':'r','target':'native','outcome':'ready'},{'op':'probe'}]}]},[['initialized','held'],[{'error':'ValueError'},{'profile':'docker-offline','source_active':True,'spent':[],'pending':['r']}]]),case({'phases':[{'ops':[{'op':'init','receipts':['r']},{'op':'migrate','receipt':'forged','target':'native','outcome':'ready'}]}]},[['initialized',{'error':'ValueError'}]])],('policy.py',"v['receipt'] in s['spent']+s['pending']","v['receipt'] in s['spent']"),driver='restart',helpers={'storage.py':STORAGE,'policy.py':'''def validate(s,v):
    if s is None or v['receipt'] not in s['registry'] or v['receipt'] in s['spent']+s['pending'] or v['target'] not in ('docker-offline','docker-open','native') or v['outcome'] not in ('ready','failed','unknown'):raise ValueError('invalid authority')
'''})

def main():
    assert len(TASKS)==18
    for level in ('easy','intermediate','difficult'):assert sum(t['difficulty']==level for t in TASKS)==6
    value={'version':1,'origin':'machine_authored_repair_benchmark','human_labels':False,'model_ranking_validated':False,'tasks':TASKS}
    Path(__file__).with_name('corpus.json').write_text(json.dumps(value,indent=2)+'\n')
if __name__=='__main__':main()
