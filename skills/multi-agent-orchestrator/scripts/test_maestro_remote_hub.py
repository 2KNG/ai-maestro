import base64
import contextlib
import hashlib
import io
import itertools
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import types
import unittest
from unittest.mock import patch
import maestro_remote_hub as hub


class HubTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root/'fleet.py').write_text('# deployed fixture')
        self.db = self.root/'fleet.sqlite3'
        db = sqlite3.connect(self.db)
        db.execute('CREATE TABLE nodes(id TEXT PRIMARY KEY,token TEXT,slot INTEGER UNIQUE)')
        db.commit(); db.close()
        self.config = dict(node_id='office',node_token='a'*48,hub='ubuntu@hub.example.com',
            hub_project=str(self.root),hub_db=str(self.db),registry_url='https://hub.example.com',
            port=16701,widget_port=18701)

    def remote(self, action, served=None, max_slots=None):
        if os.name == 'nt':
            self.skipTest('This code runs on the Linux SSH hub, using flock')
        c = hub.settings(self.config)
        c.update(action=action, orca_port_base=hub.ORCA_PORT_BASE, widget_port_base=hub.WIDGET_PORT_BASE,
                 max_slots=max_slots or hub.MAX_SLOTS)
        if served is None:
            db = sqlite3.connect(self.db)
            served = [dict(node_id=r[0],slot=r[1]) for r in db.execute('SELECT id,slot FROM nodes')]
            db.close()
        response = io.BytesIO(json.dumps({'nodes':served}).encode())
        output = io.StringIO()
        class Registry:
            def __init__(self,path,origin): self.db=sqlite3.connect(path)
            def provision(self,node,token):
                used={r[0] for r in self.db.execute('SELECT slot FROM nodes')}
                slot=next(i for i in itertools.count() if i not in used)
                self.db.execute('INSERT INTO nodes VALUES(?,?,?)',(node,hashlib.sha256(token.encode()).hexdigest(),slot));self.db.commit()
            def get(self,node): return {'slot':self.db.execute('SELECT slot FROM nodes WHERE id=?',(node,)).fetchone()[0]}
        with patch('sys.stdin',io.StringIO(json.dumps(c))), contextlib.redirect_stdout(output), patch('urllib.request.urlopen',return_value=response), patch.dict('sys.modules',fleet=types.SimpleNamespace(Registry=Registry)), patch('os.chdir'):
            exec(hub.REMOTE_CODE,{})
        return json.loads(output.getvalue())

    def test_preflight_does_not_write_database_or_lock(self):
        before=self.db.read_bytes()
        result=self.remote('preflight')
        self.assertEqual(result['port'],16701)
        self.assertEqual(self.db.read_bytes(),before)
        self.assertFalse(Path(str(self.db)+'.maestro-enroll.lock').exists())

    def test_enrollment_idempotent_and_credential_bound(self):
        base=self.config['node_id']
        self.assertEqual(self.remote('register')['slot'],0)
        self.assertEqual(self.remote('register')['slot'],0)
        # Same name from another machine (credential): hostname_2, hostname_3, ...
        self.config['node_token']='b'*48
        self.assertEqual(self.remote('preflight')['node_id'],base+'_2')
        self.assertEqual(self.remote('register'),dict(node_id=base+'_2',slot=1,port=16702,widget_port=18702))
        self.config['node_token']='c'*48
        self.assertEqual(self.remote('register')['node_id'],base+'_3')
        # A rerun with the original credential keeps its ID even when asking for another name.
        self.config.update(node_token='b'*48,node_id=base)
        self.assertEqual(self.remote('register')['node_id'],base+'_2')
        db=sqlite3.connect(self.db)
        self.assertEqual(db.execute('SELECT count(*) FROM nodes').fetchone()[0],3);db.close()

    def test_fleet_scales_past_ten_nodes_without_placeholder_slots(self):
        db=sqlite3.connect(self.db)
        for i in range(12): db.execute('INSERT INTO nodes VALUES(?,?,?)',(f'node-{i}','hash',i))
        db.commit();db.close()
        result=self.remote('register')
        self.assertEqual((result['slot'],result['port'],result['widget_port']),(12,16713,18713))
        db=sqlite3.connect(self.db)
        self.assertEqual(db.execute('SELECT count(*) FROM nodes').fetchone()[0],13);db.close()

    def test_lowest_free_slot_is_reused(self):
        db=sqlite3.connect(self.db)
        for i in (0,1,3): db.execute('INSERT INTO nodes VALUES(?,?,?)',(f'node-{i}','hash',i))
        db.commit();db.close()
        self.assertEqual(self.remote('preflight')['slot'],2)

    def test_port_range_exhaustion_refused(self):
        db=sqlite3.connect(self.db)
        for i in range(3): db.execute('INSERT INTO nodes VALUES(?,?,?)',(f'node-{i}','hash',i))
        db.commit();db.close()
        with self.assertRaisesRegex(RuntimeError,'no available'): self.remote('preflight',max_slots=3)

    def test_ssh_accepts_high_slots(self):
        result=types.SimpleNamespace(returncode=0,stdout=json.dumps(dict(slot=40,port=16741,widget_port=18741)))
        with patch('subprocess.run',return_value=result) as run:
            route=hub.preflight(self.config)
        self.assertEqual(route['port'],16741)
        self.assertIn('"max_slots"',run.call_args.kwargs['input'])

    def test_registry_origin_required(self):
        config={**self.config}; config.pop('registry_url')
        with self.assertRaisesRegex(ValueError,'registry_url is required'): hub.settings(config)

    def test_occupied_unregistered_tunnel_port_refused(self):
        original=Path.read_text
        def read(path,*args,**kwargs):
            if str(path) in ('/proc/net/tcp','/proc/net/tcp6'):
                return 'header\n0: 0100007F:413D 00000000:0000 0A\n'
            return original(path,*args,**kwargs)
        with patch.object(Path,'read_text',read):
            with self.assertRaisesRegex(RuntimeError,'occupied'): self.remote('preflight')

    def test_wrong_database_refused(self):
        with self.assertRaisesRegex(RuntimeError,'differs'): self.remote('register',[dict(node_id='other',slot=0)])
        db=sqlite3.connect(self.db)
        self.assertEqual(db.execute('SELECT count(*) FROM nodes').fetchone()[0],0);db.close()

    def test_undeployed_refused(self):
        (self.root/'fleet.py').unlink()
        with self.assertRaisesRegex(RuntimeError,'not deployed'): self.remote('preflight')

    def test_ssh_keeps_secret_off_command_and_checks_allocation(self):
        result=types.SimpleNamespace(returncode=0,stdout=json.dumps(dict(slot=2,port=16703,widget_port=18703)))
        with patch('subprocess.run',return_value=result) as run:
            route=hub.preflight(self.config)
        self.assertEqual(route['orca_url'],'wss://hub.example.com/orca/office/')
        self.assertNotIn(self.config['node_token'],' '.join(run.call_args.args[0]))
        self.assertIn(self.config['node_token'],run.call_args.kwargs['input'])
        self.assertEqual(route['widget_url'],'https://hub.example.com/orca/office-maestro/')

    def test_failure_does_not_leak_remote_output(self):
        with patch('subprocess.run',return_value=types.SimpleNamespace(returncode=1,stderr=self.config['node_token'])):
            with self.assertRaises(RuntimeError) as error: hub.register(self.config)
        self.assertNotIn(self.config['node_token'],str(error.exception))

    def test_heartbeat_no_pairing_upload_and_no_redirect(self):
        class Reply(io.BytesIO): pass
        reply=Reply(json.dumps(dict(orca_port=16701,maestro_port=18701)).encode())
        opener=types.SimpleNamespace(open=lambda request,timeout: reply)
        with patch('urllib.request.build_opener',return_value=opener), patch('urllib.request.Request',wraps=hub.urllib.request.Request) as request:
            hub.heartbeat(self.config,{'cpu_percent':2})
        body=json.loads(request.call_args.kwargs['data'])
        self.assertNotIn('pairing',body)
        self.assertIsNone(hub.NoRedirect().redirect_request(None,None,None,None,None,None))

    def test_pairing_private_read_and_authenticated_upload(self):
        payload = dict(v=2,scope='runtime',deviceToken='fixture-secret',publicKeyB64='fixture-key',pairedDeviceId='fixture-device')
        code = 'orca://pair?code='+base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip('=')
        path=self.root/'pairing.json';path.write_text(json.dumps({'pairing_code':code}));path.chmod(0o600)
        config={**self.config,'state_dir':str(self.root)}
        self.assertEqual(hub.private_pairing(config),code)
        response=io.BytesIO(json.dumps(dict(orca_port=16701,maestro_port=18701)).encode())
        opener=types.SimpleNamespace(open=lambda request,timeout: response)
        with patch('urllib.request.build_opener',return_value=opener), patch('urllib.request.Request',wraps=hub.urllib.request.Request) as request:
            hub.heartbeat(config,{'hostname':'fixture'},code)
        self.assertEqual(json.loads(request.call_args.kwargs['data'])['pairing'],code)
        path.chmod(0o644)
        if os.name != 'nt':
            with self.assertRaises(ValueError):hub.private_pairing(config)
        with self.assertRaises(ValueError):hub.validate_pairing('orca://pair?code=invalid')

    def test_invalid_origin_and_id(self):
        for key,value in [('node_id','../bad'),('registry_url','http://hub.example.com'),('registry_url','https://user:pass@hub.example.com')]:
            with self.subTest(key=key,value=value), self.assertRaises(ValueError): hub.settings({**self.config,key:value})

if __name__=='__main__': unittest.main()
