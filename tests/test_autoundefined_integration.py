"""Native integration and fresh-install privacy; no network or hardware required."""
import ast
from html.parser import HTMLParser
import io
import json
import os
from pathlib import Path
import shutil
import tempfile
import time
import unittest
from unittest.mock import Mock,patch
import zipfile

from flask import Flask,jsonify,request,send_file
from sorter.autoundefined import service as s
from sorter.autoundefined.flask_integration import register

ROOT=Path(__file__).resolve().parents[1]


class Integration(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)/'install'
        self.private=Path(self.temp.name)/'private'
        self.environment=patch.dict(os.environ,{'XDG_CONFIG_HOME':str(self.private)})
        self.environment.start()
        self.root.mkdir();self.profile={'cartridge':'9mm','model':'Fixture'}
        (self.root/'config.json').write_text(json.dumps({'active':self.profile}))
        model=self.root/'calibers/9mm/Fixture';model.mkdir(parents=True)
        (model/'model.json').write_text(json.dumps({'stamp_labels':['CBC'],'imaging':{}}))
        assets=self.root/'webui/static/autoundefined';assets.parent.mkdir(parents=True)
        shutil.copytree(ROOT/'webui/static/autoundefined',assets)
        self.dirty=Mock()
        self.app=self.make_app();self.client=self.app.test_client()

    def tearDown(self):self.environment.stop();self.temp.cleanup()

    def make_app(self):
        app=Flask(__name__)
        app.add_url_rule('/api/runs/<run_id>/rejects/<int:n>/resolve','api_run_reject_resolve',lambda run_id,n:jsonify(original=True),methods=['POST'])
        app.add_url_rule('/api/tray/resolve','api_tray_resolve',lambda:jsonify(original=True),methods=['POST'])
        app.add_url_rule('/api/runs/<run_id>/rejects','rejects',lambda run_id:jsonify(rejects=[{'n':1}]))
        register(app,self.root,lambda:{'running':False},self.dirty)
        return app

    def fixture_key(self):return 'sk-fixture-'+('x'*80)

    def save_key(self):
        def result(*args,**kwargs):return io.BytesIO(json.dumps({'data':[{'id':s.MODEL}]}).encode())
        with patch.object(s.key_settings.urllib.request,'urlopen',result):
            response=self.client.post('/api/autoundefined/config',json={'api_key':self.fixture_key()})
        self.assertEqual(response.status_code,200)
        return response

    def test_fresh_install_has_no_key_and_no_automatic_work(self):
        info=self.client.get('/api/autoundefined/config')
        self.assertEqual(info.status_code,200)
        self.assertFalse(info.json['key_configured']);self.assertFalse(info.json['enabled']);self.assertFalse(info.json['auto_file'])
        self.assertFalse(s.KEY.exists());self.assertFalse(s.KEY.is_relative_to(self.root))
        self.assertEqual(self.client.post('/api/autoundefined/config',json={'enabled':True}).status_code,400)

    def test_fresh_install_at_reused_path_does_not_inherit_previous_key(self):
        self.save_key();old_key=s.KEY;self.assertTrue(old_key.exists())
        shutil.rmtree(self.root/'var')  # Only this test's disposable installation state.
        app=self.make_app();info=app.test_client().get('/api/autoundefined/config').json
        self.assertNotEqual(old_key,s.KEY);self.assertTrue(old_key.exists())
        self.assertFalse(s.KEY.exists());self.assertFalse(info['key_configured'])
        self.assertFalse(info['enabled']);self.assertFalse(info['auto_file'])

    def test_existing_install_retains_own_key_across_restart(self):
        self.save_key();previous=s.KEY;self.make_app()
        self.assertEqual(s.KEY,previous);self.assertTrue(s.configuration()['key_configured'])

    def test_key_is_write_only_and_not_in_project(self):
        response=self.save_key();key=self.fixture_key()
        self.assertNotIn(key,response.text)
        self.assertNotIn(key,self.client.get('/api/autoundefined/state').text)
        self.assertEqual(s.KEY.stat().st_mode&0o777,0o600)
        self.assertFalse(s.KEY.resolve().is_relative_to(self.root.resolve()))
        self.assertNotIn(key,(s.STORE/'queue.sqlite3').read_bytes().decode('latin1'))

    def test_native_api_key_input_never_has_a_prefilled_value(self):
        self.save_key()
        class Inputs(HTMLParser):
            found=[]
            def handle_starttag(self,tag,attrs):
                attrs=dict(attrs)
                if tag=='input' and attrs.get('id')=='beastApiKey':self.found.append(attrs)
        parser=Inputs();parser.feed((ROOT/'webui/static/index.html').read_text())
        self.assertEqual(len(parser.found),1)
        self.assertEqual(parser.found[0]['type'],'password')
        self.assertNotIn('value',parser.found[0])

    def test_csrf_and_public_host_are_rejected(self):
        with patch.object(s.key_settings,'save') as save:
            response=self.client.post('/api/autoundefined/config',json={'api_key':'fixture'},headers={'Origin':'https://evil.example'})
        self.assertEqual(response.status_code,403);save.assert_not_called()
        self.assertEqual(self.client.get('/api/autoundefined/config',base_url='http://evil.example').status_code,403)
        self.assertEqual(self.client.get('/api/autoundefined/config',base_url='http://[::1]:5000').status_code,200)

    def test_content_type_and_response_cache_headers(self):
        response=self.client.post('/api/autoundefined/config',data='api_key=fixture')
        self.assertEqual(response.status_code,400)
        response=self.client.get('/api/autoundefined/config')
        self.assertEqual(response.headers['Cache-Control'],'no-store')

    def test_native_mutation_rejects_json_array(self):
        response=self.client.post('/api/tray/resolve',json=[1])
        self.assertEqual(response.status_code,400)

    def test_feature_off_preserves_native_manual_filing(self):
        result=self.client.post('/api/runs/run_20261102_010101/rejects/1/resolve',json={'action':'save','stamp':'CBC'})
        self.assertTrue(result.json['original'])

    def test_existing_review_routes_and_script_work(self):
        for path in ('/autoundefined/','/autoundefined/review.js','/autoundefined/api/state'):
            response=self.client.get(path);self.assertEqual(response.status_code,200);response.close()

    def test_off_worker_does_not_scan_or_generate(self):
        with patch.object(s,'scan') as scan,patch.object(s,'vision') as vision,patch.object(s.time,'sleep',side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):s.worker()
        scan.assert_not_called();vision.assert_not_called()

    def test_deployable_code_manifest_excludes_state_and_secrets(self):
        from sorter import codesync
        self.save_key()
        paths={item['path'] for item in codesync.manifest(ROOT)['files']}
        self.assertIn('sorter/autoundefined/service.py',paths)
        self.assertIn('webui/static/autoundefined/settings.js',paths)
        self.assertFalse(any(p.startswith('var/') or 'openai-key' in p for p in paths))
        self.assertIn("--exclude 'var/'",(ROOT/'tools/pi_deploy.sh').read_text())

    def test_original_export_code_does_not_include_key_or_runtime_state(self):
        self.save_key()
        source=(ROOT/'webui/server.py').read_text();tree=ast.parse(source)
        node=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='api_export')
        node.decorator_list=[]
        namespace={'request':request,'send_file':send_file,'time':time,'CONFIG_PATH':self.root/'config.json',
                   'MODELS_DIR':self.root/'calibers/9mm/Fixture/models','DATA_DIR':self.root/'calibers/9mm/Fixture/data'}
        exec(compile(ast.Module(body=[node],type_ignores=[]),'export_test','exec'),namespace)
        exporter=Flask('export_fixture')
        exporter.add_url_rule('/api/export','export',namespace['api_export'])
        response=exporter.test_client().get('/api/export?what=all')
        with zipfile.ZipFile(io.BytesIO(response.data)) as archive:
            self.assertEqual(archive.namelist(),['config.json'])
            self.assertNotIn(self.fixture_key().encode(),archive.read('config.json'))
        response.close()


if __name__=='__main__':unittest.main(verbosity=2)
