#!/usr/bin/env python3
"""Bounded real-provider pilot using installed DOXA and disposable Docker slots.

This pilot pins GLM API targets; it never invokes an unknown-billing CLI or
missing decision service. Worker and reviewer allocations are disjoint and
retained on uncertain outcomes. Output is private; labels are not human labels.
"""
import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import signal
import socket
import stat
import subprocess
import time
import threading
from runtime_probe import IMAGE, ENGINE, private_write

class Client:
    def __init__(self, path):
        self.sock=socket.socket(socket.AF_UNIX);self.sock.settimeout(12);self.sock.connect(path)
        self.stream=self.sock.makefile('rwb',buffering=0);self.hello=self.read()
        self.send({'type':'attach','cursor':self.hello['next_seq']});self.request_id=0
    def read(self):
        line=self.stream.readline(65537)
        if not line or len(line)>65536: raise RuntimeError('Invalid daemon frame')
        return json.loads(line)
    def send(self,v): self.stream.write((json.dumps(v,separators=(',',':'))+'\n').encode())
    def request(self,kind,**kw):
        self.request_id+=1;self.send({'type':kind,'id':self.request_id,**kw})
        for _ in range(2048):
            v=self.read()
            if v.get('id')==self.request_id and v.get('type')=='reply': return v
        raise RuntimeError('Daemon reply queue exceeds bound')
    def call(self,method,params=None):return self.request('call',method=method,params=params or {})
    def close(self):self.stream.close();self.sock.close()


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--live',action='store_true',help='Explicitly opt this bounded new case into paid provider calls');p.add_argument('--state',type=Path,required=True)
    p.add_argument('--handoff-only',action='store_true',help='Focused handoff/ACK/confirmation/release case; no research or migration');p.add_argument('--max-new-budget',type=float,default=.90);p.add_argument('--image',default=IMAGE);p.add_argument('--daemon',type=Path);p.add_argument('--binary',type=Path,default=Path.home()/'.local/bin/doxa');p.add_argument('--prior-held-usd','--prior-reserved-usd',dest='prior_reserved_usd',type=float,default=0.0);p.add_argument('--case-id',default='fleet-pilot-01');p.add_argument('--review-budget',type=float,default=None);p.add_argument('--context-tokens',type=int,default=32768);p.add_argument('--output-tokens',type=int,default=4096);p.add_argument('--input-rate',type=int,default=300000);p.add_argument('--output-rate',type=int,default=1200000);args=p.parse_args()
    if not args.live:p.error('Provider pilot requires explicit --live; no state or calls were created')
    if not 0<=args.prior_reserved_usd<.90:raise RuntimeError('Prior holds must leave a positive allocation')

    if not math.isfinite(args.max_new_budget) or not 0<args.max_new_budget<=.90:raise RuntimeError('New allocation must be finite and <=0.90')
    run_budget=min(.90-args.prior_reserved_usd,args.max_new_budget);review_budget=args.review_budget if args.review_budget is not None else .20-args.prior_reserved_usd
    if not 0<review_budget<run_budget:raise RuntimeError('Review allocation must leave a positive worker allocation')
    if args.input_rate<150000 or args.output_rate<500000:raise RuntimeError('Rates cannot be below the verified exact-model registry')
    request_bytes=args.context_tokens-args.output_tokens
    worker_hold_micros=(request_bytes*25*args.input_rate+args.output_tokens*args.output_rate+999999)//1000000
    slot_micros=int(round((run_budget-review_budget)*1000000))//3
    if worker_hold_micros>slot_micros:raise RuntimeError('Worst-case worker request hold exceeds its disjoint slot allocation')
    state=args.state.resolve();state.mkdir(mode=0o700,parents=True,exist_ok=False)
    if len(str(state))>42:raise RuntimeError('Use a short real-disk state path')
    home,root,repo=(state/n for n in ('h','f','repo'))
    for path in (home,root,repo):path.mkdir(mode=0o700)
    private_write(repo/'README.md','# Disposable live fleet pilot\nCase '+args.case_id+'. The fixed public evidence URL is https://example.com/.\nNo implementation changes are requested.\n')
    subprocess.run(['git','init','-q',str(repo)],check=True)
    subprocess.run(['git','-C',str(repo),'add','README.md'],check=True)
    subprocess.run(['git','-C',str(repo),'-c','user.name=DOXA acceptance','-c','user.email=acceptance@localhost','commit','-q','-m','test: seed disposable live fleet pilot'],check=True)
    keyfile=Path.home()/'.doxa/credentials.json';meta=keyfile.lstat()
    if not stat.S_ISREG(meta.st_mode) or stat.S_IMODE(meta.st_mode)!=0o600 or meta.st_uid!=os.getuid():raise RuntimeError('Unsafe private credential store')
    keys=json.loads(keyfile.read_text());env=dict(os.environ)
    for name in ('OPENAI_API_KEY','ANTHROPIC_API_KEY','TYPESAFE_API_KEY','SUBQ_API_KEY','DOXA_SESSION_BUDGET_USD','DOXA_MODEL','DOXA_ROUTER_MODEL','DOXA_MCP_ENABLED','DOXA_MCP_CONFIG'):env.pop(name,None)
    env.update(DEEPSEEK_API_KEY=keys['deepseek'],ZAI_API_KEY=keys['glm'],DOXA_HOME=str(home),DOXA_RUNTIME_DIR=str(state/'rt'),
        DOXA_DOCKER_HOST=ENGINE,DOCKER_HOST=ENGINE,DOXA_DOCKER_IMAGE=args.image,DOXA_LORE='0',DOXA_WORKTREE='1',
        DOXA_VENDOR_TOOLS='workspace-read',TMPDIR='/home/docwilde/t',LORE_ROOT=str(state/'lore'),LORE_PROJECTS_DIR=str(state/'projects'),LORE_CODEX_SESSIONS_DIR=str(state/'codex-sessions'),LORE_DISABLE_SYNC='1',LORE_DISABLE_REVIEW='1')
    if args.daemon:env['DOXA_DAEMON_BIN']=str(args.daemon.resolve(strict=True))
    candidate={'id':'glm-small','provider':'glm','model':'glm-5.3-flash','effort':'low','description':'Owner pinned low-cost GLM pilot target; no automatic routing.',
        'context_tokens':args.context_tokens,'max_output_tokens':args.output_tokens,'supports_tools':True,'input_usd_micros_per_million':args.input_rate,'output_usd_micros_per_million':args.output_rate}
    config={'version':1,'jev_model':'jev-1.13.0','criteria_version':'fleet-pilot-20261020','candidates':[candidate,{**candidate,'id':'glm-backup'}],
        'fallback_id':'glm-small','confidence_threshold':.75,'max_calls':3,'max_spend_usd_micros':3,'max_input_bytes':24576,'deadline_ms':1000}
    conf=state/'router.json';private_write(conf,json.dumps(config,indent=2)+'\n')
    task=('Bounded acceptance case '+args.case_id+'. Two workers and one acting coordinator verify a read-only dependency and research handoff. '
        'Do not write files, spawn sessions, run shell commands, use third-party services, reveal credentials or expand scope. '
        'Use only DOXA peer_list/peer_send/peer_history and workspace_read. Maximum four sent peer messages per slot. '
        'Worker 1 initially reads README.md and sends exactly one status to the coordinator, then ends its turn. '
        'Worker 2 remains dormant until human dependency release; after release send one status to the coordinator and stop. '
        'Coordinator initially says ready and sends no messages. For ordinary status messages, acknowledge in plain text only, without a peer response. '
        'When the owner supplies a checkpoint, worker 1 sends one handoff citing that exact artifact. Coordinator ACKs a handoff with exact artifact_refs, '
        'in_reply_to and readback={next_action:"Review README evidence",assumptions:[],open_questions:[]}. '
        'Worker 1 polls peer_history at most twice for the ACK and confirms it using exact artifact_refs, in_reply_to and handoff_response={agrees:true,correction:null}; omit unused optional fields. '
        'No worker has internet access. Only after explicit owner follow-up and supervisor-only network migration, worker 1 may send one question asking '
        'the coordinator to verify the page title at https://example.com/; the coordinator returns evidence only if it actually has an approved fetch tool. '
        'If no tool exists, report unavailable honestly. Never fabricate a fetched source or claim completion from peer prose.')
    if args.handoff_only:
        task=('Bounded focused handoff acceptance '+args.case_id+'. Two workers and one acting coordinator. '
            'No writes, shell commands, spawning, external MCP, web access, secrets or scope changes. '
            'Use only workspace_read initially and canonical DOXA peer_list/peer_send/peer_history. '
            'Worker 1 initially reads README.md and finishes in plain text with no peer message; wait for owner checkpoint. '
            'Worker 2 stays dormant until explicit human dependency release, then reads README.md and finishes in plain text without messaging. '
            'Coordinator initially says ready without tools. On a genuine typed handoff, use mcp__doxa__peer_send with target equal to the exact full sender session ID, '
            'text="ACK README evidence", fleet_kind="ack", in_reply_to equal to the actual handoff message_id, exact artifact_refs, '
            'and readback={"next_action":"Review README evidence","assumptions":[],"open_questions":[]}. '
            'The worker sends the handoff only after owner checkpoint instructions, then ends that turn immediately after the successful send result. '
            'When the ACK starts a new peer turn, worker 1 uses mcp__doxa__peer_send with exact coordinator target, text="Confirmed README evidence", '
            'fleet_kind="confirm", exact artifact_refs, in_reply_to equal to the actual ACK message_id, '
            'handoff_response={"agrees":true,"correction":null}; omit unused fields. '
            'Only peer_list/history and the one required send are allowed in each handoff/confirmation turn; no workspace tools in those turns. '
            'Coordinator ends confirmation receipt in plain text with no further message. Never claim a send without a successful real tool result containing its message_id. '
            'If a tool fails, report the actual failure and end; do not retry. Maximum one sent handoff and one sent confirmation by worker 1, one ACK by coordinator.')
    opts=['start','--pool','router:glm-small','--supervisor','router:glm-small','--router-config',str(conf),'-n','2','--prompt',task,
        '--worker-task','1:'+('Read README.md, finish in plain text without messaging, then wait for owner checkpoint/handoff instructions.' if args.handoff_only else 'Read README.md and send one status, then wait for owner checkpoint/handoff instructions.'),
        '--worker-task','2:'+('After explicit human dependency release, read README.md and finish in plain text without messaging.' if args.handoff_only else 'After human dependency release, send one status confirming the read-only handoff and stop.'),
        '--worker-after','2:1','--cwd',str(repo),'--root',str(root),'--run-id','pilot','--run-budget',format(run_budget,'.6f'),'--review-budget',format(review_budget,'.6f'),
        '--alignment-supervisor','deepseek:deepseek-flash','--supervision-mode','shadow','--message-review','shadow','--message-judge','llm:deepseek:deepseek-flash',
        '--review-max-calls',('6' if args.handoff_only else '12'),'--review-interval','3600','--review-input-price','1','--review-output-price','4','--quiescence-timeout',('480' if args.handoff_only else '600'),'--quiet-dwell','120',
        '--approve','peer','--approval-grace','0']
    binary=args.binary.resolve(strict=True);base=[str(binary),'fleet'];manifest_path=root/'pilot/manifest.json';report={'case_id':args.case_id,'group_id':'live-pilot-20261020',
        'human_labels':False,'started_at':time.time(),'binary_sha256':hashlib.sha256(binary.read_bytes()).hexdigest(),'image':args.image,'engine':ENGINE,'focused_handoff':args.handoff_only,
        'limits':{'total_usd':run_budget,'review_usd':review_budget,'prior_retained_usd':args.prior_reserved_usd,'combined_usd':.90,'router_slots':3,'slot_usd_floor':slot_micros/1000000,'request_bytes':request_bytes,'output_tokens_per_turn':args.output_tokens,
            'max_tool_requests_per_turn':25,'worker_hold_usd':worker_hold_micros/1000000,'input_usd_mtok':args.input_rate/1000000,'output_usd_mtok':args.output_rate/1000000,'review_max_calls':(6 if args.handoff_only else 12),'review_output_tokens':512,'review_input_usd_mtok':1,'review_output_usd_mtok':4},'steps':[]}
    def run(extra,timeout=30):
        r=subprocess.run(base+extra,env=env,cwd=repo,text=True,capture_output=True,timeout=timeout)
        return {'returncode':r.returncode,'stdout':r.stdout,'stderr':r.stderr}
    def save():
        q=state/'progress.json';q.write_text(json.dumps(report,indent=2)+'\n');q.chmod(0o600)
    def snapshot():return json.loads(manifest_path.read_text())
    def guard():
        m=snapshot();return json.loads(Path(m['supervision']['context']['state_path']).read_text())
    def require_known():
        g=guard()
        if g.get('accounting_unknown'):raise RuntimeError('Review usage unknown; reservations retained, no retry')
        for path in (home/'router').glob('*.router.json'):
            row=json.loads(path.read_text())
            if row['worker']['accounting_unknown'] or row['router']['accounting_unknown']:
                raise RuntimeError('Worker usage unknown; reservations retained, no retry')
    def wait_idle(label,timeout=90):
        deadline=time.monotonic()+timeout;stable=0
        while time.monotonic()<deadline:
            if child.poll() is not None:raise RuntimeError('Controller exited during '+label)
            require_known();m=snapshot();idle=True
            for row in m['slots']:
                c=Client(row['socket_path'])
                try:r=c.call('get_state');idle &= not r.get('running',True) and r.get('queued',1)==0
                finally:c.close()
            stable=stable+1 if idle else 0
            if stable>=5:
                report['steps'].append({'stage':label,'at':time.time(),'manifest':snapshot(),'guard':guard()});save();return
            time.sleep(.4)
        raise RuntimeError('Idle deadline reached at '+label)
    def prompt(slot,text):
        require_known();row=snapshot()['slots'][slot];c=Client(row['socket_path'])
        try:r=c.request('prompt',text=text)
        finally:c.close()
        if r.get('ok') is not True:raise RuntimeError('Owner follow-up refused')
    report['version']=subprocess.check_output([str(binary),'--version'],text=True)
    report['dry_run']=run(opts+['--dry-run']);save()
    if report['dry_run']['returncode']!=0:raise RuntimeError('Pilot dry run refused')
    collectors=[];collectors_stop=threading.Event()
    def collect_events(row):
        path=state/('events-'+row['session_id']+'.jsonl')
        sock=socket.socket(socket.AF_UNIX);sock.settimeout(.3)
        try:
            sock.connect(row['socket_path']);pending=b'';attached=False;written=0
            with open(path,'xb') as out:
                os.chmod(path,0o600)
                while not collectors_stop.is_set():
                    try:chunk=sock.recv(65536)
                    except socket.timeout:continue
                    if not chunk:break
                    pending+=chunk
                    while b'\n' in pending:
                        line,pending=pending.split(b'\n',1)
                        if len(line)>65536:raise RuntimeError('Event exceeds protocol bound')
                        frame=json.loads(line)
                        if not attached:
                            if frame.get('type')!='hello':raise RuntimeError('Missing event collector hello')
                            sock.sendall(b'{"type":"attach","cursor":0}\n');attached=True
                        written+=len(line)+1
                        if written>8*1024*1024:raise RuntimeError('Event capture exceeds private bound')
                        out.write(line+b'\n');out.flush()
        except (OSError,ValueError,RuntimeError):pass
        finally:sock.close()
    log=open(state/'controller.log','w');child=subprocess.Popen(base+opts,env=env,cwd=repo,stdin=subprocess.DEVNULL,stdout=log,stderr=log)
    report['controller_pid']=child.pid;save()
    try:
        deadline=time.monotonic()+90
        while time.monotonic()<deadline:
            if child.poll() is not None:raise RuntimeError('Controller ended before monitoring')
            if manifest_path.exists() and snapshot().get('phase')=='monitoring':break
            time.sleep(.2)
        else:raise RuntimeError('Monitoring deadline exceeded')
        for row in snapshot()['slots']:
            thread=threading.Thread(target=collect_events,args=(row,),daemon=True);thread.start();collectors.append(thread)
        wait_idle('initial_live_turns')
        m=snapshot();assert m['slots'][2]['phase']=='dependency_waiting'
        report['alignment']=run(['alignment','pilot','--root',str(root)]);save()
        if not args.handoff_only:
            report['migration_no_optin']=run(['isolation','pilot','0','docker-open','--root',str(root)])
            assert report['migration_no_optin']['returncode']!=0
            report['migration']=run(['isolation','pilot','0','docker-open','--allow-internet','--root',str(root)]);save()
            deadline=time.monotonic()+45
            while time.monotonic()<deadline:
                if snapshot()['slots'][0]['isolation']=='docker-open':break
                time.sleep(.3)
            else:raise RuntimeError('Supervisor migration did not finish')
            m=snapshot();assert all(x['isolation']=='docker-offline' for x in m['slots'][1:]);assert m['supervision']['paused'] is True
            report['steps'].append({'stage':'supervisor_only_network_optin','manifest':m,'at':time.time()});save()
            report['continue']=run(['continue','pilot',m['supervision']['context']['charter_sha256'],'--root',str(root)]);save()
            require_known()
            if report['continue']['returncode']!=0 or guard().get('paused'):
                raise RuntimeError('Owner continue did not clear the reviewed isolation hold')
            prompt(1,'Owner follow-up within frozen task: send one peer question to the coordinator asking for the actual title at https://example.com/ and explaining that your own internet is disabled. Do not fetch it yourself. Coordinator should answer once with real evidence or explicitly report that no approved fetch tool is configured.')
            wait_idle('offline_worker_research_request',timeout=90)
            report['research_messages']=guard().get('recent_messages',[])
            if not any(row.get('kind')=='question' for row in report['research_messages']):
                raise RuntimeError('No real worker research question was delivered')
            if not any(row.get('kind')=='evidence' for row in report['research_messages']):
                raise RuntimeError('No real coordinator evidence/unavailable reply was delivered')
            save()
            # Only owned controller is killed. No prompts are replayed by resume.
            m=snapshot();before=[row['session_id'] for row in m['slots']];serials=[row.get('turn_serial') for row in m['slots']];child.kill();child.wait(timeout=10)
            child=subprocess.Popen(base+['resume','pilot','--root',str(root)],env=env,cwd=repo,stdin=subprocess.DEVNULL,stdout=log,stderr=log)
            time.sleep(2)
            if child.poll() is not None:raise RuntimeError('Live controller resume refused')
            assert before==[row['session_id'] for row in snapshot()['slots']]
            assert serials==[row.get('turn_serial') for row in snapshot()['slots']], 'Resume recounted old completed turns'
            report['steps'].append({'stage':'controller_crash_recovered_exact_ids','at':time.time()});save()
        evidence=run(['dependency-evidence','pilot','1','--root',str(root)]);report['dependency_evidence']=evidence;save()
        g=guard();assignment=snapshot()['supervision']['context']['assignments'][1]['id']
        rows=[(key,value) for key,value in g['artifacts'].items() if value.get('assignment_id')==assignment and value.get('last_turn_kind')=='turn_done']
        if args.handoff_only:rows=[pair for pair in rows if pair[1].get('snapshot_sha256')]
        if not rows:raise RuntimeError('No completed predecessor host checkpoint with required proof')
        checkpoint=max(rows,key=lambda pair:pair[1].get('turn_serial',0))[0]
        if args.handoff_only:
            coordinator=snapshot()['slots'][0]['session_id']
            call={'target':coordinator,'text':'README evidence ready for explicit owner dependency review','fleet_kind':'handoff','artifact_refs':[checkpoint]}
            prompt(1,'Owner follow-up within frozen assignment. First refresh mcp__doxa__peer_list and verify exact coordinator ID '+coordinator+'. Then actually call mcp__doxa__peer_send with this exact argument object: '+json.dumps(call,separators=(',',':'))+'. No optional fields, no history polls or other work. End this turn immediately after the actual successful tool result. The real ACK will start a separate peer turn; confirm it exactly as the frozen task defines. Never claim a send without its actual successful tool result/message_id.')
        else:
            prompt(1,'Owner follow-up within frozen assignment: send one typed handoff to the coordinator now with artifact_refs:[\"'+checkpoint+'\"] and in_reply_to:null. Omit unused optional fields. Then check peer_history at most twice for its ACK and confirm with the defined response if available. Do not send any other messages or perform any new work.')
        wait_idle('handoff_ack_confirm',timeout=120)
        report['dependency_review']=run(['dependency-review','pilot','1','--root',str(root)]);save()
        # Keep human release separate. A real accepted handoff is necessary.
        if report['dependency_review']['returncode']!=0:raise RuntimeError('Dependency review not releasable; retained evidence')
        body,separator,token=report['dependency_review']['stdout'].rpartition('\nReview token: ')
        if not separator:raise RuntimeError('Dependency review token unavailable; inspect private receipt')
        review=json.loads(body);token=token.strip()
        if not review.get('handoff_resolved') or len(token)!=64:
            raise RuntimeError('Dependency handoff unresolved; human release withheld')
        report['dependency_release']=run(['dependency-release','pilot','1',token,'--root',str(root)]);save()
        if report['dependency_release']['returncode']!=0:raise RuntimeError('Explicit owner dependency release refused')
        wait_idle('released_dependent_turn')
        if args.handoff_only:
            dependent=snapshot()['slots'][2]
            if dependent['phase']!='dispatched' or dependent.get('last_turn_kind')!='turn_done' or dependent.get('last_turn',{}).get('is_error') is not False:
                raise RuntimeError('Dependent dispatch/successful completed turn was not observed')
        report['alignment_final']=run(['alignment','pilot','--root',str(root)]);report['completed']=True
    except Exception as error:
        report['failure']=str(error)
    finally:
        if manifest_path.exists():report['stop']=run(['stop','pilot','--root',str(root)],timeout=75)
        if child.poll() is None:
            child.send_signal(signal.SIGINT)
            try:child.wait(timeout=15)
            except subprocess.TimeoutExpired:child.kill();child.wait()
        collectors_stop.set()
        for thread in collectors:thread.join(timeout=1)
        log.close()
        if manifest_path.exists():
            final=snapshot();report['final_manifest']=final;report['final_guard']=guard()
            report['cleanup_verified']=final.get('phase')=='finished' and final.get('live') is False and all(not Path(row['socket_path']).exists() for row in final.get('slots',[]))
        report['router_journals']={f.name:json.loads(f.read_text()) for f in (home/'router').glob('*.router.json')}
        report['finished_at']=time.time();save();private_write(state/'report.json',json.dumps(report,indent=2)+'\n')
        print(json.dumps({'report':str(state/'report.json'),'failure':report.get('failure'),'completed':report.get('completed',False),'cleanup_verified':report.get('cleanup_verified')}))

if __name__=='__main__':main()
