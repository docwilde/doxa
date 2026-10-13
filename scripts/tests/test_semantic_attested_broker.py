"""Trust-negative protocol tests; these fixtures never authorize a binding."""
import importlib.util
import os
import pathlib
import signal
import socket
import struct
import subprocess
import sys
import threading
import tempfile
import time
import unittest
from unittest.mock import patch

spec=importlib.util.spec_from_file_location('attested_broker',pathlib.Path(__file__).parents[1]/'semantic-broker-proof/attested_broker.py')
broker=importlib.util.module_from_spec(spec);spec.loader.exec_module(broker)


class BrokerBounds(unittest.TestCase):
    def setUp(self):
        self.previous=signal.signal(signal.SIGALRM,lambda *_: (_ for _ in ()).throw(RuntimeError('deadline')))
    def tearDown(self):
        signal.setitimer(signal.ITIMER_REAL,0);signal.signal(signal.SIGALRM,self.previous)
    def test_slow_challenge_is_bounded_by_absolute_deadline(self):
        client,server=socket.socketpair()
        def trickle():
            try:
                for _ in range(12): server.send(b'x');time.sleep(.025)
            except OSError: pass
            finally: server.close()
        thread=threading.Thread(target=trickle);thread.start();started=time.monotonic()
        try:
            with broker.deadline(.08):
                with self.assertRaisesRegex(RuntimeError,'deadline'): broker.read_exact(client,12)
            self.assertLess(time.monotonic()-started,.2)
        finally: client.close();thread.join()
    def test_nested_engine_deadline_does_not_reset_transaction_budget(self):
        started=time.monotonic()
        with self.assertRaisesRegex(RuntimeError,'deadline'):
            with broker.deadline(.08):
                time.sleep(.03)
                with broker.deadline(2): time.sleep(.08)
        self.assertLess(time.monotonic()-started,.2)
    def test_fabricated_stdout_is_not_allowed_to_choose_cid(self):
        # A wrong pre-inspect CID fails before any upgrade/LSP I/O.
        client,server=socket.socketpair()
        def daemon():
            try:
                server.recv(4096)
                body=b'{"Id":"different"}'
                server.sendall(b'HTTP/1.1 200 OK\r\nContent-Length: '+str(len(body)).encode()+b'\r\n\r\n'+body)
            finally: server.close()
        thread=threading.Thread(target=daemon);thread.start()
        engine=broker.Engine({})
        try:
            with patch.object(engine,'connect',return_value=client):
                with self.assertRaisesRegex(RuntimeError,'ID drift'): engine.attach('a'*64)
        finally: client.close();thread.join()
    def test_same_uid_or_root_engine_configuration_is_refused(self):
        for client,engine in [(1000,1000),(1000,0),(0,1001),('1000',1001)]:
            with self.assertRaisesRegex(RuntimeError,'separation'):
                broker.validate_config({'client_uid':client,'engine_uid':engine})
        broker.validate_config({'client_uid':1000,'engine_uid':1001})
    def test_failed_source_open_removes_reserved_snapshot_directory(self):
        with tempfile.TemporaryDirectory() as temporary:
            parent=pathlib.Path(temporary);source=parent/'regular-file';source.write_text('file')
            destination=parent/'snapshots';destination.mkdir()
            with self.assertRaises(NotADirectoryError):
                broker.snapshot(str(source.resolve()),destination,'nonce','unreached')
            self.assertEqual(list(destination.iterdir()),[])
    def test_project_executable_overrides_are_refused_before_launch(self):
        for name in (".cargo/config", ".cargo/config.toml", "rust-toolchain.toml", "rust-analyzer.toml"):
            with tempfile.TemporaryDirectory() as temporary:
                parent=pathlib.Path(temporary);source=parent/'source';source.mkdir();destination=parent/'snapshots';destination.mkdir()
                path=source/name;path.parent.mkdir(exist_ok=True);path.write_text('untrusted override')
                with self.assertRaisesRegex(RuntimeError,'override refused'):
                    broker.snapshot(str(source.resolve()),destination,'nonce','unreached')
                self.assertEqual(list(destination.iterdir()),[])

    def test_busy_or_unhealthy_status_during_definition_is_refused(self):
        for status in [{'quiescent':False,'health':'ok'},{'quiescent':True,'health':'error'}]:
            client,server=socket.socketpair()
            messages=[{'jsonrpc':'2.0','method':'experimental/serverStatus','params':status},
                      {'jsonrpc':'2.0','id':3,'result':None}]
            for message in messages:
                body=broker.compact(message);lsp=b'Content-Length: '+str(len(body)).encode()+b'\r\n\r\n'+body
                server.sendall(bytes([1,0,0,0])+len(lsp).to_bytes(4,'big')+lsp)
            server.close();session=broker.Lsp(client);session.guard_quiescence=True
            try:
                with self.assertRaisesRegex(RuntimeError,'quiescence/health drift'):
                    session.until(lambda message:message.get('id')==3)
            finally:client.close()
    def test_disconnect_cancels_silent_analyzer_promptly(self):
        attached,analyzer=socket.socketpair();client,caller=socket.socketpair()
        caller.close();session=broker.Lsp(attached,client);started=time.monotonic()
        try:
            with self.assertRaisesRegex(RuntimeError,'cancelled'):
                session.receive()
            self.assertLess(time.monotonic()-started,.2)
        finally:attached.close();analyzer.close();client.close()
    def test_lost_create_reply_recovers_name_and_removes_exact_id(self):
        self.recover_create_case("lost",False)

    def test_malformed_create_ids_still_recover_nonce_name(self):
        for value in ("",None,[],17,"g"*64,"../unsafe"):
            self.recover_create_case(value,False)

    def test_failed_recovered_deletion_never_claims_cleanup(self):
        self.recover_create_case("lost",True)

    def recover_create_case(self,created_id,delete_fails):
        requests=[];cid='a'*64;nonce='b'*64
        class Recorder:
            def __init__(self,config):pass
            def close(self):pass
            def live(self):pass
            def request(self,method,path,body=None,acceptable=None):
                requests.append((method,path))
                if path=='/info':return 200,{'SecurityOptions':['name=rootless'],'CgroupVersion':'2'}
                if path.startswith('/images/'):return 200,{'Id':'sha256:'+'f'*64}
                if path.startswith('/containers/create?'):
                    if created_id=='lost':raise TimeoutError('lost create response')
                    return 201,{'Id':created_id}
                if path.endswith('/json'):
                    if not delete_fails and any(method=='DELETE' for method,_ in requests):return 404,None
                    return 200,{'Id':cid,'Name':'/doxa-semantic-'+nonce,'Image':'sha256:'+'f'*64,
                        'Config':{'Labels':{'io.doxa.semantic.nonce':nonce}}}
                if method=='DELETE':
                    if delete_fails:raise RuntimeError('deletion failed')
                    return 204,None
                raise AssertionError((method,path))
        config={'allowed_source_parent':'/sources','snapshot_parent':'unused','config_sha256':'c'*64,
            'engine_sha256':'e'*64,'analyzer_sha256':'a'*64,'image_id':'sha256:'+'f'*64}
        challenge={'protocol':broker.PROTOCOL,'nonce':nonce,'root':'/sources/project',
            'source_input_sha256':'s'*64,'request':{'method':'textDocument/definition','id':3,
            'params':{'textDocument':{'uri':'file:///sources/project/src/lib.rs'}}}}
        with patch.object(broker,'Engine',Recorder),patch.object(broker,'snapshot',return_value=(None,'m'*64)):
            receipt=broker.transaction(config,challenge,b'challenge')
        self.assertEqual(receipt['status'],'unknown');self.assertEqual(receipt['cleanup_removed'],not delete_fails)
        self.assertTrue(receipt['create_response_recovered'])
        self.assertIn(('DELETE',f'/containers/{cid}?force=1&v=1'),requests)

    def test_held_executable_reader_rejects_current_mapping_drift(self):
        reader_spec=importlib.util.spec_from_file_location('reader',pathlib.Path(__file__).parents[1]/'semantic-broker-proof/read_analyzer.py')
        reader=importlib.util.module_from_spec(reader_spec);reader_spec.loader.exec_module(reader)
        from types import SimpleNamespace
        stable=SimpleNamespace(st_dev=1,st_ino=2,st_size=0,st_mtime_ns=3,st_ctime_ns=4,st_mode=0o100555)
        changed=SimpleNamespace(st_dev=1,st_ino=99,st_size=0,st_mtime_ns=3,st_ctime_ns=4,st_mode=0o100555)
        with patch.object(reader.sys,'argv',['reader','123','5','5','5']), \
             patch.object(reader.os,'stat',return_value=SimpleNamespace(st_ino=5)), \
             patch.object(reader.os,'open',side_effect=[7,8]), \
             patch.object(reader.os,'fstat',side_effect=[stable,stable,changed]), \
             patch.object(reader.os,'read',return_value=b''), patch.object(reader.os,'close'):
            with self.assertRaisesRegex(RuntimeError,'mapping changed'):reader.main()

    def test_reader_rejects_wrong_namespace_before_executable_open(self):
        reader_spec=importlib.util.spec_from_file_location('reader',pathlib.Path(__file__).parents[1]/'semantic-broker-proof/read_analyzer.py')
        reader=importlib.util.module_from_spec(reader_spec);reader_spec.loader.exec_module(reader)
        from types import SimpleNamespace
        with patch.object(reader.sys,'argv',['reader','123','5','5','5']), \
             patch.object(reader.os,'stat',return_value=SimpleNamespace(st_ino=6)), \
             patch.object(reader.os,'open') as opened:
            with self.assertRaisesRegex(RuntimeError,'namespace identity differs'):reader.main()
            opened.assert_not_called()

    def helper_case(self,program,expected,cancel=False):
        # Real disposable subprocesses exercise pipe limits, cancellation,
        # deadline, kill and reap. Their outputs never authorize a binding.
        pid=os.getpid();pidfd=os.pidfd_open(pid);children=[];original=subprocess.Popen
        config={'nsenter_path':sys.executable,'python_path':sys.executable,'reader_path':sys.executable,
            'nsenter_sha256':'pin','python_sha256':'pin','reader_sha256':'pin',
            'analyzer_sha256':'analyzer','_cancel':lambda:cancel}
        def spawn(argv,**options):
            self.assertEqual(options['env'],{'LANG':'C','PYTHONDONTWRITEBYTECODE':'1'})
            self.assertTrue(options['close_fds']);self.assertEqual(len(options['pass_fds']),1)
            self.assertEqual(argv[2:4],['--',sys.executable]);self.assertIn('-I',argv)
            child=original([sys.executable,'-I','-c',program],**options);children.append(child);return child
        started=time.monotonic()
        try:
            with patch.object(broker,'controlled'),patch.object(broker,'sha_file',return_value='pin'), \
                 patch.object(broker.subprocess,'Popen',side_effect=spawn):
                with self.assertRaisesRegex(RuntimeError,expected):broker.analyzer_executable(config,pid,pidfd)
            self.assertLess(time.monotonic()-started,3.2)
            self.assertEqual(len(children),1);self.assertIsNotNone(children[0].poll())
        finally:
            os.close(pidfd)
            for child in children:
                if child.poll() is None:child.kill();child.wait()

    @unittest.skipUnless(hasattr(os,'pidfd_open'),'Linux pidfd/user-namespace helper')
    def test_reader_helper_timeout_kills_and_reaps(self):
        self.helper_case('import time;time.sleep(30)','helper deadline')

    @unittest.skipUnless(hasattr(os,'pidfd_open'),'Linux pidfd/user-namespace helper')
    def test_reader_helper_output_is_bounded(self):
        self.helper_case('import sys;sys.stdout.write("x"*4097);sys.stdout.flush()','helper output bound')

    @unittest.skipUnless(hasattr(os,'pidfd_open'),'Linux pidfd/user-namespace helper')
    def test_reader_helper_cancellation_kills_and_reaps(self):
        self.helper_case('import time;time.sleep(30)','client cancelled',cancel=True)

    @unittest.skipUnless(hasattr(os,'pidfd_open'),'Linux pidfd/user-namespace helper')
    def test_reader_helper_namespace_output_cannot_override_pin(self):
        self.helper_case('print(\'{"sha256":"analyzer","namespaces":{"user":0}}\')','bytes/namespace differ')

    @unittest.skipUnless(hasattr(os,'pidfd_open'),'Linux pidfd/user-namespace helper')
    def test_reader_helper_changed_pin_is_refused_before_exec(self):
        pidfd=os.pidfd_open(os.getpid())
        try:
            with patch.object(broker,'controlled'),patch.object(broker,'sha_file',return_value='changed'), \
                 patch.object(broker.subprocess,'Popen') as spawned:
                with self.assertRaisesRegex(RuntimeError,'helper changed'):
                    broker.analyzer_executable({'nsenter_path':sys.executable,'nsenter_sha256':'pin'},os.getpid(),pidfd)
                spawned.assert_not_called()
        finally:os.close(pidfd)

    def test_lsp_frame_stderr_is_never_a_definition(self):
        client,server=socket.socketpair()
        body=b'Content-Length: 27\r\n\r\n{"jsonrpc":"2.0","id":3}'
        server.sendall(bytes([2,0,0,0])+len(body).to_bytes(4,'big')+body);server.close()
        try:
            with self.assertRaisesRegex(RuntimeError,'ended early'): broker.Lsp(client).receive()
        finally: client.close()


if __name__=='__main__': unittest.main()
