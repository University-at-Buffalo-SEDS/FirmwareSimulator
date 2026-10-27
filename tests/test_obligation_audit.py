import importlib.util,json,unittest
from pathlib import Path
spec=importlib.util.spec_from_file_location('audit',Path(__file__).parents[1]/'scripts/audit_obligations.py');a=importlib.util.module_from_spec(spec);spec.loader.exec_module(a)
STATE_ID=a.EXPECTED_MANAGED['FLIGHT_STATE']
EXCLUDED_ID=a.EXPECTED_EXCLUDED['FLIGHT_COMMAND']
def scope():
 return a.SCOPE+json.dumps({'version':1,'managed_limit_ms':500,'covered':[{'name':name,'id':ty}for name,ty in a.EXPECTED_MANAGED.items()],'excluded':[{'name':name,'id':ty}for name,ty in a.EXPECTED_EXCLUDED.items()]})
def check(lines,**kwargs):return a.audit([scope()]+lines,**kwargs)
def req(token,pid,start,submit=None):
 return [a.REQ+json.dumps(x) for x in [{'event':'begin','token':token,'request_ms':start,'publisher':'controlled' if token==1 else 'automatic'}, {'event':'bind','token':token,'packet_id':pid,'submit_ms':start if submit is None else submit}, {'event':'submit_result','token':token,'packet_id':pid,'ok':True},{'event':'return','token':token,'submissions':1}]]
def reg(pid,t,targets='{10: 0, 20: 0}'):return f'[sedsnet-reliable] register time={t} type={STATE_ID} id={pid:#x} waiting=false targets={targets}'
def ack(pid,owner,t,remaining):return f'[sedsnet-reliable] receipt time={t} id={pid:#x} sender={owner:#x} matched=true remaining={remaining}'
class Tests(unittest.TestCase):
 def test_background_own_request_is_separate_and_fill_owner_required(self):
  lines=req(1,101,3251)+[reg(101,3252),ack(101,10,3492,'{20: 0}'),ack(101,20,3560,'{}')]+req(2,102,3500)+[reg(102,3501),ack(102,10,3838,'{20: 0}'),ack(102,20,3900,'{}')]
  result=check(lines);self.assertTrue(result['passed'],result);self.assertEqual([r['latency_ms'] for r in result['obligations']],[309,400]);self.assertGreater(3900-3251,500) # old global origin mislabels valid later obligation
 def test_unrecorded_api_not_substituted_by_uart(self):
  r=check([reg(1,20),ack(1,10,30,'{20: 0}'),ack(1,20,40,'{}')]);self.assertFalse(r['passed']);self.assertIn('unknown API',r['errors'][0])
 def test_missing_owner_expiry_or_pending_disappearance_never_success(self):
  r=check(req(1,1,0)+[reg(1,1),ack(1,10,30,'{20: 0}'),'pending=[]']);self.assertFalse(r['passed']);self.assertTrue(any('unresolved' in e for e in r['errors']))
 def test_background_over500_not_ignored(self):
  r=check(req(2,2,100)+[reg(2,101),ack(2,10,300,'{20: 0}'),ack(2,20,601,'{}')]);self.assertFalse(r['passed']);self.assertTrue(any('501ms' in e for e in r['errors']))
 def test_pre_persistence_time_charged(self):
  r=check(req(1,1,0,200)+[reg(1,201),ack(1,10,499,'{20: 0}'),ack(1,20,501,'{}')]);self.assertFalse(r['passed'])
 def test_unknown_request_clock_failed_binding_and_no_trace_fail(self):
  for lines in [req(1,1,None)+[reg(1,1),ack(1,10,30,'{20: 0}'),ack(1,20,40,'{}')],req(1,1,0),[],[a.REQ+json.dumps({'event':'begin','token':1,'request_ms':0,'publisher':'failure'}),a.REQ+json.dumps({'event':'return','token':1,'submissions':0})]]:self.assertFalse(check(lines)['passed'])
 def test_empty_remaining_cannot_hide_earlier_owner(self):
  self.assertFalse(check(req(1,1,0)+[reg(1,1),ack(1,20,40,'{}')])['passed'])
 def test_missing_controlled_round_and_no_owners_cannot_vacuously_pass(self):
  self.assertFalse(check(req(1,1,0)+[reg(1,1,'{}')])['passed'])
  lines=req(1,1,0)+[reg(1,1),ack(1,10,30,'{20: 0}'),ack(1,20,40,'{}')]
  self.assertFalse(check(lines,expect_rounds=3)['passed'])
 def test_nonmanaged_command_exclusion_is_type_family_only_and_explicit(self):
  lines=req(1,1,0)+[reg(1,1),ack(1,10,30,'{20: 0}'),ack(1,20,40,'{}')]
  excluded=reg(90,1).replace(f'type={STATE_ID}',f'type={EXCLUDED_ID}')
  result=check(lines+[excluded]);self.assertTrue(result['passed'],result);self.assertEqual(result['excluded_obligations'][0]['packet_id'],90)
  self.assertFalse(check(lines+[reg(91,2)])['passed']) # another managed ID never skipped
  self.assertFalse(check(lines+[reg(92,2).replace(f'type={STATE_ID}','type=999')])['passed'])
 def test_scope_must_cover_all_managed_types_and_never_reclassify_one(self):
  lines=req(1,1,0)+[reg(1,1),ack(1,10,30,'{20: 0}'),ack(1,20,40,'{}')]
  self.assertFalse(a.audit(lines)['passed'])
  wrong=json.loads(scope().split(a.SCOPE)[1]);wrong['covered'].pop()
  self.assertFalse(a.audit([a.SCOPE+json.dumps(wrong)]+lines)['passed'])
  wrong=json.loads(scope().split(a.SCOPE)[1]);wrong['excluded'][0]['id']=STATE_ID
  self.assertFalse(a.audit([a.SCOPE+json.dumps(wrong)]+lines)['passed'])
 def test_runtime_scope_ids_cannot_move_managed_traffic_into_excluded_family(self):
  wrong=json.loads(scope().split(a.SCOPE)[1]);wrong['covered'][0]['id']=123456
  result=a.audit([a.SCOPE+json.dumps(wrong)]+req(1,1,0)+[reg(1,1),ack(1,10,30,'{20: 0}'),ack(1,20,40,'{}')])
  self.assertFalse(result['passed']);self.assertTrue(any('frozen application schema' in e for e in result['errors']))
 def test_old509_preserved(self):
  r=check(req(1,13232732942222349579,3674)+[reg(13232732942222349579,3674,'{9644134183172063844: 0}'),ack(13232732942222349579,9644134183172063844,4182,'{}')]);self.assertFalse(r['passed']);self.assertEqual(r['obligations'][0]['latency_ms'],508)
if __name__=='__main__':unittest.main()
