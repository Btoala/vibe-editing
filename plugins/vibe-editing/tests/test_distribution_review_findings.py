"""Independent desired-behavior checks against unchanged PR source.

All fixture data is synthetic. The OS denies network sockets, and any release
gate replacement is confined to this process with fake provider methods.
Failures are deliberately retained as evidence of review findings.
"""
import contextlib, copy, importlib.util, io, json, sys, tempfile, types, unittest
from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT),str(ROOT/'skills/highlight/buffer')]
import distribution_safety as safety
import distribution_runtime as runtime
import distribution_youtube as youtube
import buffer_api, storage

def module(name,path):
    spec=importlib.util.spec_from_file_location(name,path)
    value=importlib.util.module_from_spec(spec);spec.loader.exec_module(value);return value
post=module('review_post',ROOT/'skills/highlight/scripts/highlight_post.py')
bridge=module('review_bridge',ROOT/'skills/highlight/scripts/highlight_publish_bridge.py')

class IndependentReview(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.now=datetime.now(timezone.utc)
        self.due=(self.now+timedelta(days=1)).isoformat()
        self.d=safety.Destination('buffer-a','Synthetic A','UC'+'A'*22,'buffer',buffer_id='buffer-A',organization_id='org-A',storage_base_url='https://synthetic.invalid',storage_bucket='videos')
        self.y=replace(self.d,alias='youtube-a',route='youtube')
        self.other=replace(self.y,alias='youtube-b',youtube_id='UC'+'B'*22,name='Synthetic B')
        self.registry=self.root/'registry.json';self.registry.write_text(json.dumps({'schema':1,'destinations':[asdict(d) for d in (self.d,self.y,self.other)]}))
        self.state=self.root/'state/history.sqlite3';self.ledger=safety.Ledger(self.state);self.ledger.initialize()
    def plan(self,name='one',destination=None,**kw):
        d=destination or self.d;p=self.root/(name+'.mp4');p.write_bytes(('synthetic '+name).encode())
        manifest={'schema':1,'destination':d.key,'sha256':safety.full_hash(p)[0],'asset_id':name,'title':'Title','description':'Description','youtube_video_id':'video-A'}
        Path(str(p)+'.publish.json').write_text(json.dumps(manifest))
        media={'duration':10.,'width':90,'height':160,'audio':True} if d.route=='buffer' else {'duration':10.,'width':160,'height':90,'audio':True}
        defaults={'operation':'buffer_draft' if d.route=='buffer' else 'upload_private','media_probe':lambda _:media,'now':self.now}
        defaults.update(kw)
        return safety.prepare(p,d,manifest,'Title','Description',**defaults)
    def observation(self,d):
        return {'checked_at':datetime.now(timezone.utc).isoformat(),'name':d.name,'service':'youtube','youtube_id':d.youtube_id,'buffer_id':d.buffer_id,'organization_id':d.organization_id,'can_write':True,'disconnected':False,'locked':False,'paused':False,'mine_channel_ids':[d.youtube_id]}
    def receipt(self,p):
        return {'id':p['remote_id'] or 'remote-'+p['asset_id'],'destination':p['destination_key'],'sha256':p['sha256'],'plan_id':p['plan_id'],'due_at':p['due_at'],'visibility':p['visibility']}
    def youtube_snapshot(self):
        return {'id':'video-A','snippet':{'channelId':self.y.youtube_id,'title':'Title','description':'Description','categoryId':'22'},'status':{'privacyStatus':'private'}}
    def buffer_snapshot(self,p,source):
        return {'id':p['remote_id'] or 'post-A','channelId':self.d.buffer_id,'status':'scheduled' if p['due_at'] else 'draft','dueAt':p['due_at'],'text':p['description'],'metadata':{'title':p['title'],'privacy':p['visibility'],'madeForKids':False,'notifySubscribers':False,'category':{'categoryId':'22'}},'assets':[{'id':'asset-remote','mimeType':'video/mp4','source':source}]}
    def execution(self,plans):
        review=runtime.packet(plans);saved=self.root/'review.json';runtime.save_packet(saved,review)
        return types.SimpleNamespace(registry=self.registry,state=self.state,dry_run=False,verify=False,save_plan=None,execute_plan=saved,approve_plan_id=review['batch_id'])
    def test_dry_run_verify_rejects_before_any_account_access(self):
        p=self.plan(destination=self.y);selection=self.root/'selection.json'
        selection.write_text(json.dumps([{'video_path':p['file'],'video_id':'video-A','publish_at':self.due}]))
        argv=['post','--registry',str(self.registry),'--destination',self.y.alias,'--state',str(self.state),'--selection',str(selection),'--dry-run','--verify']
        with patch.object(sys,'argv',argv),patch.object(post,'get_service') as service,patch.object(post,'list_uploads',return_value=[self.youtube_snapshot()]) as inventory,patch.object(safety,'probe',return_value=p['media']),patch.object(post.safety,'prepare',return_value=p),contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(safety.Blocked):post.main()
        service.assert_not_called();inventory.assert_not_called()
    def test_buffer_readback_rejects_wrong_media(self):
        p=self.plan();self.ledger.reserve(p,self.now)
        provider=buffer_api.BufferProvider(self.d)
        remote=self.buffer_snapshot(p,'https://synthetic.invalid/WRONG-VIDEO.mp4')
        with patch.object(safety,'require_live_publishing'),patch.object(provider,'observation',return_value=self.observation(self.d)),patch.object(storage,'upload_video',return_value=safety.expected_media_url(p)),patch.object(buffer_api,'_mutate',return_value={'id':'post-A'}),patch.object(buffer_api,'get_post',return_value=remote):
            with self.assertRaises(safety.Blocked):provider.submit(p,Path(p['file']),self.ledger)
    def test_promotion_rejects_media_changed_since_confirmed_draft(self):
        prior=self.plan();self.ledger.reserve(prior,self.now);self.ledger.finish(prior['plan_id'],'confirmed','post-A')
        current=self.buffer_snapshot(prior,'https://synthetic.invalid/WRONG-VIDEO.mp4')
        try:
            p=self.plan(operation='buffer_promote',visibility='public',due_at=self.due,remote_id='post-A',remote_snapshot=current)
        except safety.Blocked:
            return  # Rejecting the mismatched media during planning is correct.
        self.ledger.reserve(p,self.now);provider=buffer_api.BufferProvider(self.d)
        actual=self.buffer_snapshot(p,'https://synthetic.invalid/WRONG-VIDEO.mp4')
        with patch.object(safety,'require_live_publishing'),patch.object(provider,'observation',return_value=self.observation(self.d)),patch.object(buffer_api,'_mutate',return_value={'id':'post-A'}) as mutate,patch.object(buffer_api,'get_post',side_effect=[current,actual]):
            try:provider.submit(p,Path(p['file']),self.ledger)
            except safety.Blocked:pass
        mutate.assert_not_called()
    def test_duplicate_legacy_records_can_link_one_existing_mapping(self):
        p=self.plan(destination=self.y);legacy=self.root/'posted_log.json';legacy.write_text(json.dumps({'old-name':['2026-11-10'],'renamed-copy':['2026-11-10']}))
        ledger=safety.Ledger(self.root/'migration/history.sqlite3');ledger.initialize([legacy]);remote=self.youtube_snapshot()
        ledger.adopt(self.y,p['sha256'],p['asset_id'],remote,'Synthetic reviewed mapping',legacy,'old-name')
        ledger.adopt(self.y,p['sha256'],p['asset_id'],remote,'Synthetic same reviewed mapping',legacy,'renamed-copy')
        self.assertEqual(ledger.summary(self.y.key)['unresolved_legacy_records'],0)
    def test_batch_known_duplicate_blocks_before_first_submission(self):
        first,second=self.plan('first'),self.plan('second')
        self.ledger.reserve(second,self.now);self.ledger.finish(second['plan_id'],'confirmed','old-post')
        args=self.execution([first,second]);provider=Mock();provider.observation.return_value=self.observation(self.d)
        provider.submit.side_effect=lambda p,*_:self.receipt(p)
        with patch.object(safety,'require_live_publishing'),contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(safety.Blocked):runtime.run_batch([],args,provider_factory=lambda _:provider)
        provider.submit.assert_not_called()
    def test_execute_plan_rejects_conflicting_cli_channel(self):
        p=self.plan(destination=self.y);args=self.execution([p]);provider=Mock();provider.observation.return_value=self.observation(self.y)
        provider.submit.side_effect=lambda p,*_:self.receipt(p)
        argv=['bridge','--registry',str(self.registry),'--state',str(self.state),'--destination',self.other.alias,'--channel-id',self.other.youtube_id,'--execute-plan',str(args.execute_plan),'--approve-plan-id',args.approve_plan_id]
        actual_run=runtime.run_batch
        with patch.object(sys,'argv',argv),patch.object(safety,'require_live_publishing'),patch.object(runtime,'run_batch',side_effect=lambda ps,a:actual_run(ps,a,provider_factory=lambda _:provider)),contextlib.redirect_stdout(io.StringIO()):
            try:bridge.main()
            except safety.Blocked:pass
        provider.submit.assert_not_called()
    def test_explicit_schedule_conflict_rejected_in_offline_review(self):
        p=self.plan(destination=self.y);selection=self.root/'selection.json';snapshot=self.root/'snapshot.json'
        selection.write_text(json.dumps([{'video_path':p['file'],'video_id':'video-A','publish_at':self.due}]))
        other=self.youtube_snapshot();other['id']='video-B';other['status']['publishAt']=self.due
        snapshot.write_text(json.dumps({'channel_id':self.y.youtube_id,'videos':[self.youtube_snapshot(),other]}))
        argv=['post','--registry',str(self.registry),'--destination',self.y.alias,'--state',str(self.state),'--selection',str(selection),'--snapshot',str(snapshot),'--dry-run']
        real_prepare=safety.prepare
        with patch.object(sys,'argv',argv),patch.object(safety,'prepare',side_effect=lambda *a,**kw:real_prepare(*a,media_probe=lambda _:p['media'],**kw)),contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(safety.Blocked):post.main()
    def test_live_gate_still_blocks_without_any_patch(self):
        with self.assertRaisesRegex(safety.Blocked,'disabled'):safety.require_live_publishing()

    def test_missing_buffer_media_is_rejected(self):
        p=self.plan()
        with self.assertRaises(safety.Blocked):safety.require_buffer_media_binding(p,{})
    def test_multiple_buffer_assets_are_rejected(self):
        p=self.plan();asset={'source':safety.expected_media_url(p),'mimeType':'video/mp4'}
        with self.assertRaises(safety.Blocked):safety.require_buffer_media_binding(p,{'assets':[asset,asset]})
    def test_batch_cap_preflight_is_read_only(self):
        plans=[self.plan('first'),self.plan('second')];before=self.state.read_bytes()
        with self.assertRaisesRegex(safety.Blocked,'Global'):self.ledger.preflight(plans,self.now,global_cap=1)
        self.assertEqual(before,self.state.read_bytes());self.assertEqual(self.ledger.history(self.d.key),[])
    def test_matching_cli_destination_is_accepted(self):
        runtime.check_cli_destination(types.SimpleNamespace(destination=self.y.alias,channel_id=self.y.youtube_id,tz=self.y.timezone),self.y)
    def test_changed_legacy_mapping_does_not_clear_hold(self):
        p=self.plan(destination=self.y);legacy=self.root/'posted_log.json';legacy.write_text(json.dumps({'a':[], 'b':[]}))
        ledger=safety.Ledger(self.root/'migration/state.sqlite3');ledger.initialize([legacy]);remote=self.youtube_snapshot()
        ledger.adopt(self.y,p['sha256'],p['asset_id'],remote,'Synthetic reviewed mapping',legacy,'a')
        remote['id']='different-video'
        with self.assertRaises(safety.Blocked):ledger.adopt(self.y,p['sha256'],p['asset_id'],remote,'Different mapping',legacy,'b')
        self.assertEqual(ledger.summary(self.y.key)['unresolved_legacy_records'],1)
    def test_pending_identity_cannot_resolve_legacy_hold(self):
        p=self.plan(destination=self.y);self.ledger.reserve(p,self.now)
        with self.assertRaises(safety.Blocked):self.ledger.adopt(self.y,p['sha256'],p['asset_id'],self.youtube_snapshot(),'Synthetic mapping')
        self.assertEqual(self.ledger.history(self.y.key)[0]['status'],'submitting')

if __name__=='__main__':unittest.main(verbosity=2)
