"""Run on the Pi with its installed imaging code, using an isolated fixture root."""
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from sorter.autoundefined import service as b


class Workflow(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        b.SORTIQ = self.root / 'SortIQ'
        b.STORE = self.root / 'storage'
        self.profile = {'cartridge': '9mm', 'model': 'Fixture'}
        self.directory = b.SORTIQ / 'calibers/9mm/Fixture'
        self.directory.mkdir(parents=True)
        (b.SORTIQ / 'config.json').write_text(json.dumps({'active': self.profile}))
        (self.directory / 'model.json').write_text(json.dumps({'stamp_labels': ['CBC', 'S&B'], 'imaging': {}}))
        self.run = b.SORTIQ / 'runs/run_20261102_010101'
        (self.run / 'rejects').mkdir(parents=True)
        (self.run / 'run.json').write_text(json.dumps({'mode': 'serial', 'capture': False}))
        import cv2
        import numpy as np
        self.source = self.run / 'rejects/0001_A.png'
        image = np.zeros((200,200,3), dtype=np.uint8)
        cv2.circle(image,(100,100),80,(180,180,180),-1)
        cv2.putText(image,'CBC',(55,80),cv2.FONT_HERSHEY_SIMPLEX,.7,(0,0,0),2)
        assert cv2.imwrite(str(self.source),image)
        self.original = self.source.read_bytes()
        self.calls = patch.object(b, 'call', return_value={'running': False})
        self.calls.start()
        b.init()
        b.scan()
        self.ident = b.snapshot()['cases'][0]['id']

    def tearDown(self):
        self.calls.stop()
        self.temp.cleanup()

    def review_client(self):
        from flask import Flask,jsonify
        from sorter.autoundefined.flask_integration import register
        app=Flask(__name__)
        app.add_url_rule('/api/runs/<run_id>/rejects/<int:n>/resolve','api_run_reject_resolve',lambda run_id,n:jsonify(original=True),methods=['POST'])
        app.add_url_rule('/api/tray/resolve','api_tray_resolve',lambda:jsonify(original=True),methods=['POST'])
        register(app,b.SORTIQ,lambda:{'running':False})
        b.scan()
        self.ident=b.snapshot()['cases'][0]['id']
        return app.test_client()

    def test_confirm_files_selected_class_instead_of_api_proposal(self):
        client=self.review_client();self.make_auto_ready()
        result=client.post('/autoundefined/api/file',json={'id':self.ident,'stamp':'S&B','profile':self.profile,'confirmed':True})
        self.assertEqual(result.status_code,200)
        receipt=json.loads(b.getcase(self.ident)['receipt'])
        self.assertEqual(receipt['stamp'],'S&B')
        self.assertTrue(all('/S&B/' in x['path'] for x in receipt['outputs']))
        self.assertFalse((self.directory/'data/stamp/CBC').exists())
        self.assertEqual(b.snapshot()['cases'][0]['filed_stamp'],'S&B')
        b.undo(self.ident);self.assertEqual(self.source.read_bytes(),self.original)

    def test_saved_user_choice_survives_restart_and_bulk_overrides_api(self):
        self.make_auto_ready()
        b.select_class(self.ident,'S&B',self.profile);b.init()
        self.assertEqual(b.getcase(self.ident)['user_stamp'],'S&B')
        self.assertEqual(b.snapshot()['cases'][0]['filing_stamp'],'S&B')
        result=b.file_all(self.profile);self.assertEqual(result['filed'],[self.ident])
        self.assertEqual(json.loads(b.getcase(self.ident)['receipt'])['stamp'],'S&B')
        b.undo(self.ident)
        self.assertEqual(self.source.read_bytes(),self.original)
        self.assertIsNone(b.getcase(self.ident)['user_stamp'])
        self.assertEqual(b.file_all(self.profile)['filed'],[])
        b.init();self.assertEqual(b.file_all(self.profile)['filed'],[])

    def test_bulk_api_and_manual_cases_each_get_receipt_and_exact_undo(self):
        self.make_auto_ready()
        second=self.run/'rejects/0002_A.png';second.write_bytes(self.original);b.scan()
        other=next(r['id'] for r in b.snapshot()['cases'] if r['id']!=self.ident)
        b.select_class(other,'S&B',self.profile)
        result=b.file_all(self.profile)
        self.assertCountEqual(result['filed'],[self.ident,other]);self.assertEqual(result['failed'],[])
        for ident in result['filed']:
            receipt=json.loads(b.getcase(ident)['receipt'])
            self.assertTrue(all(b.digest(b.SORTIQ/x['path'])==x['sha256'] for x in receipt['outputs']))
            b.undo(ident)
        self.assertEqual(self.source.read_bytes(),self.original);self.assertEqual(second.read_bytes(),self.original)

    def test_bulk_skips_unidentified_ambiguous_local_and_other_profiles(self):
        for proposal in [None,{'stamp':'CBC'},dict(self.make_auto_ready(),needs_manual_review=True),
                         dict(self.make_auto_ready(),disagreement=True),dict(self.make_auto_ready(),stamp='UNKNOWN')]:
            with b.db() as c:c.execute("UPDATE cases SET state='review',proposal=? WHERE id=?",(json.dumps(proposal) if proposal else None,self.ident))
            self.assertEqual(b.file_all(self.profile)['filed'],[])
            self.assertEqual(self.source.read_bytes(),self.original)
        self.make_auto_ready()
        with b.db() as c:c.execute('UPDATE cases SET profile=? WHERE id=?',(json.dumps({'cartridge':'other','model':'Fixture'}),self.ident))
        self.assertEqual(b.file_all(self.profile)['filed'],[])

    def test_bulk_failure_reports_case_and_preserves_source_then_retry_is_safe(self):
        self.make_auto_ready();self.source.write_bytes(b'changed')
        result=b.file_all(self.profile)
        self.assertFalse(result['ok']);self.assertEqual(result['failed'][0]['id'],self.ident)
        self.assertEqual(self.source.read_bytes(),b'changed')
        self.source.write_bytes(self.original)
        self.assertEqual(b.file_all(self.profile)['filed'],[self.ident])
        self.assertEqual(b.file_all(self.profile)['filed'],[])

    def test_bulk_running_sorter_and_stale_profile_are_blocked(self):
        self.make_auto_ready()
        with patch.object(b,'call',return_value={'running':True}):
            with self.assertRaises(ValueError):b.file_all(self.profile)
        with self.assertRaises(ValueError):b.file_all({'cartridge':'other','model':'Fixture'})
        self.assertEqual(self.source.read_bytes(),self.original)

    def test_user_choice_blocks_automatic_api_class_filing(self):
        self.make_auto_ready();b.select_class(self.ident,'S&B',self.profile)
        b.configure({'auto_file':True});self.assertFalse(b.auto_file_one())
        b.file_all(self.profile)
        self.assertEqual(json.loads(b.getcase(self.ident)['receipt'])['stamp'],'S&B')

    def test_class_selection_and_bulk_routes_validate_and_persist(self):
        client=self.review_client()
        for stamp in ['../CBC','UNKNOWN','missing',12]:
            response=client.post('/autoundefined/api/select-class',json={'id':self.ident,'stamp':stamp,'profile':self.profile})
            self.assertEqual(response.status_code,400)
        response=client.post('/autoundefined/api/select-class',json={'id':self.ident,'stamp':'S&B','profile':self.profile})
        self.assertEqual(response.status_code,200)
        response=client.post('/autoundefined/api/file-all',json={'profile':self.profile})
        self.assertEqual(response.status_code,200);self.assertEqual(response.json['filed'],[self.ident])
        self.assertEqual(json.loads(b.getcase(self.ident)['receipt'])['stamp'],'S&B')
        b.undo(self.ident);self.assertEqual(self.source.read_bytes(),self.original)

    def test_clearing_user_choice_restores_api_candidate(self):
        self.make_auto_ready();b.select_class(self.ident,'S&B',self.profile)
        b.select_class(self.ident,None,self.profile)
        self.assertEqual(b.snapshot()['cases'][0]['filing_stamp'],'CBC')

    def test_bulk_continues_after_individual_disk_failure(self):
        self.make_auto_ready()
        second=self.run/'rejects/0002_A.png';second.write_bytes(self.original);b.scan()
        other=next(r['id'] for r in b.snapshot()['cases'] if r['id']!=self.ident)
        b.select_class(other,'S&B',self.profile)
        original_file=b.file_case
        def fail_one(ident,*args,**kwargs):
            if ident==self.ident:raise OSError('synthetic full disk')
            return original_file(ident,*args,**kwargs)
        with patch.object(b,'file_case',side_effect=fail_one):result=b.file_all(self.profile)
        self.assertEqual(result['filed'],[other]);self.assertEqual(result['failed'][0]['id'],self.ident)
        self.assertEqual(self.source.read_bytes(),self.original)
        b.undo(other);self.assertEqual(second.read_bytes(),self.original)

    def test_existing_database_migrates_without_losing_case_or_settings(self):
        with b.db() as c:
            c.execute('ALTER TABLE cases DROP COLUMN user_stamp')
            c.execute("UPDATE settings SET value='true' WHERE key='auto_file'")
        b.init();b.init()
        self.assertIsNone(b.getcase(self.ident)['user_stamp'])
        self.assertTrue(b.configuration()['auto_file'])
        self.assertEqual(self.source.read_bytes(),self.original)

    def test_bulk_post_rejects_cross_origin_without_filing(self):
        client=self.review_client();self.make_auto_ready()
        response=client.post('/autoundefined/api/file-all',json={'profile':self.profile},headers={'Origin':'https://evil.example'})
        self.assertEqual(response.status_code,403)
        self.assertEqual(self.source.read_bytes(),self.original)

    def test_explicit_reclassification_after_undo_survives_restart(self):
        self.make_auto_ready();b.file_all(self.profile);b.undo(self.ident)
        b.select_class(self.ident,'S&B',self.profile);b.init()
        self.assertEqual(b.getcase(self.ident)['user_stamp'],'S&B')
        self.assertEqual(b.file_all(self.profile)['filed'],[self.ident])
        self.assertEqual(json.loads(b.getcase(self.ident)['receipt'])['stamp'],'S&B')
        b.undo(self.ident);self.assertEqual(self.source.read_bytes(),self.original)

    def test_duplicate_scan_does_not_duplicate(self):
        self.assertEqual(b.scan(),0)
        self.assertEqual(len(b.snapshot()['cases']),1)

    def test_no_filing_without_confirmation(self):
        with self.assertRaises(ValueError): b.file_case(self.ident,'CBC',self.profile,False)
        self.assertEqual(self.source.read_bytes(),self.original)

    def test_profile_and_class_validation(self):
        for stamp, profile in [('UNKNOWN',self.profile),('CBC',{'cartridge':'9mm','model':'Other'}),('../CBC',self.profile)]:
            with self.assertRaises(ValueError): b.file_case(self.ident,stamp,profile,True)
        self.assertTrue(self.source.exists())

    def test_source_change_is_blocked(self):
        self.source.write_bytes(b'changed')
        with self.assertRaises(ValueError): b.file_case(self.ident,'CBC',self.profile,True)

    def test_running_sorter_blocks_filing(self):
        with patch.object(b,'call',return_value={'running':True}):
            with self.assertRaises(ValueError): b.file_case(self.ident,'CBC',self.profile,True)

    def test_file_and_undo_are_exact(self):
        result=b.file_case(self.ident,'CBC',self.profile,True)
        self.assertTrue(result['ok']); self.assertFalse(self.source.exists())
        row=b.getcase(self.ident); receipt=json.loads(row['receipt'])
        self.assertEqual(row['state'],'filed')
        for output in receipt['outputs']: self.assertEqual(b.digest(b.SORTIQ/output['path']),output['sha256'])
        with self.assertRaises(ValueError): b.file_case(self.ident,'CBC',self.profile,True)
        b.undo(self.ident)
        self.assertEqual(self.source.read_bytes(),self.original)
        for output in receipt['outputs']: self.assertFalse((b.SORTIQ/output['path']).exists())
        self.assertEqual(b.getcase(self.ident)['state'],'review')

    def test_native_confirmation_uses_reversible_transaction(self):
        from flask import Flask,jsonify
        from sorter.autoundefined.flask_integration import register
        app=Flask(__name__)
        app.add_url_rule('/api/runs/<run_id>/rejects/<int:n>/resolve','api_run_reject_resolve',lambda run_id,n:jsonify(original=True),methods=['POST'])
        app.add_url_rule('/api/tray/resolve','api_tray_resolve',lambda:jsonify(original=True),methods=['POST'])
        register(app,b.SORTIQ,lambda:{'running':False})
        b.scan();ident=b.snapshot()['cases'][0]['id']
        with b.db() as c:
            c.execute("UPDATE cases SET state='review',proposal=? WHERE id=?",(json.dumps({'stamp':'CBC'}),ident))
        result=app.test_client().post('/api/runs/run_20261102_010101/rejects/1/resolve',json={'action':'save','stamp':'CBC'})
        self.assertEqual(result.status_code,200);self.assertTrue(result.json['ok'])
        self.assertEqual(b.getcase(ident)['state'],'filed');self.assertFalse(self.source.exists())
        result=app.test_client().post('/api/autoundefined/undo',json={'id':ident,'confirmed':True})
        self.assertEqual(result.status_code,200);self.assertEqual(self.source.read_bytes(),self.original)

    def test_undo_refuses_changed_dataset(self):
        b.file_case(self.ident,'CBC',self.profile,True)
        receipt=json.loads(b.getcase(self.ident)['receipt'])
        target=b.SORTIQ/receipt['outputs'][0]['path']; target.write_bytes(b'manual edit')
        with self.assertRaises(ValueError): b.undo(self.ident)
        self.assertEqual(target.read_bytes(),b'manual edit')

    def test_api_failure_keeps_source_and_pauses(self):
        with patch.object(b,'vision',side_effect=RuntimeError('OpenAI HTTP 401')):
            b.process_one()
        self.assertEqual(b.getcase(self.ident)['state'],'error')
        self.assertFalse(b.snapshot()['enabled'])
        self.assertEqual(self.source.read_bytes(),self.original)

    def test_model_proposal_does_not_file(self):
        with patch.object(b,'vision',return_value={'stamp':'CBC','confidence':.8,'visible_text':'CBC','explanation':'Clear','needs_manual_review':False}):
            b.process_one()
        self.assertEqual(b.getcase(self.ident)['state'],'review')
        self.assertTrue(self.source.exists())
        self.assertFalse((self.directory/'data/raw/CBC').exists())

    def test_write_failure_rolls_back(self):
        real_open=Path.open
        def fail(path,*args,**kwargs):
            if '/data/stamp/' in str(path) and args and args[0]=='xb':
                raise OSError('simulated disk failure')
            return real_open(path,*args,**kwargs)
        with patch.object(Path,'open',fail):
            with self.assertRaises(OSError): b.file_case(self.ident,'CBC',self.profile,True)
        self.assertEqual(self.source.read_bytes(),self.original)
        self.assertEqual(list((self.directory/'data/raw/CBC').glob('*.png')),[])

    def test_interrupted_commit_recovers(self):
        b.file_case(self.ident,'CBC',self.profile,True)
        row=b.getcase(self.ident);receipt=json.loads(row['receipt']);receipt['state']='committing'
        b.write_receipt(row,receipt);b.recover()
        self.assertEqual(b.getcase(self.ident)['state'],'recovery')
        b.recover_case(self.ident)
        self.assertEqual(self.source.read_bytes(),self.original)
        self.assertEqual(b.getcase(self.ident)['state'],'review')

    def test_real_request_shape_and_unknown_class_rejected(self):
        from io import BytesIO
        key=self.root/'test-key';key.write_text('fixture-key-not-real')
        proposal={'stamp':'NOT A CLASS','confidence':.9,'visible_text':'XYZ','explanation':'Unknown mark','needs_manual_review':False}
        result={'status':'completed','id':'fixture-response','output':[{'content':[{'type':'output_text','text':json.dumps(proposal)}]}]}
        captured=[]
        def response(request,timeout):
            captured.append(json.loads(request.data))
            return BytesIO(json.dumps(result).encode())
        with patch.object(b,'KEY',key),patch.object(b.urllib.request,'urlopen',response):
            value=b.vision(self.source,['CBC','S&B'],self.profile)
        self.assertEqual(captured[0]['model'],'gpt-6.1-sol')
        self.assertFalse(captured[0]['store'])
        self.assertTrue(captured[0]['text']['format']['strict'])
        self.assertTrue(captured[0]['input'][0]['content'][1]['image_url'].startswith('data:image/jpeg;base64,'))
        self.assertIsNone(value['stamp']);self.assertTrue(value['needs_manual_review'])

    def test_tray_import_and_undo(self):
        tray=self.directory/'data/unknown';tray.mkdir(parents=True)
        original=tray/'0000_A.png';original.write_bytes(self.original)
        self.assertEqual(b.scan(),1)
        ident=next(c['id'] for c in b.snapshot()['cases'] if c['kind']=='tray')
        b.file_case(ident,'S&B',self.profile,True);self.assertFalse(original.exists())
        b.undo(ident);self.assertEqual(original.read_bytes(),self.original)

    def test_scan_skips_capture_and_recorded_other_profile(self):
        metadata=self.run/'run.json'
        with b.db() as c:c.execute('DELETE FROM cases')
        metadata.write_text(json.dumps({'mode':'sim','capture':True}))
        self.assertEqual(b.scan(),0)
        metadata.write_text(json.dumps({'mode':'serial','profile':{'cartridge':'other','model':'Fixture'}}))
        self.assertEqual(b.scan(),0)


    def test_duplicate_images_share_one_paid_result(self):
        tray=self.directory/'data/unknown';tray.mkdir(parents=True)
        (tray/'0000_A.png').write_bytes(self.original);b.scan()
        with patch.object(b,'vision',return_value={'stamp':'CBC','confidence':.9,'visible_text':'CBC','explanation':'Clear','needs_manual_review':False}) as vision:
            self.assertTrue(b.process_one());self.assertTrue(b.process_one())
            self.assertEqual(vision.call_count,1)
        self.assertTrue(all(c['state']=='review' for c in b.snapshot()['cases']))

    def test_capture_unknowns_included_but_recognized_frames_excluded(self):
        (self.run/'run.json').write_text(json.dumps({'mode':'serial','capture':True}))
        frames=self.run/'frames';frames.mkdir()
        (frames/'0002_A.png').write_bytes(self.original)
        (frames/'0003_A.png').write_bytes(self.original)
        (self.run/'log.jsonl').write_text(json.dumps({'n':2,'category':'unmatched','reason':'blurry'})+'\n'+json.dumps({'n':3,'category':'CBC','reason':'ok'})+'\n')
        self.assertEqual(b.scan(),1)
        self.assertFalse(any(c['n']==3 for c in b.snapshot()['cases']))

    def test_key_rotation_is_write_only_and_keeps_protected_previous_key(self):
        from io import BytesIO
        key=self.root/'protected/openai-key';key.parent.mkdir();key.write_text('old-fixture-value')
        value='sk-fixture-'+('x'*80)
        def response(*args,**kwargs):return BytesIO(json.dumps({'data':[{'id':b.MODEL}]}).encode())
        with patch.object(b,'KEY',key),patch.object(b.key_settings.urllib.request,'urlopen',response):
            result=b.configure({'api_key':value})
        self.assertNotIn(value,json.dumps(result));self.assertTrue(result['key_configured'])
        self.assertEqual(key.read_text(),value)
        self.assertEqual(key.stat().st_mode&0o777,0o600)
        self.assertEqual((key.parent/'openai-key.previous').read_text(),'old-fixture-value')
        self.assertEqual((key.parent/'openai-key.previous').stat().st_mode&0o777,0o600)

    def test_invalid_key_never_replaces_current_key(self):
        key=self.root/'protected-key';key.write_text('preserve-me')
        with patch.object(b,'KEY',key):
            with self.assertRaises(ValueError):b.configure({'api_key':'key_tracking_fixture'})
        self.assertEqual(key.read_text(),'preserve-me')

    def test_automatic_setting_does_not_need_per_case_confirmation(self):
        key=self.root/'key';key.write_text('fixture')
        with patch.object(b,'KEY',key):
            self.assertTrue(b.configure({'enabled':True})['enabled'])
            self.assertFalse(b.configure({'enabled':False})['enabled'])

    def test_local_disagreement_is_visible_and_never_files(self):
        local={'available':True,'stamp':'S&B','similarity':.94,'accepted':True,'known_class':True}
        with patch.object(b.local_recognizer,'predict',return_value=local),patch.object(b,'vision',return_value={'stamp':'CBC','confidence':.9,'visible_text':'CBC','explanation':'Read CBC','needs_manual_review':False}):b.process_one()
        proposal=json.loads(b.getcase(self.ident)['proposal'])
        self.assertTrue(proposal['disagreement']);self.assertTrue(proposal['needs_manual_review'])
        self.assertEqual(proposal['local_model']['stamp'],'S&B');self.assertTrue(self.source.exists())

    def test_transient_failure_retries_with_backoff(self):
        with patch.object(b,'vision',side_effect=RuntimeError('OpenAI HTTP 429')):b.process_one()
        row=b.getcase(self.ident)
        self.assertEqual(row['state'],'queued');self.assertGreater(row['retry_at'],b.time.time())
        self.assertFalse(b.process_one())

    def test_restart_recovers_inflight_with_bounded_backoff(self):
        with b.db() as c:c.execute("UPDATE cases SET state='processing',attempts=1 WHERE id=?",(self.ident,))
        b.init();row=b.getcase(self.ident)
        self.assertEqual(row['state'],'queued');self.assertGreater(row['retry_at'],b.time.time())
        with b.db() as c:c.execute("UPDATE cases SET state='processing',attempts=4 WHERE id=?",(self.ident,))
        b.init();self.assertEqual(b.getcase(self.ident)['state'],'error')

    def test_profile_switch_does_not_reimport_historical_run(self):
        other=b.SORTIQ/'calibers/9mm/Other';other.mkdir(parents=True)
        (other/'model.json').write_text((self.directory/'model.json').read_text())
        (b.SORTIQ/'config.json').write_text(json.dumps({'active':{'cartridge':'9mm','model':'Other'}}))
        self.assertEqual(b.scan(),0);self.assertEqual(len(b.snapshot()['cases']),1)

    def test_queue_keeps_processing_original_profile_after_switch(self):
        other=b.SORTIQ/'calibers/9mm/Other';other.mkdir(parents=True)
        (other/'model.json').write_text(json.dumps({'stamp_labels':['OTHER'],'imaging':{}}))
        (b.SORTIQ/'config.json').write_text(json.dumps({'active':{'cartridge':'9mm','model':'Other'}}))
        with patch.object(b,'vision',return_value={'stamp':'CBC','confidence':.9,'visible_text':'CBC','explanation':'CBC','needs_manual_review':False}) as vision:
            b.process_one()
        self.assertEqual(vision.call_args.args[2],self.profile)
        self.assertEqual(vision.call_args.args[1],['CBC','S&B'])
        self.assertEqual(b.getcase(self.ident)['state'],'review')

    def test_unknown_trays_from_other_profiles_are_discovered(self):
        other=b.SORTIQ/'calibers/45ACP/Other';(other/'data/unknown').mkdir(parents=True)
        (other/'model.json').write_text(json.dumps({'stamp_labels':['CBC'],'imaging':{}}))
        (other/'data/unknown/0000_A.png').write_bytes(self.original)
        self.assertEqual(b.scan(),1)
        item=next(c for c in b.snapshot()['cases'] if c['kind']=='tray')
        self.assertEqual(item['profile'],{'cartridge':'45ACP','model':'Other'})

    def make_auto_ready(self,**changes):
        proposal={'stamp':'CBC','confidence':.98,'visible_text':'CBC','explanation':'Readable CBC',
                  'needs_manual_review':False,'source':b.MODEL,'model':b.MODEL,
                  'response_id':'fixture_response','disagreement':False}
        proposal.update(changes)
        with b.db() as c:
            c.execute("UPDATE cases SET state='review',proposal=?,error=NULL WHERE id=?",(json.dumps(proposal),self.ident))
        return proposal

    def test_auto_filing_defaults_off_and_preserves_review(self):
        self.make_auto_ready()
        self.assertFalse(b.configuration()['auto_file'])
        self.assertFalse(b.auto_file_one());self.assertTrue(self.source.exists())

    def test_auto_filing_on_files_valid_result_with_receipt_and_undo(self):
        self.make_auto_ready();b.configure({'auto_file':True})
        self.assertTrue(b.auto_file_one())
        row=b.getcase(self.ident);self.assertEqual(row['state'],'filed')
        receipt=json.loads(row['receipt']);self.assertEqual(receipt['filed_by'],'automatic')
        self.assertEqual(receipt['proposal']['response_id'],'fixture_response')
        self.assertFalse(self.source.exists())
        b.undo(self.ident);self.assertEqual(self.source.read_bytes(),self.original)
        self.assertTrue(b.getcase(self.ident)['auto_file_blocked'])
        self.assertFalse(b.auto_file_one())

    def test_auto_filing_rejects_ambiguous_failed_and_unknown_results(self):
        b.configure({'auto_file':True})
        for fields in [{'stamp':None},{'stamp':'UNDEFINED'},{'stamp':'NOT A CLASS'},
                       {'needs_manual_review':True},{'disagreement':True},
                       {'response_id':None},{'source':'SortIQ local model'}]:
            with self.subTest(fields=fields):
                self.make_auto_ready(**fields)
                self.assertFalse(b.auto_file_one());self.assertTrue(self.source.exists())

    def test_auto_filing_setting_persists_and_can_be_disabled(self):
        self.make_auto_ready();b.configure({'auto_file':True});b.init()
        self.assertTrue(b.configuration()['auto_file'])
        b.configure({'auto_file':False});self.assertFalse(b.auto_file_one())
        self.assertTrue(self.source.exists())

    def test_auto_filing_flag_validation(self):
        for value in ['true',1,None]:
            with self.assertRaises(ValueError):b.configure({'auto_file':value})

    def test_auto_filing_does_not_write_while_sorting(self):
        self.make_auto_ready();b.configure({'auto_file':True})
        with patch.object(b,'call',return_value={'running':True}):self.assertFalse(b.auto_file_one())
        self.assertTrue(self.source.exists())

    def test_auto_filing_uses_original_profile_after_switch(self):
        self.make_auto_ready();b.configure({'auto_file':True})
        other=b.SORTIQ/'calibers/45ACP/Other';other.mkdir(parents=True)
        (other/'model.json').write_text(json.dumps({'stamp_labels':['OTHER'],'imaging':{}}))
        (b.SORTIQ/'config.json').write_text(json.dumps({'active':{'cartridge':'45ACP','model':'Other'}}))
        self.assertTrue(b.auto_file_one())
        receipt=json.loads(b.getcase(self.ident)['receipt'])
        self.assertEqual(receipt['profile'],self.profile)
        self.assertTrue(all(x['path'].startswith('calibers/9mm/Fixture/') for x in receipt['outputs']))

    def test_auto_filing_failure_retains_source_and_blocks_hot_loop(self):
        self.make_auto_ready();b.configure({'auto_file':True})
        with patch.object(b,'file_case',side_effect=OSError('fixture disk error')):
            self.assertTrue(b.auto_file_one())
        self.assertTrue(self.source.exists());self.assertTrue(b.getcase(self.ident)['auto_file_blocked'])
        self.assertFalse(b.auto_file_one())

    def test_manual_filing_still_requires_confirmation_with_auto_on(self):
        self.make_auto_ready();b.configure({'auto_file':True})
        with self.assertRaises(ValueError):b.file_case(self.ident,'CBC',self.profile,False)
        self.assertTrue(self.source.exists())

    def test_direct_automatic_call_cannot_bypass_off_switch(self):
        self.make_auto_ready()
        with self.assertRaises(ValueError):b.file_case(self.ident,'CBC',self.profile,True,automatic=True)
        self.assertTrue(self.source.exists())

    def test_completed_undo_survives_restart_without_automatic_refiling(self):
        self.make_auto_ready();b.configure({'auto_file':True});b.auto_file_one();b.undo(self.ident)
        # Simulate interruption after the durable undo receipt but before its DB update.
        with b.db() as c:c.execute("UPDATE cases SET state='filed',auto_file_blocked=0 WHERE id=?",(self.ident,))
        b.recover()
        self.assertEqual(b.getcase(self.ident)['state'],'review')
        self.assertTrue(b.getcase(self.ident)['auto_file_blocked']);self.assertFalse(b.auto_file_one())


if __name__=='__main__':
    unittest.main(verbosity=2)
