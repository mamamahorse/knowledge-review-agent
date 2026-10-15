"""Controlled interface/lifecycle tests, not real model semantic evaluation."""
import json
import unittest
from contextlib import closing
from unittest.mock import patch
from fastapi.testclient import TestClient
from generation_budget import GenerationBudget
from learning_store import LearningStore
from learning_types import LearningDraft
from outline_store import OutlineStore, OutlineBusy
from outline_service import edit_candidate, public_outline
from outline_routes import (start_route,run_route,continue_route,edit_edge,add_edge,save_order,public_routes)
from outline_route_store import route_record,route_pages,route_edges,pause_route
from outline_route_model import RoutePage,prepare_page,validate_page,all_pairs,ordered_nodes
from learning_service import correct_grade, start_review
from test_practice import grade
import test_outline_groups as groups
import test_outlines as fixtures
from web_app import create_app


def controlled_route(messages, schema=RoutePage):
    data=json.loads(messages[1]['content'])
    cite={k:data['original_excerpts'][0][k] for k in ('source_id','line_start','line_end')}
    return schema(comparisons=[{'pair_index':p['index'],'relation':'a_before_b','basis':'source',
        'certainty':'tentative','reason':'Synthetic prior concept supports this target.','citations':[cite]} for p in data['comparisons']],
        missing_topics=[],note='Controlled fixture; no semantic assertion.')


class RouteTests(unittest.TestCase):
    setUp=fixtures.OutlineTests.setUp
    tearDown=fixtures.OutlineTests.tearDown
    current=fixtures.OutlineTests.current
    create=fixtures.OutlineTests.create
    generated=fixtures.OutlineTests.generated
    counts=fixtures.OutlineTests.counts
    prepare=groups.OutlineGroupTests.prepare
    decide=groups.OutlineGroupTests.decide
    enroll=groups.OutlineGroupTests.enroll

    def begin(self, limit=24):
        job,_=self.generated()
        key=start_route(self.store,job,'route','en',None,self.current,limit)
        return job,key

    def complete(self):
        job,key=self.begin()
        with patch('outline_routes._request_structured',side_effect=controlled_route):run_route(self.store,key,self.current)
        self.assertEqual(route_record(self.store,key)['status'],'completed')
        return job,key

    def public(self,job):return public_routes(self.store,job,self.current)['runs'][0]

    def edges(self,key):
        with closing(self.practice._connect()) as db:return route_edges(db,key)

    def review(self,edge,status='accepted',**changes):
        d={**edge['decision'],**changes}
        return edit_edge(self.store,edge['id'],edge['revision'],d['before_id'],d['after_id'],d['kind'],d['reason'],status,self.current)

    def answer(self,enrolled,assessment='developing',assistance='none_recorded',fit='within_scope',follow=False):
        sid=enrolled['session_id']
        g=grade(follow).model_copy(update={'overall_assessment':assessment,'target_fit':fit,'suggested_rating':'Good' if assessment=='meets_standard' else 'Again'})
        self.practice.submit_answer(sid,'The idea matters',assistance=assistance)
        self.practice.save_grade(sid,g,assessment,g.follow_up)
        return self.practice.get(sid)['questions'][0]

    def test_scope_navigation_pair_coverage_budget_and_no_enrollment(self):
        job,key=self.complete();r=self.public(job)
        self.assertEqual(r['cursor'],3);self.assertEqual(len(r['edges']),3)
        page=route_pages(self.store,key)[0];self.assertEqual([p['index'] for p in page['inputs']],[0,1,2])
        self.assertLessEqual(page['diagnostic']['estimated_input_tokens'],page['diagnostic']['input_limit'])
        self.assertLessEqual(page['diagnostic']['output_estimate'],page['diagnostic']['output_reserved'])
        self.assertEqual(self.counts(),[0]*5)
        self.assertEqual(r['recommendation']['facts'][0]['state'],'unseen')

    def test_reject_uncertain_then_include_and_use_duplicate_canonical(self):
        job,c,_,g=self.prepare('needs_review')
        with self.assertRaises(OutlineBusy):start_route(self.store,job,'bad','en',[c[0]['id']],self.current)
        self.decide(g,'rejected')
        key=start_route(self.store,job,'good','en',None,self.current)
        self.assertEqual(len(route_record(self.store,key)['inputs']['nodes']),3)
        pause_route(self.store,key)

    def test_pending_duplicate_exclusion_then_representative_only(self):
        job,c,_,g=self.prepare('duplicate_targets')
        self.assertEqual(public_routes(self.store,job,self.current)['eligible_ids'],[c[2]['id']])
        self.decide(g,canonical_id=c[1]['id'])
        key=start_route(self.store,job,'route','en',None,self.current)
        self.assertEqual([n['id'] for n in route_record(self.store,key)['inputs']['nodes']],[c[1]['id'],c[2]['id']])

    def test_schema_and_actual_evidence_validation(self):
        job,key=self.begin();r=route_record(self.store,key)
        pairs,msg,visible=prepare_page(GenerationBudget(),r,self.store.get(job),all_pairs(r['inputs']['nodes']))
        result=controlled_route(msg)
        for value in [result.model_copy(update={'comparisons':result.comparisons[:1]}),
                      result.model_copy(update={'comparisons':[result.comparisons[0]]*len(pairs)})]:
            with self.assertRaises(ValueError):validate_page(value,pairs,visible)
        c=result.comparisons[0]
        for modified in [c.model_copy(update={'citations':[]}),c.model_copy(update={'reason':' '}),
            c.model_copy(update={'citations':[c.citations[0].model_copy(update={'source_id':'invented'})]})]:
            with self.assertRaises(ValueError):validate_page(result.model_copy(update={'comparisons':[modified]+result.comparisons[1:]}),pairs,visible)
        allowed=c.model_copy(update={'basis':'pedagogical','citations':[]})
        self.assertTrue(validate_page(result.model_copy(update={'comparisons':[allowed]+result.comparisons[1:]}),pairs,visible))

    def test_budget_routes_by_output_and_rejects_minimum_insufficient(self):
        job,key=self.begin();r=route_record(self.store,key);pairs=all_pairs(r['inputs']['nodes'])
        chosen,msg,_=prepare_page(GenerationBudget(32768,2000,2048),r,self.store.get(job),pairs)
        self.assertEqual(len(chosen),1)
        with self.assertRaises(ValueError):prepare_page(GenerationBudget(32768,1000,2048),r,self.store.get(job),pairs)
        with self.assertRaises(ValueError):prepare_page(GenerationBudget(2000,1400,80),r,self.store.get(job),pairs)

    def test_accept_edit_skip_original_attribution_and_revision(self):
        job,key=self.complete();edge=self.edges(key)[0];self.review(edge)
        edge=self.edges(key)[0];original=edge['model']
        self.review(edge,'proposed',reason='My teaching preference',kind='helpful_first')
        edited=self.edges(key)[0]
        self.assertEqual(edited['model'],original)
        self.assertEqual(edited['decision']['basis'],'user');self.assertEqual(edited['decision']['citations'],[])
        with self.assertRaises(OutlineBusy):self.review(edge)
        self.review(edited,'rejected');self.assertEqual(self.edges(key)[0]['status'],'rejected')
        self.assertEqual(self.counts(),[0]*5)

    def test_cycles_are_reviewable_but_cannot_accept_closing_edge(self):
        _,key=self.complete();a,b,c=self.edges(key)
        self.review(a);self.review(c)
        with self.assertRaises(ValueError):self.review(b,before_id=b['decision']['after_id'],after_id=b['decision']['before_id'])
        self.assertEqual(self.edges(key)[1]['status'],'proposed')
        ids=[n['id'] for n in route_record(self.store,key)['inputs']['nodes']]
        _,cycle=ordered_nodes(ids,[a['decision'],c['decision'],{**b['decision'],'before_id':ids[2],'after_id':ids[0]}])
        self.assertTrue(cycle)

    def test_uncertain_relation_needs_edit_to_accept(self):
        _,key=self.complete();e=self.edges(key)[0]
        self.review(e,'proposed',kind='uncertain');e=self.edges(key)[0]
        with self.assertRaises(ValueError):self.review(e)
        self.review(e,'accepted',kind='helpful_first')

    def test_manual_relationship_duplicate_and_recover_after_reopen(self):
        job,key=self.complete();e=self.edges(key)[0];self.review(e,'rejected')
        r=route_record(self.store,key);a,b=[n['id'] for n in r['inputs']['nodes'][:2]]
        manual=add_edge(self.store,key,r['revision'],a,b,'helpful_first','I prefer this order',self.current)
        reopened=OutlineStore(LearningStore(self.practice.path))
        with closing(self.practice._connect()) as db:new=next(x for x in route_edges(db,key) if x['id']==manual)
        self.assertIsNone(new['model']);self.assertEqual(new['status'],'proposed')
        self.assertEqual(public_routes(reopened,job,self.current)['runs'][0]['id'],key)
        with self.assertRaises(ValueError):add_edge(self.store,key,r['revision']+1,a,b,'helpful_first','Again',self.current)

    def test_order_conflict_allowed_skip_restore_no_fsrs_mutation(self):
        job,key=self.complete();e=self.edges(key)[0];self.review(e);r=self.public(job)
        save_order(self.store,key,r['revision'],r['order'][::-1],[r['order'][0]],self.current)
        r=self.public(job);self.assertTrue(r['order_conflicts']);self.assertEqual(r['order_mode'],'user')
        self.assertNotEqual(r['recommendation']['candidate_id'],r['skipped'][0])
        save_order(self.store,key,r['revision'],None,[],self.current)
        self.assertEqual(self.public(job)['skipped'],[]);self.assertEqual(self.counts(),[0]*5)

    def test_order_strict_permutation_unknown_skip_and_cas(self):
        job,key=self.complete();r=self.public(job)
        for order,skip in [(r['order'][:1],[]),(r['order'],['invented']),(r['order'],[r['order'][0]]*2)]:
            with self.assertRaises(ValueError):save_order(self.store,key,r['revision'],order,skip,self.current)
        save_order(self.store,key,r['revision'],None,[],self.current)
        with self.assertRaises(OutlineBusy):save_order(self.store,key,r['revision'],None,[],self.current)

    def test_request_idempotence_repeated_run_does_not_duplicate(self):
        job,key=self.complete()
        self.assertEqual(start_route(self.store,job,'route','en',None,self.current),key)
        with patch('outline_routes._request_structured') as m:run_route(self.store,key,self.current);m.assert_not_called()
        self.assertEqual(len(self.edges(key)),3)

    def test_allowance_pause_and_continue_reuses_completed_pages(self):
        job,key=self.begin(1)
        budget=GenerationBudget(32768,2000,2048)
        with patch('outline_routes.GenerationBudget.from_env',return_value=budget),patch('outline_routes._request_structured',side_effect=controlled_route):
            run_route(self.store,key,self.current)
            self.assertEqual(route_record(self.store,key)['status'],'paused');self.assertEqual(route_record(self.store,key)['cursor'],1)
            first=route_pages(self.store,key)[0]
            continue_route(self.store,key,self.current,2);run_route(self.store,key,self.current)
        self.assertEqual(self.public(job)['cursor'],3);self.assertEqual(route_pages(self.store,key)[0],first)

    def test_failure_retry_keeps_complete_page_no_duplicate(self):
        job,key=self.begin();calls=[]
        def failing(m,s):
            calls.append(m)
            if len(calls)==2:raise RuntimeError('provider unavailable')
            return controlled_route(m,s)
        with patch('outline_routes.GenerationBudget.from_env',return_value=GenerationBudget(32768,2000,2048)),patch('outline_routes._request_structured',side_effect=failing):run_route(self.store,key,self.current)
        self.assertEqual(route_record(self.store,key)['status'],'failed');self.assertEqual(route_record(self.store,key)['cursor'],1)
        page=route_pages(self.store,key)[1]
        continue_route(self.store,key,self.current)
        with patch('outline_routes._request_structured',side_effect=controlled_route):run_route(self.store,key,self.current)
        self.assertEqual(route_pages(self.store,key)[1]['id'],page['id']);self.assertEqual(len(self.edges(key)),3)

    def test_truncated_output_shrinks_without_publishing_partial(self):
        job,key=self.begin();sizes=[]
        def truncated(m,s):
            size=len(json.loads(m[1]['content'])['comparisons']);sizes.append(size)
            if size>1:raise RuntimeError('The model response was truncated.')
            return controlled_route(m,s)
        with patch('outline_routes._request_structured',side_effect=truncated):run_route(self.store,key,self.current)
        self.assertEqual(sizes,[3,1,1,1]);self.assertEqual(self.public(job)['cursor'],3)
        self.assertEqual(route_record(self.store,key)['calls_used'],4)

    def test_no_network_write_lock_pause_invalidates_late_result(self):
        job,key=self.begin()
        def late(m,s):
            self.library.create('network writer','root');pause_route(self.store,key)
            return controlled_route(m,s)
        with patch('outline_routes._request_structured',side_effect=late):run_route(self.store,key,self.current)
        self.assertEqual(route_record(self.store,key)['status'],'paused');self.assertEqual(self.edges(key),[])

    def test_candidate_change_during_network_revokes_late_result(self):
        job,key=self.begin();c=self.store.get(job)['candidates'][0]
        def changed(m,s):
            draft=LearningDraft.model_validate(c['learning'])
            edit_candidate(self.store,c['id'],draft.model_copy(update={'target':draft.target.model_copy(update={'title':'Changed'})}),c['revision'])
            return controlled_route(m,s)
        with patch('outline_routes._request_structured',side_effect=changed):run_route(self.store,key,self.current)
        self.assertEqual(route_record(self.store,key)['status'],'failed');self.assertTrue(self.public(job)['stale']);self.assertEqual(self.edges(key),[])

    def test_scope_change_stale_no_recommend_or_edit(self):
        job,key=self.complete();self.snapshots=[{**s,'source_id':'changed'} for s in self.snapshots]
        r=self.public(job);self.assertTrue(r['stale']);self.assertIsNone(r['recommendation'])
        with self.assertRaises(OutlineBusy):self.review(self.edges(key)[0])

    def test_unknown_pending_no_fake_due_enrollment_keeps_stamp(self):
        job,key=self.complete();c=self.store.get(job)['candidates'][0];enrolled=self.enroll(c)
        r=self.public(job);self.assertFalse(r['stale']);self.assertEqual(r['recommendation']['kind'],'continue')
        self.assertEqual(r['recommendation']['pending_session_id'],enrolled['session_id'])
        self.assertEqual(r['recommendation']['facts'][0]['state'],'unseen');self.assertFalse(r['recommendation']['facts'][0]['due'])

    def test_current_effective_score_correction_changes_recommend_without_model(self):
        job,key=self.complete();enrolled=self.enroll(self.store.get(job)['candidates'][0]);q=self.answer(enrolled)
        self.assertEqual(self.public(job)['recommendation']['kind'],'address_gap')
        with patch('outline_routes._request_structured') as m:
            correct_grade(self.practice,enrolled['session_id'],q['id'],{'overall_assessment':'meets_standard','suggested_rating':'Good'},'Reviewed the actual answer',1)
            r=self.public(job);m.assert_not_called()
        self.assertEqual(r['recommendation']['facts'][0]['state'],'supported')
        self.assertEqual(r['recommendation']['facts'][0]['evidence']['revision'],2)
        self.assertNotEqual(r['recommendation']['candidate_id'],r['order'][0])

    def test_hint_outside_scope_uncertain_are_insufficient_not_wrong(self):
        job,key=self.complete()
        for c,assessment,assistance,fit in zip(self.store.get(job)['candidates'],['meets_standard','developing','uncertain'],['hint_used','none_recorded','none_recorded'],['within_scope','outside_scope','within_scope']):
            self.answer(self.enroll(c),assessment,assistance,fit)
        r=self.public(job)['recommendation'];self.assertTrue(all(f['state']=='insufficient' and not f['due'] for f in r['facts']))
        self.assertEqual(r['kind'],'explore')

    def test_followup_does_not_erase_primary_or_count_as_proof(self):
        job,key=self.complete();enrolled=self.enroll(self.store.get(job)['candidates'][0]);self.answer(enrolled,follow=True)
        self.practice.continue_follow_up(enrolled['session_id'])
        self.practice.submit_answer(enrolled['session_id'],'The idea matters')
        g=grade(False).model_copy(update={'overall_assessment':'meets_standard','suggested_rating':'Good'})
        self.practice.save_grade(enrolled['session_id'],g,'meets_standard',None)
        self.assertEqual(self.public(job)['recommendation']['facts'][0]['state'],'gap')

    def test_due_uses_actual_review_event_and_skip_does_not_pause_item(self):
        job,key=self.complete();c=self.store.get(job)['candidates'][0];enrolled=self.enroll(c);self.answer(enrolled,'meets_standard')
        with self.practice.transaction() as db:db.execute("UPDATE review_items SET next_review_at='2020-01-01T00:00:00+00:00' WHERE id=?",(enrolled['item_id'],))
        r=self.public(job);self.assertEqual(r['recommendation']['kind'],'review')
        save_order(self.store,key,r['revision'],None,[c['id']],self.current)
        self.assertNotEqual(self.public(job)['recommendation']['candidate_id'],c['id'])
        self.assertEqual(self.practice.item(enrolled['item_id'])['status'],'active')
        # Direct study is available even after a route skip.
        self.assertTrue(start_review(self.practice,enrolled['item_id'],mode='original')['id'])

    def test_accepted_prerequisite_is_soft_advice_with_user_order(self):
        job,key=self.complete();e=self.edges(key)[0];self.review(e)
        r=self.public(job);a,b=e['decision']['before_id'],e['decision']['after_id']
        order=[b,a]+[x for x in r['order'] if x not in (a,b)]
        save_order(self.store,key,r['revision'],order,[],self.current)
        self.assertEqual(self.public(job)['recommendation']['kind'],'check_prerequisite')

    def test_old_process_recovery_and_api_schema_errors(self):
        job,key=self.begin()
        with self.practice.transaction() as db:db.execute("UPDATE outline_routes SET status='running',attempt='lost' WHERE id=?",(key,))
        self.assertEqual(self.public(job)['status'],'failed')
        app=create_app(self.root/'uploads')
        with TestClient(app) as client:
            self.assertEqual(client.post('/api/outlines/missing/routes',json={'request_id':'x','call_limit':0}).status_code,422)
            self.assertEqual(client.post('/api/outlines/missing/routes',json={'request_id':'x'}).status_code,404)
            self.assertEqual(client.patch('/api/outline-routes/missing/order',json={'revision':1,'order':[],'skipped':[]}).status_code,404)
            self.assertEqual(client.patch('/api/outline-route-edges/missing',json={'revision':1,'before_id':'a','after_id':'b','kind':'prerequisite','reason':'x','decision':'accepted','unknown':1}).status_code,422)

    def test_http_complete_review_order_and_no_implicit_enrollment(self):
        from quality_route import CASES,seed_outline
        from sources import load_sources
        notes=self.root/'uploads';folder=notes/('a'*32);folder.mkdir(parents=True)
        file=folder/'route.md';file.write_text(CASES['chain']['text'],encoding='utf-8')
        source=load_sources([file])[0]
        snapshots=[{'document_id':'upload-'+folder.name,'source_id':source.source_id,'path':str(file),'name':file.name,'text':source.text}]
        app=create_app(notes,self.practice.path,self.root/'conversations.sqlite3')
        collection=app.state.library_store.create('Route HTTP fixture')['id']
        app.state.library_store.assign_documents([snapshots[0]['document_id']],collection)
        job,_=seed_outline(app.state.outline_store,CASES['chain'],source=source,snapshots=snapshots,collection=collection)
        with TestClient(app) as client,patch('outline_routes._request_structured',side_effect=controlled_route):
            payload={'request_id':'http-route','response_language':'en','call_limit':2}
            response=client.post(f'/api/outlines/{job}/routes',json=payload);self.assertEqual(response.status_code,202)
            key=response.json()['id']
            self.assertEqual(client.post(f'/api/outlines/{job}/routes',json=payload).json()['id'],key)
            r=client.get(f'/api/outlines/{job}').json()['routes']['runs'][0];self.assertEqual(r['status'],'completed')
            e=r['edges'][0]
            self.assertEqual(client.patch(f"/api/outline-route-edges/{e['id']}",json={**{k:e['decision'][k] for k in ('before_id','after_id','kind','reason')},'decision':'accepted','revision':1}).status_code,200)
            r=client.get(f'/api/outlines/{job}').json()['routes']['runs'][0]
            self.assertEqual(client.patch(f'/api/outline-routes/{key}/order',json={'revision':r['revision'],'order':r['order'][::-1],'skipped':[]}).status_code,200)
            self.assertEqual(client.post(f'/api/outline-routes/{key}/edges',json={'revision':r['revision']+1,'before_id':r['order'][2],'after_id':r['order'][0],'kind':'helpful_first','reason':'User preference'}).status_code,201)
        self.assertEqual(self.counts(),[0]*5)

    def test_active_generation_is_explicitly_rejected(self):
        job,key=self.begin()
        with self.assertRaises(OutlineBusy):start_route(self.store,job,'second','en',None,self.current)
        pause_route(self.store,key)
        other=start_route(self.store,job,'second','en',None,self.current)
        with self.assertRaises(OutlineBusy):continue_route(self.store,key,self.current)
        self.assertNotEqual(other,key)

    def test_single_target_finishes_without_a_model_call(self):
        job,_=self.generated();c=self.store.get(job)['candidates'][0]
        key=start_route(self.store,job,'single','zh',[c['id']],self.current)
        with patch('outline_routes._request_structured') as model:run_route(self.store,key,self.current);model.assert_not_called()
        self.assertEqual(self.public(job)['status'],'completed');self.assertEqual(self.public(job)['total_pairs'],0)

    def test_accepted_group_binding_on_enroll_keeps_route_but_decision_change_invalidates(self):
        job,c,_,g=self.prepare(different=True);self.decide(g)
        key=start_route(self.store,job,'route','en',None,self.current)
        with patch('outline_routes._request_structured',side_effect=controlled_route):run_route(self.store,key,self.current)
        self.enroll(c[0]);self.assertFalse(self.public(job)['stale'])
        from outline_group_store import rows
        self.decide(rows(self.store,job)[1][0],'rejected');self.assertTrue(self.public(job)['stale'])

    def test_latest_hinted_attempt_does_not_reuse_old_support(self):
        job,key=self.complete();c=self.store.get(job)['candidates'][0];enrolled=self.enroll(c);self.answer(enrolled,'meets_standard')
        next_session=start_review(self.practice,enrolled['item_id'],mode='original')
        self.answer({'session_id':next_session['id']},'meets_standard','hint_used')
        self.assertEqual(self.public(job)['recommendation']['facts'][0]['state'],'insufficient')

    def test_repeated_resumption_with_queued_job_does_not_increase_allowance(self):
        _,key=self.begin(1);continue_route(self.store,key,self.current,200)
        self.assertEqual(route_record(self.store,key)['allowance'],1)


if __name__=='__main__':unittest.main()
