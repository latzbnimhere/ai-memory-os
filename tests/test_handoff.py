"""Isolated bridge safety and lifecycle integration tests; no real Drive writes."""
import contextlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'lib'))
from aimem import core, handoff as h

class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='handoff-test-'))
        self.root = self.tmp/'memory'; self.root.mkdir()
        self.patch = mock.patch.multiple(core,ROOT=self.root,LOCKS=self.root/'.locks'); self.patch.start()
        self.drive = self.tmp/'CloudStorage/GoogleDrive-test/My Drive/AI-Project-Handoffs'
        self.drive.mkdir(parents=True)
        (self.drive/'00-MASTER').mkdir()
        (self.root/'registry').mkdir()
        c = {'version':1,'drive':h.discover(self.tmp/'CloudStorage'),'projects':[]}
        for pid,slug in [('Alpha','alpha'),('Beta','beta')]:
            (self.drive/pid).mkdir()
            c['projects'].append(dict(project_id=pid,display_name=pid,folder=pid,memory_slug=slug,active=True))
            p=self.root/'projects'/slug; (p/'checkpoints/cp1').mkdir(parents=True)
            (p/'CURRENT.md').write_text('# Current\nSafe state\n');(p/'NEXT.md').write_text('# Next\nRead only\n')
            m=dict(id='cp1',label='Accepted phase',result='PASS',time='2026-09-13T20:00:00Z',current_sha256=h.digest((p/'CURRENT.md').read_bytes()),next_sha256=h.digest((p/'NEXT.md').read_bytes()),repo_state={})
            for n in ['CURRENT.md','NEXT.md']: shutil.copy2(p/n,p/'checkpoints/cp1'/n)
            (p/'checkpoints/cp1/meta.json').write_text(h.dumps(m))
            (p/'project.json').write_text(h.dumps(dict(slug=slug,current_checkpoint='cp1',memory_version=1,repo='')))
        h.atomic(h.cfg_path(),h.dumps(c))
    def tearDown(self):
        self.patch.stop(); shutil.rmtree(self.tmp)
    def result(self,pid='Alpha'):return h.read_bundle(self.drive/pid)
    def state(self,pid='Alpha'):
        p=h.resolve(h.config(),pid); _,m,_=h.metadata(p)
        return dict(checkpoint=m['id'],current_sha256=m['current_sha256'],next_sha256=m['next_sha256'],authority=['Test workspace'],service_state='Stopped',executed=['Read-only check'],did_not_execute=['No production mutation'],blockers=[],owner_decision_required='NONE',next_safe_action='Read checkpoint',hard_boundaries=['No deployment'],phase_status='PASS',evidence=dict.fromkeys(['packet_path','seal_sha256','report_path','result_path'],'N/A'))
    def put_state(self,s,pid='Alpha'):
        p=h.state_path(h.resolve(h.config(),pid)); p.parent.mkdir(exist_ok=True);p.write_text(h.dumps(s))
    def test_single(self):
        self.put_state(self.state()); self.assertEqual(h.publish('Alpha')['handoff_sync'],'SYNCED_LOCAL');self.assertEqual(self.result()['status'],'PASS')
    def test_all_and_two_sequential_master(self):
        for pid in ['Alpha','Beta']:self.assertEqual(h.publish(pid)['handoff_sync'],'SYNCED_LOCAL')
        j=json.loads((self.drive/'00-MASTER/PROJECT_INDEX.json').read_text())
        self.assertEqual(len(j['projects']),2)
        for row in j['projects']:self.assertEqual(row['generation'],self.result(row['project_id'])['generation'])
        last=json.loads((self.drive/'00-MASTER/LAST_SYNC.json').read_text())
        for n,d in last['content_sha256'].items():self.assertEqual(h.digest((self.drive/'00-MASTER'/n).read_bytes()),d)
    def test_atomic_file_never_torn_and_retry(self):
        self.assertEqual(h.publish('Alpha')['handoff_sync'],'SYNCED_LOCAL')
        old=(self.drive/'Alpha/CURRENT_RESULT.json').read_bytes(); oldindex=(self.drive/'00-MASTER/PROJECT_INDEX.json').read_bytes()
        original=os.replace
        def fail(a,b):
            if str(b)==str(self.drive/'Alpha/LATEST_PACKET.txt'):raise OSError('simulated disconnection')
            return original(a,b)
        with mock.patch.object(h.os,'replace',side_effect=fail): self.assertEqual(h.publish('Alpha')['handoff_sync'],'FAILED')
        self.assertEqual((self.drive/'Alpha/CURRENT_RESULT.json').read_bytes(),old)
        self.assertEqual((self.drive/'00-MASTER/PROJECT_INDEX.json').read_bytes(),oldindex)
        with self.assertRaises(h.BridgeError):self.result()
        self.assertEqual(h.publish('Alpha')['handoff_sync'],'SYNCED_LOCAL'); self.result()
        self.assertFalse(list((h.local()/'pending').glob('*.json')))
    def test_invalid_mapping_pending(self):
        self.assertEqual(h.publish('Unknown')['handoff_sync'],'PROJECT_MAPPING_REQUIRED')
        self.assertTrue(list((h.local()/'pending').glob('*.json')));self.assertFalse(list((self.drive/'Alpha').iterdir()))
    def test_missing_drive_no_recreation_and_retry(self):
        moved=self.drive.with_name('Disconnected');self.drive.rename(moved)
        self.assertEqual(h.publish('Alpha')['handoff_sync'],'FAILED');self.assertFalse(self.drive.exists())
        moved.rename(self.drive);self.assertEqual(h.publish('Alpha')['handoff_sync'],'SYNCED_LOCAL')
    def test_temporarily_unavailable_master(self):
        self.assertEqual(h.publish('Alpha')['handoff_sync'],'SYNCED_LOCAL')
        with mock.patch.object(h,'master',side_effect=PermissionError()):self.assertEqual(h.publish('Beta')['handoff_sync'],'FAILED')
        self.result('Beta');self.assertTrue(list((h.local()/'pending').glob('*.json')))
        self.assertEqual(h.publish('Beta')['handoff_sync'],'SYNCED_LOCAL')
    def test_secret_rejection_only_remediation(self):
        s=self.state();s['executed']=["api_key="+"sk"+"-"+("a"*30)];self.put_state(s)
        self.assertEqual(h.publish('Alpha')['handoff_sync'],'REFUSED_SECRET')
        self.assertFalse(list((h.local()/'pending').glob('*.json')))
        self.assertFalse(list((self.drive/'Alpha').iterdir()))
        for f in h.local().rglob('*'):
            if f.is_file():self.assertNotIn('sk-'+'a'*30,f.read_text())
    def test_more_secrets(self):
        for secret in ['totp_secret'+'=ABCDEFGHIJKLMNOP','recovery_code'+': 123456789','postgres'+'://a:pass@localhost/db','-----BEGIN '+'OPENSSH PRIVATE KEY-----','ENV_CREDENTIAL'+'=do-not-publish']:
            with self.assertRaises(h.BridgeError):h.scan(secret)
    def test_malformed_result_json(self):
        p=h.state_path(h.resolve(h.config(),'Alpha'));p.parent.mkdir();p.write_text('{broken')
        self.assertEqual(h.publish('Alpha')['reason'],'MALFORMED_RESULT_JSON');self.assertFalse(list((self.drive/'Alpha').iterdir()))
    def test_new_registration(self):
        h.register('Gamma');self.assertEqual(h.publish('Gamma')['handoff_sync'],'SYNCED_LOCAL')
        self.assertEqual(self.result('Gamma')['status'],'NEEDS_RECONCILIATION')
    def test_no_checkpoint_corruption(self):
        files={p:p.read_bytes() for p in (self.root/'projects').rglob('*') if p.is_file()}
        for pid in ['Alpha','Beta']:h.publish(pid)
        self.assertTrue(all(p.read_bytes()==v for p,v in files.items()))
    def test_stale_state_cannot_claim_pass(self):
        self.put_state(self.state());(self.root/'projects/alpha/NEXT.md').write_text('Changed')
        h.publish('Alpha');self.assertEqual(self.result()['status'],'NEEDS_RECONCILIATION')
    def test_root_identity_pin(self):
        old=self.drive.with_name('Old');self.drive.rename(old);self.drive.mkdir()
        self.assertEqual(h.publish('Alpha')['reason'],'DRIVE_IDENTITY_MISMATCH')
    def test_duplicate_discovery(self):
        (self.tmp/'CloudStorage/GoogleDrive-other/My Drive/AI-Project-Handoffs').mkdir(parents=True)
        with self.assertRaises(h.BridgeError):h.discover(self.tmp/'CloudStorage')
    def test_destination_symlink_refused(self):
        (self.drive/'Alpha/CHATGPT_HANDOFF.md').symlink_to(self.tmp/'outside')
        self.assertEqual(h.publish('Alpha')['reason'],'SYMLINK_REFUSED');self.assertFalse((self.tmp/'outside').exists())
    def test_hook_nonfatal_even_local_disk_failure(self):
        with mock.patch.object(h,'publish',side_effect=OSError('disk full')):
            out=io.StringIO()
            with contextlib.redirect_stdout(out):h.after_checkpoint('alpha')
            self.assertIn('HANDOFF_SYNC=FAILED',out.getvalue())
    def test_detection_ambiguity(self):
        path=self.tmp/'repo';path.mkdir()
        (self.root/'registry/projects.json').write_text(h.dumps({'projects':{'alpha':{'repo':str(path)},'beta':{'repo':str(path)}}}))
        self.assertIsNone(h.detect(path))
    def test_next_summary_binds_after_checkpoint(self):
        s=self.state();s['checkpoint']='NEXT_ACCEPTED_CHECKPOINT';self.put_state(s)
        h.after_checkpoint('alpha')
        self.assertEqual(self.result()['status'],'PASS')
        self.assertEqual(h.read_json(h.state_path(h.resolve(h.config(),'Alpha')))['checkpoint'],'cp1')
    def exclude_alpha(self):
        c=h.config()
        c['projects']=[p for p in c['projects'] if p['project_id']!='Alpha']
        c['excluded_projects']=[dict(project_id='Alpha',memory_slug='alpha',folder='Alpha',reason='EXCLUDED_BY_OWNER')]
        h.atomic(h.cfg_path(),h.dumps(c))
    def test_owner_exclusion_manual_hook_and_pending_preserves_content(self):
        h.publish('Alpha');h.queue('alpha');self.exclude_alpha()
        before={p:p.read_bytes() for root in [self.root/'projects/alpha',self.drive/'Alpha'] for p in root.rglob('*') if p.is_file()}
        with mock.patch.object(h,'metadata',side_effect=AssertionError('excluded source read')), mock.patch.object(h,'drive',side_effect=AssertionError('excluded Drive access')):
            self.assertEqual(h.publish('Alpha')['handoff_sync'],'EXCLUDED_BY_OWNER')
            h.after_checkpoint('alpha')
        self.assertFalse(list((h.local()/'pending').glob('*.json')))
        self.assertTrue(all(p.read_bytes()==b for p,b in before.items()))
    def test_owner_exclusion_all_and_master(self):
        h.publish('Alpha');h.publish('Beta');self.exclude_alpha()
        from aimem import cli
        with mock.patch.object(h,'publish',return_value={'handoff_sync':'SYNCED_LOCAL'}) as pub:
            cli.main(['handoff','--all'])
            self.assertEqual([c.args[0] for c in pub.call_args_list],['Beta'])
        h.publish('Beta')
        master=self.drive/'00-MASTER'
        self.assertNotIn('Alpha',(master/'PROJECT_INDEX.json').read_text())
        self.assertNotIn('Alpha',(master/'MASTER_HANDOFF.md').read_text())
        self.assertEqual(h.read_json(master/'LAST_SYNC.json')['project_count'],1)
    def test_owner_exclusion_registration_and_stale_registry(self):
        original=h.config()['projects'][0];self.exclude_alpha()
        for args in [('Alpha',None,None),('Another','alpha',None),('Another',None,'ALPHA')]:
            with self.assertRaises(h.BridgeError) as cm:h.register(*args)
            self.assertEqual(cm.exception.code,'EXCLUDED_BY_OWNER')
        c=h.read_json(h.cfg_path());c['projects'].append(original);h.atomic(h.cfg_path(),h.dumps(c))
        self.assertEqual(len(h.config()['projects']),1)
        self.assertEqual(h.publish('ALPHA')['handoff_sync'],'EXCLUDED_BY_OWNER')
    def test_owner_exclusion_retry_cancels_old_pending(self):
        self.exclude_alpha();h.queue('alpha')
        from aimem import cli
        cli.main(['handoff','--retry-pending'])
        self.assertFalse(list((h.local()/'pending').glob('*.json')))
        self.assertFalse(list((self.drive/'Alpha').iterdir()))
    def test_cli_finish_local_success_with_missing_drive(self):
        env=dict(os.environ,AI_MEMORY_ROOT=str(self.tmp/'lifecycle'))
        exe=Path(__file__).resolve().parents[1]/'bin/aimem'
        def run(*args):
            r=subprocess.run([sys.executable,str(exe),*args],env=env,capture_output=True,text=True)
            self.assertEqual(r.returncode,0,r.stdout+r.stderr);return r.stdout
        run('init',env['AI_MEMORY_ROOT']);run('register','alpha','--name','Alpha')
        lr=Path(env['AI_MEMORY_ROOT']);shutil.copy2(h.cfg_path(),lr/'registry/handoffs.json')
        out=run('begin','alpha','--agent','codex','--task','lifecycle');sid=next(x.split('=',1)[1] for x in out.splitlines() if x.startswith('SESSION_ID='))
        (lr/'projects/alpha/CURRENT.md').write_text('# Current\nAccepted test\n')
        self.drive.rename(self.drive.with_name('Unavailable'))
        out=run('finish','alpha','--session',sid,'--result','PASS','--label','test')
        self.assertIn('FINISH=PASS',out);self.assertIn('HANDOFF_SYNC=FAILED',out)
        session=json.loads((lr/'projects/alpha/sessions'/f'{sid}.json').read_text());self.assertEqual(session['status'],'CLOSED')
        self.assertTrue(list((lr/'.handoff/pending').glob('*.json')))
        run('doctor','--deep','--no-repo')

if __name__=='__main__':unittest.main()
