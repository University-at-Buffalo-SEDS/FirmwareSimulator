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
def bootstrap_fixture():
 def event(prefix,**value):return prefix+json.dumps(value)
 common={'phase':7,'source':'main.initial_publish_current.before_workers','at_ms':0,'configured_sides':2,'discovered_announcers':0,'pending_owners':0}
 rows=[{'name':name,'id':ty,'packet_id':1000+i}for i,(name,ty)in enumerate(a.EXPECTED_MANAGED.items())]
 primary={'FLIGHT_STATE','AV_BAY_UNDERGLOW','FLIGHT_BUZZER','DAQ_LOG_CLOCK'}
 lines=[event(a.BOOT,event='begin',**common)]
 lines.append(event(a.REQ,event='begin',token=100,publisher='publish_current',request_ms=0,bootstrap_phase=7))
 def bind(row,token):return [event(a.REQ,event='bind',token=token,packet_id=row['packet_id'],data_type=row['id'],submit_ms=0),event(a.REQ,event='submit_result',token=token,packet_id=row['packet_id'],ok=True)]
 for row in rows:
  if row['name']in primary and row['name']!='DAQ_LOG_CLOCK':lines+=bind(row,100)
 lines.append(event(a.REQ,event='begin',token=101,publisher='set_daq_calibration',request_ms=0,bootstrap_phase=7))
 for row in rows:
  if row['name']not in primary:lines+=bind(row,101)
 lines.append(event(a.REQ,event='return',token=101,submissions=5))
 lines+=bind(next(row for row in rows if row['name']=='DAQ_LOG_CLOCK'),100)
 lines.append(event(a.REQ,event='return',token=100,submissions=4))
 lines.append(event(a.BOOT,event='seal',ok=True,cache=rows,meaning='LOCAL_CACHE_PRIMED; remote delivery not proven',**common))
 return lines,rows

def delivered_operation(start=100,pid=1):return req(1,pid,start)+[reg(pid,start),ack(pid,10,start+20,'{20: 0}'),ack(pid,20,start+40,'{}')]

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
 def test_exact_initial_local_cache_is_separate_from_real_operational_receipts(self):
  boot,rows=bootstrap_fixture();result=check(boot+delivered_operation())
  self.assertTrue(result['passed'],result);self.assertEqual(len(result['bootstrap_cache_initializations']),9)
  self.assertEqual(len(result['obligations']),1)
  self.assertTrue(all('remote delivery not proven'in row['meaning']for row in result['bootstrap_cache_initializations']))
 def test_every_accepted_bootstrap_id_still_has_all_owner500ms_gate(self):
  boot,rows=bootstrap_fixture();pid=rows[0]['packet_id']
  for delay in [501,707,763,814]:
   result=check(boot+[reg(pid,0),ack(pid,10,20,'{20: 0}'),ack(pid,20,delay,'{}')]+delivered_operation())
   self.assertFalse(result['passed']);self.assertTrue(any(f'{delay}ms'in error for error in result['errors']))
  self.assertFalse(check(boot+[reg(pid,0),ack(pid,10,20,'{20: 0}')]+delivered_operation())['passed'])
 def test_operational_missing_admission_never_waived_by_zero_clock_or_publisher_name(self):
  boot,_=bootstrap_fixture();later=req(2,2,0)
  value=json.loads(later[0].split(a.REQ)[1]);value['publisher']='publish_current';later[0]=a.REQ+json.dumps(value)
  self.assertFalse(check(boot+delivered_operation()+later)['passed'])
  value['bootstrap_phase']=7;later[0]=a.REQ+json.dumps(value)
  result=check(boot+delivered_operation()+later);self.assertFalse(result['passed']);self.assertTrue(any('outside one-shot'in error for error in result['errors']))
 def test_second_unsealed_or_unknown_reader_bootstrap_cannot_exempt_bindings(self):
  boot,_=bootstrap_fixture()
  for bad in [boot+boot,boot[:-1],boot[1:]]:self.assertFalse(check(bad+delivered_operation())['passed'])
  value=json.loads(boot[0].split(a.BOOT)[1]);value['discovered_announcers']=1
  self.assertFalse(check([a.BOOT+json.dumps(value)]+boot[1:]+delivered_operation())['passed'])
 def test_bad_cache_manifest_and_failed_submission_are_not_bootstrap_success(self):
  boot,_=bootstrap_fixture()
  for kind in ['missing','duplicate','wrong_type','failed']:
   changed=boot.copy();seal=json.loads(changed[-1].split(a.BOOT)[1])
   if kind=='missing':seal['cache'].pop()
   if kind=='duplicate':seal['cache'][1]['packet_id']=seal['cache'][0]['packet_id']
   if kind=='wrong_type':seal['cache'][0]['id']=123
   if kind=='failed':seal['ok']=False
   changed[-1]=a.BOOT+json.dumps(seal)
   self.assertFalse(check(changed+delivered_operation())['passed'])
  changed=boot.copy();at=next(i for i,line in enumerate(changed)if 'submit_result'in line);value=json.loads(changed[at].split(a.REQ)[1]);value['ok']=False;changed[at]=a.REQ+json.dumps(value)
  self.assertFalse(check(changed+delivered_operation())['passed'])
 def test_no_manifest_can_skip_an_unbound_managed_accepted_id(self):
  boot,_=bootstrap_fixture();result=check(boot+delivered_operation()+[reg(9000,0)])
  self.assertFalse(result['passed']);self.assertTrue(any('unknown API request'in error for error in result['errors']))
 def test_old509_preserved(self):
  r=check(req(1,13232732942222349579,3674)+[reg(13232732942222349579,3674,'{9644134183172063844: 0}'),ack(13232732942222349579,9644134183172063844,4182,'{}')]);self.assertFalse(r['passed']);self.assertEqual(r['obligations'][0]['latency_ms'],508)
if __name__=='__main__':unittest.main()
