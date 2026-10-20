#!/usr/bin/env python3
"""Drive the real Start Fleet menu in private GTK/VTE.

Requires an existing isolated runtime-probe slot. Native terminal screenshots
and text are recorded; no owner terminal, display, clipboard or sessions used.
The default stops at review. The caller's explicit live=True mode confirms a
bounded $0.10 provider run, observes a known-usage worker turn and stops it.
"""
import json
import os
from pathlib import Path
import signal
import subprocess
import time


def probe(binary, env, repo, root, state, live=False):
    read_fd,write_fd=os.pipe()
    display=subprocess.Popen(['Xvfb','-displayfd',str(write_fd),'-screen','0','2400x1600x24','-nolisten','tcp'],pass_fds=(write_fd,),stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    os.close(write_fd)
    with os.fdopen(read_fd) as r:display_name=':'+r.readline().strip()
    os.environ['DISPLAY']=display_name
    # Import Gtk only after selecting the private display: its override caches initialization.
    import gi
    gi.require_version('Gtk','3.0');gi.require_version('Vte','2.91')
    from gi.repository import GLib,Gtk,Pango,Vte
    Gtk.init([])
    output=state/'native-menu';output.mkdir(mode=0o700)
    window=Gtk.Window();window.set_decorated(False);window.set_default_size(2400,1600)
    terminal=Vte.Terminal();terminal.set_font(Pango.FontDescription('DejaVu Sans Mono 12'));window.add(terminal);window.show_all()
    child=[];failure=[];start=time.monotonic();steps=[];active={'index':0,'sent_at':start,'review':False,'pages':[],'confirmed':False,'launched_manifest':None}
    local_env=dict(env,DISPLAY=display_name,TERM='xterm-256color',COLORTERM='truecolor',LC_ALL='C.UTF-8',DOXA_NERD_FONT='off',DOXA_KEY_NOTICE='0')
    if live:local_env.update(DOXA_FLEET_REVIEW_INPUT_PRICE='1',DOXA_FLEET_REVIEW_OUTPUT_PRICE='4',DOXA_FLEET_SUPERVISION_MODE='shadow')
    # Existing reviewed JSON is purely configuration, no secrets.
    candidate={'id':'glm-small','provider':'glm','model':'glm-5.3-flash','effort':'low',
        'description':'Explicitly bounded live GLM menu target.' if live else 'Menu-only review fixture; launch is never confirmed.',
        'context_tokens':24576 if live else 32768,'max_output_tokens':4096,'supports_tools':True,
        'input_usd_micros_per_million':150000 if live else 300000,'output_usd_micros_per_million':500000 if live else 1200000}
    value={'version':1,'jev_model':'jev-1.13.0','criteria_version':'native-menu-review',
        'candidates':[candidate,{**candidate,'id':'glm-backup'}],'fallback_id':'glm-small',
        'confidence_threshold':.75,'max_calls':3,'max_spend_usd_micros':3,'max_input_bytes':24576,'deadline_ms':1000}
    config=state/'menu-router.json';config.write_text(json.dumps(value));config.chmod(0o600)
    down=b'\x1b[B';left=b'\x1b[D';enter=b'\r';erase=b'\x7f'
    script=[
        (b'\x1b', 'close-onboarding'),(b'\x10', 'palette'),(b'fleet start', 'palette-query'),(enter, 'open-menu'),
        (enter+(b'Bounded live menu case: reply exactly ready, call no tools, write no files and stop.' if live else b'Disposable menu review only; no model calls')+enter, 'task'),
        (down*2+left, 'worker-router'),(down+enter+b'glm-small'+enter,'worker-pin'),
        (down+enter+erase+b'1'+enter,'worker-count'),(down+left*(1 if live else 2),'coordinator-off' if live else 'coordinator-router'),
        (down+enter+b'glm-small'+enter,'coordinator-pin'),
        (down*3+enter+erase+b'0.10'+enter,'budget'),
    ]
    if live:
        script.extend([(down+enter+erase*3+b'deepseek:deepseek-flash'+enter,'independent-api-reviewer'),
            (down*3+enter+erase*3+b'0.02'+enter,'review-allocation'),
            (down+enter+str(config).encode()+enter,'private-config')])
    else:script.append((down*5+enter+str(config).encode()+enter,'private-config'))
    script.append((b'\x1b[21~','open-review'))
    def spawned(_terminal,pid,error,_data=None):
        if error:failure.append(str(error));Gtk.main_quit()
        else:child.append(pid)
    def tick():
        if time.monotonic()-start>(110 if live else 25):failure.append('Native menu deadline exceeded');Gtk.main_quit();return False
        if not child:return True
        now=time.monotonic()
        if now-start<2 or now-active['sent_at']<.28:return True
        text=terminal.get_text_format(Vte.Format.TEXT)
        if active['index']<len(script):
            data,label=script[active['index']]
            expected='Actions' if label in ('palette-query','open-menu') else ('New fleet' if label not in ('close-onboarding','palette') else None)
            if expected and expected not in text:
                (output/'terminal.txt').write_text(text)
                failure.append('Unexpected UI before '+label+'; no text entered');Gtk.main_quit();return False
            terminal.feed_child(data);steps.append(label);active['index']+=1;active['sent_at']=now;return True
        if not active['review']:
            active['pages'].append(text)
            joined='\n'.join(active['pages'])
            if len(active['pages'])<10:
                terminal.feed_child(b'\x1b[6~');active['sent_at']=now;return True
            (output/'terminal.txt').write_text(joined)
            (output/'terminal-pages.json').write_text(json.dumps(active['pages']))
            required=['Start fleet','router:glm-small','docker-offline','Reviewed Router config SHA256']
            if any(value not in joined for value in required):
                failure.append('Native menu did not reach expected Router launch review');Gtk.main_quit();return False
            subprocess.run(['ffmpeg','-loglevel','error','-y','-f','x11grab','-draw_mouse','0','-video_size','2400x1600','-i',display_name+'.0','-frames:v','1',str(output/'review.png')],check=True)
            active['review']=True
            if live:terminal.feed_child(b'A')
            else:terminal.feed_child(b'\x1b');terminal.feed_child(b'\x11')
            active['sent_at']=now;return True
        if live:
            if not active['confirmed']:
                terminal.feed_child(b'Y');steps.append('explicit-arm-and-launch');active['confirmed']=True;active['sent_at']=now;return True
            fleet_root=Path(env['DOXA_HOME'])/'fleet'
            candidates=list(fleet_root.glob('ui-*/manifest.json'))
            if not candidates:return True
            if len(candidates)!=1:failure.append('Unexpected menu-run count');Gtk.main_quit();return False
            manifest_path=candidates[0];manifest=json.loads(manifest_path.read_text());active['launched_manifest']=manifest_path
            if manifest.get('phase')=='finished':failure.append('Menu run ended before observed success');Gtk.main_quit();return False
            slots=manifest.get('slots',[])
            if len(slots)==1 and slots[0].get('last_turn_kind')=='turn_done':
                turn=slots[0]['last_turn']
                if turn.get('is_error') or turn.get('accounting_unknown') or not turn.get('usage_complete'):
                    failure.append('Live menu turn failed or usage unknown');Gtk.main_quit();return False
                active['live_turn']=turn
                active['live_guard']=json.loads(Path(manifest['supervision']['context']['state_path']).read_text())
                result=subprocess.run([str(binary),'fleet','stop',manifest['run_id'],'--root',str(fleet_root)],env=env,cwd=repo,capture_output=True,text=True,timeout=75)
                final=json.loads(manifest_path.read_text());active['live_final']=final
                if result.returncode or final.get('live') is not False:failure.append('Menu fleet cleanup incomplete')
                terminal.feed_child(b'\x1b');terminal.feed_child(b'\x11');Gtk.main_quit();return False
            return True
        Gtk.main_quit();return False
    def exited(_terminal,status):
        child.clear()
        if not active['review']:failure.append('TUI exited before review')
        Gtk.main_quit()
    terminal.connect('child-exited',exited)
    terminal.spawn_async(Vte.PtyFlags.DEFAULT,str(repo),[str(binary),'fleet','attach','probe','0','--root',str(root)],
        [f'{k}={v}' for k,v in local_env.items()],GLib.SpawnFlags.DEFAULT,None,None,-1,None,spawned,None)
    GLib.timeout_add(100,tick)
    try:
        Gtk.main()
        record={'native_gtk_vte':True,'menu_navigation':steps,'review_reached':active['review'],'launched':bool(active.get('launched_manifest')),'model_calls':None if live else 0,'live_turn':active.get('live_turn'),'live_guard':active.get('live_guard'),'live_final':active.get('live_final'),'failures':failure}
        (output/'proof.json').write_text(json.dumps(record,indent=2)+'\n')
        if failure:raise RuntimeError('; '.join(failure))
        return record
    finally:
        manifest_path=active.get('launched_manifest')
        if manifest_path and manifest_path.exists():
            manifest=json.loads(manifest_path.read_text())
            if manifest.get('live'):
                subprocess.run([str(binary),'fleet','stop',manifest['run_id'],'--root',str(manifest_path.parent.parent)],env=env,cwd=repo,capture_output=True,timeout=75)
        for pid in child:
            try:os.kill(pid,signal.SIGTERM)
            except ProcessLookupError:pass
        window.destroy();display.terminate();display.wait(timeout=10)
