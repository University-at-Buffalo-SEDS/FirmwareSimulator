#!/usr/bin/env python3
"""Qualification-only request/accepted-target/all-side receipt audit. Never infers ACKs from disappearance."""
import argparse,json,re
from pathlib import Path
REQ='[gs-qualification-request] '
SCOPE='[gs-qualification-scope] '
BOOT='[gs-qualification-bootstrap] '
MANAGED_NAMES={'FLIGHT_STATE','AV_BAY_UNDERGLOW','FLIGHT_BUZZER','DAQ_LOADCELL_CALIBRATION','DAQ_KG50_CALIBRATION','DAQ_LOG_CLOCK','DAQ_THERMAL_CALIBRATION','DAQ_FILTER_CALIBRATION','DAQ_KG50_INPUT'}
EXCLUDED_NAMES={'FLIGHT_COMMAND','VALVE_COMMAND','ACTUATOR_COMMAND'}
# Frozen from the actual GS telemetry_config runtime registry under SDK4.3.0/ad08676.
# The trace cannot reclassify a managed ID by merely claiming a different name.
EXPECTED_MANAGED={'FLIGHT_STATE': 256715704, 'AV_BAY_UNDERGLOW': 68266860, 'FLIGHT_BUZZER': 169036741, 'DAQ_LOADCELL_CALIBRATION': 189079860, 'DAQ_KG50_CALIBRATION': 166428631, 'DAQ_LOG_CLOCK': 206396146, 'DAQ_THERMAL_CALIBRATION': 194365925, 'DAQ_FILTER_CALIBRATION': 46105338, 'DAQ_KG50_INPUT': 125302539}
EXPECTED_EXCLUDED={'FLIGHT_COMMAND': 223048036, 'VALVE_COMMAND': 245583120, 'ACTUATOR_COMMAND': 218702105}
REG=re.compile(r'\[sedsnet-reliable\] register time=(\d+) type=(\d+) id=(0x[0-9a-f]+) waiting=(true|false) targets=(.*)')
ACK=re.compile(r'\[sedsnet-reliable\] receipt time=(\d+) id=(0x[0-9a-f]+) sender=(0x[0-9a-f]+) matched=(true|false) remaining=(.*)')
def owners(text):
    # SDK Debug emits BTreeMap<u64, ...>; keys only, never timestamps/values.
    return {int(x) for x in re.findall(r'(\d+):',text)}
def verified_bootstrap(events, requests, bindings, results, returned, errors):
    """Only an exact, sealed initial local-cache manifest can lack remote admission."""
    tagged={token:request for token,request in requests.items() if request.get('bootstrap_phase') is not None}
    if not events:
        if tagged:errors.append('bootstrap-tagged API request has no lifecycle proof')
        return set()
    initial_errors=len(errors)
    if len(events)!=2 or [event.get('event') for event in events]!=['begin','seal']:
        errors.append('bootstrap lifecycle must begin and seal exactly once');return set()
    begin,seal=events
    phase=begin.get('phase')
    if type(phase) is not int or phase<=0 or seal.get('phase')!=phase:errors.append('invalid bootstrap phase identity')
    for event in events:
        if event.get('source')!='main.initial_publish_current.before_workers':errors.append('invalid bootstrap source boundary')
        if (event.get('configured_sides'),event.get('discovered_announcers'),event.get('pending_owners'))!=(2,0,0):errors.append('bootstrap requires two sides and no learned readers/pending owners')
        if type(event.get('at_ms')) is not int or event['at_ms']<0:errors.append('bootstrap requires actual API clock')
    if seal.get('ok') is not True:errors.append('bootstrap cache priming failed')
    if type(begin.get('at_ms')) is int and type(seal.get('at_ms')) is int and seal['at_ms']<begin['at_ms']:errors.append('bootstrap clock reversed')
    manifest=seal.get('cache',[])
    if not isinstance(manifest,list) or len(manifest)!=9 or {row.get('name')for row in manifest}!=MANAGED_NAMES:
        errors.append('bootstrap manifest must contain all nine managed cache types');return set()
    by_id={}
    for row in manifest:
        packet_id=row.get('packet_id')
        if EXPECTED_MANAGED.get(row.get('name'))!=row.get('id') or type(packet_id) is not int or packet_id<0 or packet_id in by_id:
            errors.append('invalid/duplicate bootstrap cache type or PacketID')
        else:by_id[packet_id]=row
    phase_bindings={pid:binding for pid,binding in bindings.items() if binding['token']in tagged}
    if set(phase_bindings)!=set(by_id):errors.append('bootstrap manifest does not match exact submitted cache PacketIDs')
    for token,request in tagged.items():
        if request.get('bootstrap_phase')!=phase or not begin['_line']<request['_line']<seal['_line']:
            errors.append('API request outside one-shot bootstrap boundary')
        if token not in returned or not begin['_line']<returned[token]['_line']<seal['_line']:errors.append('bootstrap API request did not return inside boundary')
        if type(request.get('request_ms')) is not int or not begin.get('at_ms',0)<=request['request_ms']<=seal.get('at_ms',-1):errors.append('bootstrap API request clock outside lifecycle')
    primary_names={'FLIGHT_STATE','AV_BAY_UNDERGLOW','FLIGHT_BUZZER','DAQ_LOG_CLOCK'}
    for packet_id,binding in phase_bindings.items():
        row=by_id.get(packet_id,{})
        expected_publisher='publish_current' if row.get('name')in primary_names else 'set_daq_calibration'
        if (requests[binding['token']].get('publisher')!=expected_publisher or binding.get('data_type')!=row.get('id')
                or results.get(packet_id) is not True or not begin['_line']<binding['_line']<seal['_line']):
            errors.append('bootstrap cache binding lacks matching successful real API submission')
    return set(by_id) if len(errors)==initial_errors else set()

def audit(lines,limit=500,expect_rounds=0):
    requests={};bindings={};accepted={};errors=[];results={};returned={};settles=[];scope=None;bootstrap=[]
    for index,line in enumerate(lines):
        if BOOT in line:
            event=json.loads(line.split(BOOT,1)[1]);event['_line']=index;bootstrap.append(event)
        if SCOPE in line:
            value=json.loads(line.split(SCOPE,1)[1])
            if scope is not None and scope!=value:errors.append('inconsistent runtime audit scope')
            scope=value
        if REQ in line:
            x=json.loads(line.split(REQ,1)[1]);event=x['event'];token=x['token']
            if event=='begin':
                if token in requests:errors.append(f'duplicate request token {token}')
                x['_line']=index;requests[token]=x
            elif event=='bind':
                pid=x['packet_id']
                if pid in bindings:errors.append(f'duplicate packet submission {pid}')
                x['_line']=index;bindings[pid]=x
            elif event=='submit_result':results[x['packet_id']]=x['ok']
            elif event=='return':
                x['_line']=index;returned[token]=x
        m=REG.search(line)
        if m:
            at,ty,pid,waiting,targets=m.groups();pid=int(pid,16)
            if pid in accepted:errors.append(f'duplicate reliable admission {pid}')
            accepted[pid]={'packet_id':pid,'accepted_ms':int(at),'data_type':int(ty),'targets':sorted(owners(targets)),'receipts':{},'waiting':waiting=='true'}
        m=ACK.search(line)
        if m:
            at,pid,owner,matched,remaining=m.groups();pid=int(pid,16);owner=int(owner,16)
            if matched=='true' and pid in accepted:
                row=accepted[pid]
                if owner not in row['targets']:errors.append(f'unregistered owner {owner} for {pid}')
                row['receipts'].setdefault(owner,int(at))
                # Empty remaining alone is not sufficient: every frozen owner must match.
                if not owners(remaining) and set(row['receipts'])==set(row['targets']):row['completion_ms']=int(at)
        if 'full-bay prior reliable traffic drained' in line or 'missed latency deadline' in line:settles.append(line.rstrip())
    boot_ids=verified_bootstrap(bootstrap,requests,bindings,results,returned,errors)
    covered={};excluded={}
    if scope is None:
        errors.append('missing actual runtime managed-family audit scope')
    else:
        for key,names,destination in [('covered',MANAGED_NAMES,covered),('excluded',EXCLUDED_NAMES,excluded)]:
            rows=scope.get(key,[])
            if len(rows)!=len(names) or {row.get('name') for row in rows}!=names:
                errors.append(f'incomplete/unknown {key} type names')
            for row in rows:
                ty=row.get('id')
                if type(ty) is not int or ty<=0 or ty in destination:errors.append(f'invalid/duplicate {key} runtime type ID')
                else:
                    expected=EXPECTED_MANAGED if key=='covered' else EXPECTED_EXCLUDED
                    if expected.get(row.get('name'))!=ty:errors.append(f'{key} runtime ID differs from frozen application schema: {row.get("name")}')
                    destination[ty]=row.get('name')
        if set(covered)&set(excluded):errors.append('managed type cannot be excluded')
        if scope.get('version')!=1 or scope.get('managed_limit_ms')!=500 or limit!=500:errors.append('audit requires exact500ms managed scope version1')
    covered_rows={};excluded_rows=[]
    for pid,row in accepted.items():
        ty=row['data_type']
        if ty in excluded and ty not in covered:
            row['name']=excluded[ty]
            row['scope']='Excluded typed command: original physical/status/execution and RF-soak gates unchanged; no new per-packet500ms claim'
            excluded_rows.append(row)
        elif ty in covered:
            row['name']=covered[ty];covered_rows[pid]=row
        else:errors.append(f'accepted type outside declared managed/excluded families: {ty}, packet {pid}')
    for pid,row in covered_rows.items():
        binding=bindings.get(pid);request=requests.get(binding['token']) if binding else None
        if not request or request.get('request_ms') is None:
            row['request_ms']=None;errors.append(f'unknown API request for accepted packet {pid}')
        else:
            row['request_ms']=request['request_ms'];row['publisher']=request['publisher'];row['controlled_round']=request.get('controlled_round')
            row['submission_ms']=binding.get('submit_ms')
            if results.get(pid) is not True:errors.append(f'missing/failed submit for {pid}')
            if row['accepted_ms']<row['request_ms']:errors.append(f'clock/order violation {pid}')
        if not row['targets']:errors.append(f'no destination owners admitted for {pid}; cannot qualify end-to-end control')
        if row['targets'] and 'completion_ms' not in row:errors.append(f'unresolved destinations {pid}: {sorted(set(row["targets"])-set(row["receipts"]))}')
        if 'completion_ms' in row and row['request_ms'] is not None:
            row['latency_ms']=row['completion_ms']-row['request_ms']
            if not 0<=row['latency_ms']<=limit:errors.append(f'latency {pid}: {row["latency_ms"]}ms exceeds {limit}')
    for pid,binding in bindings.items():
        if pid not in covered_rows and pid not in boot_ids:errors.append(f'API packet has no accepted managed obligation {pid}')
    for token,request in requests.items():
        if not any(x['token']==token for x in bindings.values()):errors.append(f'API request unbound/failed before submit {token}')
        if token not in returned:errors.append(f'API request not returned {token}')
    if expect_rounds:
        for round in range(1,expect_rounds+1):
            rows=[row for row in covered_rows.values() if row.get('controlled_round')==round]
            names=[row.get('publisher') for row in rows]
            if sorted(names)!=['set_flight_buzzer','set_flight_state','set_underglow']:
                errors.append(f'controlled round {round} missing/duplicate publisher IDs: {names}')
        if any(row.get('controlled_round') is not None and not 1<=row['controlled_round']<=expect_rounds for row in covered_rows.values()):errors.append('unexpected controlled round')
    if not covered_rows:errors.append('no accepted managed obligations observed')
    return {'passed':not errors,'limit_ms':limit,'obligations':list(covered_rows.values()),'excluded_obligations':excluded_rows,'bootstrap_cache_initializations':[{'packet_id':pid,'meaning':'LOCAL_CACHE_PRIMED; remote delivery not proven'}for pid in sorted(boot_ids) if pid not in covered_rows],'bootstrap_lifecycle':bootstrap,'runtime_type_scope':scope,'errors':errors,'global_settling_and_legacy_round_observations':settles,'scope':'Every accepted managed-family publication, including unexpected/background IDs: actual API clock to ALL owner receipts within500ms. Typed FLIGHT/VALVE/ACTUATOR commands explicitly retain original independent gates; no500ms per-packet claim for them. Unknown types or unbound managed IDs fail; no UART origin substitution.'}
if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('log',type=Path);p.add_argument('--output',required=True,type=Path);p.add_argument('--expect-rounds',type=int,default=3);a=p.parse_args();result=audit(a.log.read_text().splitlines(),500,a.expect_rounds);a.output.write_text(json.dumps(result,indent=2)+'\n');print(json.dumps({'passed':result['passed'],'obligations':len(result['obligations']),'errors':result['errors']}));raise SystemExit(0 if result['passed'] else 1)
