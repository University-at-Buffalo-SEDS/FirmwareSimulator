#!/usr/bin/env python3
"""Ordinary GSE policy against real linked firmware and explicit assumed sensor inputs.

No qualification/ACK injection. All paths and credentials are fresh test fixtures.
The first gate covers self-test, nitrogen receipts and a deliberately unmet mass
fill/arm gate. It does not claim physical pneumatic or complete Fire validation.
"""
from pathlib import Path
import argparse,base64,hashlib,hmac,importlib.util,json,os,re,shutil,subprocess,time,urllib.request,urllib.error

def ordinary_recipe(text, flight=False):
    """Fail closed if a simulator builder silently selects policy-bypassing modes."""
    commands=[part for part in text.replace('\\\n',' ').split('&&') if 'cargo build' in part and 'groundstation_backend' in part]
    if len(commands)!=1:raise ValueError('Expected one explicit backend release command')
    features=re.search(r'--features\s+([\w,]+)',commands[0])
    if not features or set(features[1].split(','))!=({'firmware_sim'} if flight else {'test_fire_mode','firmware_sim'}):
        raise ValueError('Operational acceptance requires test_fire_mode,firmware_sim only')

def raw_pressure(pressure):return max(0,min(4095,round(pressure*3103/2000)))
def plant_pressure(previous,dt_ms,valves):
    """Assumption: relief dominates supply;35psi/s rise,200psi/s relief,no leak."""
    dt=max(0,min(dt_ms,200))/1000
    if len(valves)!=5 or any(v is None for v in valves):return previous
    if valves[1] or valves[2]:return max(0,previous-200*dt)
    if valves[3]:return min(130,previous+35*dt)
    return previous

def raw_loadcell_kg(mass):
    # Actual DAQ historical raw units: code*2.2104/2^24*2/16.
    # Synthetic positive calibration is100kg per raw unit, with zero offset.
    return max(0,min(8388607,round(mass/100*16777216/(2.2104*2/16))))
def mass_samples(mass):
    values=[((i+1)*0x100000)&0xffffff for i in range(16)]
    for i in range(8,12):values[i]=(values[2*(i-8)]-values[2*(i-8)+1])&0xffffff
    values[12]=305652;values[0]=0;values[1]=raw_loadcell_kg(mass)
    return ','.join(str(v)for v in values)

def atomic(path,text):
    temporary=path.with_suffix('.next');temporary.write_text(text);temporary.replace(path)
def request(origin,path,body=None,token=None):
    headers={'Content-Type':'application/json'}
    if token:headers['Authorization']='Bearer '+token
    req=urllib.request.Request(origin+path,None if body is None else json.dumps(body).encode(),headers)
    try:
        with urllib.request.urlopen(req,timeout=2)as response:
            data=response.read(1024*1024);return response.status,json.loads(data)if data and data[:1]in(b'{',b'[')else None
    except urllib.error.HTTPError as error:return error.code,None

def fixture_user():
    # This known value belongs only to a disposable synthetic acceptance account.
    salt=b'isolated-gse-model';key=hashlib.pbkdf2_hmac('sha256',b'fixture-only-password',salt,120000)
    return key,{'version':1,'anonymous':{'view_data':False,'send_commands':False},'users':[{'username':'sequence-fixture','roles':['admin'],'password':{'algorithm':'pbkdf2_sha256','iterations':120000,'salt_b64':base64.b64encode(salt).decode(),'hash_b64':base64.b64encode(key).decode()},'permissions':{'view_data':True,'send_commands':True},'disabled':False}]}
def login(origin,key):
    code,c=request(origin,'/api/auth/challenge',{'username':'sequence-fixture'});assert code==200
    nonce=base64.urlsafe_b64encode(os.urandom(32)).decode().rstrip('=')
    message={'username':'sequence-fixture','challenge_id':c['challenge_id'],'server_nonce_b64':c['server_nonce_b64'],'client_nonce_b64':nonce,'remember_me':False}
    proof=base64.urlsafe_b64encode(hmac.new(key,json.dumps(message,separators=(',',':')).encode(),hashlib.sha256).digest()).decode().rstrip('=')
    code,result=request(origin,'/api/auth/login',{**message,'proof_b64':proof});assert code==200
    return result['token']

def append_probe_symbols(layout,board,definitions):
    elf=board/layout['artifacts']['elf'];cache=(elf.parent/'CMakeCache.txt').read_text()
    nm=re.search(r'^CMAKE_NM:FILEPATH=(.*)$',cache,re.M)[1]
    text=subprocess.check_output([nm,'-n',str(elf)],text=True)
    symbols={parts[-1]for line in text.splitlines()if len(parts:=line.split())>=3}
    for name,symbol,offset in definitions:
        matches=[s for s in symbols if s==symbol or s.startswith(symbol+'.')]
        if symbol in symbols:resolved=symbol
        else:
            if len(matches)!=1:raise ValueError('Missing/ambiguous real firmware probe '+symbol)
            resolved=matches[0]
        layout['execution']['memory_probes'].append({'name':name,'symbol':resolved,'byte_offset':offset})

def capture_recent_gps(origin,token,path):
    req=urllib.request.Request(origin+'/api/recent',headers={'Accept':'application/x-ndjson','Authorization':'Bearer '+token})
    rows=[];total=0;scanned=0;complete=False
    with urllib.request.urlopen(req,timeout=5)as response:
        for _ in range(1000000):
            line=response.readline(16385)
            if not line:complete=True;break
            total+=len(line);scanned+=1
            if len(line)>16384 or total>64*1024*1024:raise ValueError('Recent diagnostic input exceeded bound')
            row=json.loads(line)
            if row.get('sender_id')in('RF','FC')and 'GPS'in row.get('data_type',''):
                rows.append(row)
                if len(rows)>8192:raise ValueError('GPS diagnostic output exceeded bound')
    if not complete:raise ValueError('Recent diagnostic did not reach end of snapshot')
    path.write_text(json.dumps(rows,indent=2)+'\n')
    path.with_suffix('.capture.json').write_text(json.dumps({'complete':complete,'scanned_rows':scanned,'scanned_bytes':total,'retained_gps_rows':len(rows)},indent=2)+'\n')

def flight_probe_rows(diagnostics):
    """Read bounded, actual FC probe words without manufacturing readiness."""
    files=list(diagnostics.glob('bay-live-*/memory-probes.txt'))
    if not files:return {}
    text=files[0].read_text();assert len(text)<1024*1024,'FC probe evidence exceeded bound'
    rows={}
    for key,sample,value in re.findall(r'SEDS_BAY_PROBE flight ([\w_]+) (\d+)\n(0x[0-9a-fA-F]+)',text):rows.setdefault(int(sample),{})[key]=int(value,16)
    return rows

def flight_launch_evidence(diagnostics):
    """Transport acceptance alone is insufficient: inspect actual FC firmware state."""
    rows=flight_probe_rows(diagnostics)
    for sample,row in sorted(rows.items(),reverse=True):
        if (all(row.get(key,0)>0 for key in ['fc_commands_received','fc_commands_accepted','fc_commands_processed'])
            and row.get('fc_last_command_id')==1 and row.get('fc_config_flags',0)&0x200
            and row.get('fc_state')in range(3,10) and row.get('fc_watchdog_resume_rejected')==0):return {'sample':sample,**row}
    return None

def prepare(args):
    out=args.evidence.resolve();assert not out.exists(),'Use a new additive evidence directory';out.mkdir(parents=True)
    config=out/'config';config.mkdir();diagnostics=out/'diagnostics';diagnostics.mkdir(mode=0o770);(diagnostics/'temp').mkdir(mode=0o770)
    diagnostics.chmod(0o770);(diagnostics/'temp').chmod(0o770)
    # Shared supplemental group permits this host and unprivileged simulator
    # to exchange only files inside the newly created fixture directory.
    (diagnostics/'state').mkdir(mode=0o770);(diagnostics/'state').chmod(0o770)
    atomic(diagnostics/'pressure.raw','0')
    if args.positive_fill:atomic(diagnostics/'loadcell.raw',mass_samples(0))
    if args.flight:atomic(diagnostics/'gps.fix','0')
    shutil.copyfile(Path(__file__),out/'controller.py')
    key,users=fixture_user();(config/'users.json').write_text(json.dumps(users))
    (config/'gse.json').write_text(json.dumps({'nitrogen_target_psi':50,'pressure_step_psi':50,'pressure_ceiling_psi':150,'maximum_zero_offset_psi':20,'dry_self_test_confirmed':False,'grouped_panel':True}))
    (config/'fill-targets.json').write_text(json.dumps({'version':1,'fill_source':'kg50','nitrogen':{'target_mass_kg':10,'target_pressure_psi':50},'nitrous':{'target_mass_kg':5 if args.positive_fill else 10,'target_pressure_psi':100}}))
    items=[{'id':name,'text':name,'checked':False,'complete_on':kind,'completed_by':None,'completed_at_ms':None}for name,kind in [('self-test','valve_self_test'),('nitrogen','nitrogen_test')]]
    (config/'checklists.json').write_text(json.dumps({'revision':0,'enforce_before_launch':True,'checklists':[{'id':'checkout','title':'Checkout','required_before_launch':True,'items':items}]}))
    # Explicit affine calibration for a synthetic ADC fixture; no completion or
    # qualification state is preset. The mass target intentionally stays unmet.
    calibration={'version':1,'ch1':{'m':100,'b':0},'iadc':{'m':1,'b':0},'kg50_input':'amp2','extra_channels':{'kg50':{'linear':{'m':100 if args.positive_fill else 0.000001,'b':0},'zero_raw':None,'points':[],'fit':None}}}
    (config/'calibration.json').write_text(json.dumps(calibration))
    topology=json.loads(args.template.read_text());names={'gateway','actuator','valve','daq'} | ({'flight','rf','power'} if args.flight else set())
    topology.update(name='ordinary-test-fire-real-firmware-sequence-acceptance',virtual_time_ms=args.duration_ms,sample_count=args.duration_ms//1000,reboots=[],can_ack_events=[],can_link_events=[],radio_link_events=[],assertions=[],host_log_assertions=[])
    topology['nodes']=[n for n in topology['nodes']if n['name']in names]
    if not args.flight:topology['links']=[link for link in topology['links']if link['name']in('fill_pico','fill_can')]
    host=topology['host_nodes'][0]
    if not args.flight:host['serial_links']=[s for s in host['serial_links']if s['link']=='fill_pico']
    host['env']={'RUST_LOG':'info','GS_DEBUG_PRINTS':'0','GS_SIMULATED_SERIAL_PTY':'1','GS_BIND_ADDRESS':'127.0.0.1:0','GS_HTTPS_BIND_ADDRESS':'off','GS_VIDEO_MANAGED':'0','GS_VIDEO_DISCOVERY':'0','GS_SIM_MODE':'0','GS_COMMS_LINK_CONFIG':'/simulation/comms.json','GS_LAYOUT_PATH':'/opt/groundstation/backend/layout/layout_test_fire.json','GS_FLIGHT_STATE_DEFAULT':'1','GS_HEARTBEAT_PERIOD_MS':'500','GS_SIM_NETWORK_EPOCH_MS':'1767225600000','GS_NETWORK_VARIABLE_CACHE':'/diagnostics/state/network-variables.json','GS_USERS_FILE':'/simulation/users.json','GS_GSE_CONFIG':'/simulation/gse.json','GS_GSE_QUALIFICATION':'/diagnostics/state/qualification.json','GS_FILL_TARGETS_CONFIG':'/simulation/fill-targets.json','GS_CHECKLISTS_FILE':'/diagnostics/state/checklists.json','GS_LOADCELL_CALIBRATION_PATH':'/diagnostics/state/calibration.json','GS_CALIBRATION_CONFIG_PATH':'/diagnostics/state/calibration-config.json'}
    if args.flight:host['env']['GS_LAYOUT_PATH']='/opt/groundstation/backend/layout/layout.json'
    shutil.copyfile(config/'checklists.json',diagnostics/'state/checklists.json');shutil.copyfile(config/'calibration.json',diagnostics/'state/calibration.json')
    for p in (diagnostics/'state').iterdir():p.chmod(0o660)
    topology['host_nodes']=[host]
    loader=args.cohort/'ActuatorBoard26/sim/run_full.py';spec=importlib.util.spec_from_file_location('layout_fixture',loader);suite=importlib.util.module_from_spec(spec);spec.loader.exec_module(suite)
    repos={'gateway':'gateway-board26','actuator':'ActuatorBoard26','valve':'ValveBoard26','daq':'DAQ-Board'}
    if args.flight:repos.update(flight='FlightComputer26',rf='RFBoard26',power='PowerBoard26')
    for node in topology['nodes']:
        board=args.cohort/repos[node['name']];layout=suite.load_layout_for_build(board,'Release_Script'if(board/'build/Release_Script/CMakeCache.txt').exists()else'Release')
        layout['execution']['memory_probe_warmup_samples']=3
        required_probes={endpoint.get(key)for link in topology['links']for endpoint in link['endpoints']if endpoint.get('node')==node['name']for key in('tx_probe','rx_probe')}
        layout['execution']['memory_probes']=[probe for probe in layout['execution'].get('memory_probes',[])if probe['name']in required_probes or probe['name']in('panics','tlsf_failures','can_rx_frames','valve_commands_processed','actuator_commands_processed','uart_tx_frames','uart_rx_frames','fdcan_tx_ok','fdcan_rx')]
        if node['name']=='valve':
            append_probe_symbols(layout,board,[(name,symbol,offset)for name,symbol,offset in [('launch_start_count','g_launch_sequence_start_count',0),('launch_service_count','g_launch_sequence_service_count',0),('launch_elapsed_ticks','g_launch_sequence_last_elapsed_ticks',0),('launch_pilot_attempts','g_launch_sequence_pilot_attempt_count',0),('launch_finish_count','g_launch_sequence_finish_count',0),('launch_abort_seen','g_launch_sequence_abort_seen_count',0),('pilot_actual_state','g_sim_pilot_valve_state',0),('pilot_driver_result','g_sim_pilot_solenoid_result',0),('valve_abort','g_sim_valve_aborted',0),('valve_local_ticks','_tx_timer_system_clock',0),('launch_command_utc_lo','g_thread_comm_launch_command_timestamp_ms',0),('launch_command_utc_hi','g_thread_comm_launch_command_timestamp_ms',4)]])
            sensor=next(p for p in layout['peripherals']if p.get('bus')=='adc3');sensor.update(channel_samples=[0],noise_lsb=0,stimulus_path='/diagnostics/pressure.raw')
        if node['name']=='rf' and args.flight:
            next(p for p in layout['peripherals']if p.get('model')=='neo_m9n')['stimulus_path']='/diagnostics/gps.fix'
            append_probe_symbols(layout,board,[(name,symbol,offset)for name,symbol,offset in [('rf_gps_has_fix_word','g_neom9n_has_fix',0),('rf_gps_publish_attempts','g_sim_gps_publish_attempts',0),('rf_gps_publish_ok','g_sim_gps_publish_ok',0),('rf_gps_time_valid','g_gps_time_offset_valid',0),('rf_gps_time_offset_lo','g_gps_time_offset_ms',0),('rf_gps_time_offset_hi','g_gps_time_offset_ms',4),('rf_gps_stack_remaining','g_neom9n_stack_remaining',0),('rf_gps_stack_used','g_neom9n_stack_used',0)]])
        if node['name']=='flight' and args.flight:
            append_probe_symbols(layout,board,[(name,symbol,0)for name,symbol in [('fc_commands_received','g_network_flight_commands_received'),('fc_commands_accepted','g_network_flight_commands_accepted'),('fc_commands_processed','g_network_flight_commands_processed'),('fc_last_command_id','g_last_network_flight_command_id'),('fc_last_command_msg','g_last_network_flight_command_msg'),('fc_recovery_dequeued','g_recovery_commands_dequeued'),('fc_config_flags','g_conf'),('fc_state','sm'),('fc_watchdog_resume_rejected','g_fc_watchdog_resume_rejected'),('fc_watchdog_resumed','g_fc_watchdog_resumed')]])
        if node['name']=='actuator':
            append_probe_symbols(layout,board,[('ab_last_executed_command','g_sim_last_valve_command',0),('ab_sequence_active_word','g_launch_sequence_active',0),('ab_igniter_started_word','g_launch_sequence_igniter_started',0),('ab_igniter_output_word','g_igniter_on',0),('ab_igniter_start_ticks','g_launch_sequence_igniter_start_ticks',0),('ab_igniter_stop_ticks','g_launch_sequence_igniter_stop_ticks',0),('ab_abort_word','g_aborted',0),('ab_local_ticks','_tx_timer_system_clock',0)])
            # Actual GPIO registers: PB7 is the firmware enable output; PC3 is
            # the active-low driver fault input already modeled high by profile.
            layout['execution']['memory_probes'] += [{'name':'ab_igniter_enable_odr','address':0x48000414},{'name':'ab_driver_fault_idr','address':0x48000810}]
        if node['name']=='daq':
            append_probe_symbols(layout,board,[('daq_launch_deadline_lo','g_sd_close_unix_ms',0),('daq_launch_deadline_hi','g_sd_close_unix_ms',4),('daq_launch_session_lo','g_sd_session',0),('daq_launch_session_hi','g_sd_session',4),('daq_local_deadline_lo','g_sd_close_local_ms',0),('daq_local_deadline_hi','g_sd_close_local_ms',4)])
        if node['name']=='daq' and args.positive_fill:
            next(p for p in layout['peripherals']if p.get('model')=='mcp3564r')['stimulus_path']='/diagnostics/loadcell.raw'
        (config/(node['name']+'.json')).write_text(json.dumps(layout))
    # Reuse the versioned Ethernet discovery configuration from the linked suite.
    suite.write_network_comms_config(config);generated=config/'comms-v2.json';assert generated.exists();generated.rename(config/'comms.json')
    (config/'topology.json').write_text(json.dumps(topology,indent=2))
    command=['docker','run','--rm','--name','seds-operational-gse-'+str(os.getpid()),'--network','host','--group-add',str(os.getgid()),'-e','TMPDIR=/diagnostics/temp','-e','FIRMWARE_SIM_DIAGNOSTICS_DIR=/diagnostics','-v',str(config)+':/simulation:ro','-v',str(diagnostics)+':/diagnostics']
    for name,repo in repos.items():command+=['-v',str((args.cohort/repo).resolve())+':/nodes/'+name+':ro']
    if args.radio_trace:command[2:2]=['-e','FIRMWARE_SIM_RADIO_TRACE_DIR=/diagnostics/radio-trace','-e','FIRMWARE_SIM_RADIO_TRACE_UNTIL_MS='+str(args.duration_ms-1000)]
    command+=[args.image,'bay','--topology','/simulation/topology.json']
    return out,diagnostics,key,command

def owned_container_name(command):
    name=command[command.index('--name')+1]
    if not name.startswith('seds-operational-gse-')or not name.removeprefix('seds-operational-gse-').isdigit():raise ValueError('Unexpected owned simulator container name')
    return name

def run(args):
    out,diagnostics,key,command=prepare(args)
    (out/'command.json').write_text(json.dumps(command,indent=2))
    if args.prepare_only:return
    summary={'phase':'running','scope':'Assumed sensor response;actual firmwareADC/network/ACK/operationalpolicy;no direct qualification writes','image':args.image,'milestones':[],'plant_assumptions':{'nitrogen_psi_per_second':35,'relief_psi_per_second':200,'closed_leak_psi_per_second':0,'mass_target_intentionally_unmet':not args.positive_fill,'nitrous_fill_kg_per_second':2 if args.positive_fill else None}}
    progress=lambda:(out/'result.json').write_text(json.dumps(summary,indent=2)+'\n')
    log=(out/'simulator.log').open('w');process=subprocess.Popen(command,stdout=log,stderr=subprocess.STDOUT);start=time.monotonic();origin=None;token=None;stage='ready';pressure=0.;previous_clock=0;phase=None;mass=0.;fire_clock=False;previous_valves=None;fire_saved=False;qd_ack_at=None;qd_started_at=None;fc_rejected_baseline=None;qd_queue_seen=False
    try:
        progress()
        while process.poll()is None:
            if time.monotonic()-start>args.timeout:raise TimeoutError('Operational simulator deadline; not a pass')
            clocks=list((diagnostics/'temp').glob('*/virtual-clock.txt'));logs=list((diagnostics/'temp').glob('*/groundstation-*log'))
            if not clocks or not logs:time.sleep(.1);continue
            if origin is None:
                text='\n'.join(p.read_text(errors='replace')[:65536] for p in logs)
                match=re.search(r'HTTP listening on (127\.0\.0\.1:\d+)',text)
                if not match:time.sleep(.1);continue
                assert ('features testing=false hitl_mode=false test_fire_mode='+('false' if args.flight else 'true'))in text,'Wrong operational build features'
                origin='http://'+match[1];token=login(origin,key)
            try:now=int(clocks[0].read_text().strip())
            except ValueError:time.sleep(.02);continue
            if args.flight and fc_rejected_baseline is None:
                probes=flight_probe_rows(diagnostics)
                if probes:
                    fc_rejected_baseline=probes[min(probes)].get('fc_watchdog_resume_rejected')
                    summary['fc_initial_watchdog_resume_rejected']=fc_rejected_baseline;progress()
            if phase is not None and now == previous_clock:time.sleep(.05);continue
            try:code,status=request(origin,'/api/gse/status',token=token)
            except OSError:
                if process.poll()is not None:break
                time.sleep(.1);continue
            assert code==200
            try:status_clock_upper=max(now,int(clocks[0].read_text().strip()))
            except ValueError:status_clock_upper=None
            assert status['request_gate']['hitl_mode']is False and 'qualification'in status,'Ordinary policy missing'
            delta=max(0,min(now-previous_clock,200));pressure=plant_pressure(pressure,delta,status['valves']);previous_clock=now;atomic(diagnostics/'pressure.raw',str(raw_pressure(pressure)))
            if args.positive_fill:
                if status['phase']=='filling'and status['valves'][4]is True:mass=min(5.2,mass+2*delta/1000)
                atomic(diagnostics/'loadcell.raw',mass_samples(mass))
            if status['valves']!=previous_valves:
                previous_valves=list(status['valves']);events=summary.setdefault('actual_valve_events',[])
                assert len(events)<512,'Valve event evidence bound exceeded'
                events.append({'virtual_ms':now,'valves':previous_valves});progress()
            def saved_evidence(label):
                data=json.loads((diagnostics/'state/qualification.json').read_text())
                assert data['qualification']==status['qualification']
                for name in ['qualification','checklists','calibration']:
                    source=diagnostics/'state'/(name+'.json')
                    shutil.copyfile(source,out/(label+'-'+name+'.json'))
                cache=diagnostics/'state/network-variables.json'
                if cache.exists():shutil.copyfile(cache,out/(label+'-network-variables.json'))
                if args.flight:capture_recent_gps(origin,token,out/(label+'-recent-gps.json'))
            if status['phase']!=phase:
                phase=status['phase'];summary['milestones'].append({'virtual_ms':now,'phase':phase,'mission_stage':status['mission_stage'],'pressure_psi':status['pressure_psi']});progress()
            if args.gps_loss_during_qd and stage=='complete':assert not status['qualification']['fire_started'],'Cancelled launch fired later'
            if status['qualification']['fault']or phase=='fault':raise AssertionError('Actual procedure fault: '+status['message'])
            if stage=='ready'and status['topology_stable']and status['valves']==[False,True,True,False,False]and phase in('idle','cancelled'):
                assert request(origin,'/api/command','StartFill',token)[0]==403,'Fill admitted before tests'
                assert request(origin,'/api/command','NitrogenTest',token)[0]==403,'Nitrogen admitted before self-test'
                assert request(origin,'/api/gse/self-test-confirmation',{'confirmed':True},token)[0]==200
                assert request(origin,'/api/command','ValveSelfTest',token)[0]==200;stage='self-test'
            elif stage=='self-test'and status['qualification']['self_test']:
                document=request(origin,'/api/checklists',token=token)[1];assert document['checklists'][0]['items'][0]['checked'];assert not document['checklists'][0]['items'][1]['checked']
                assert json.loads((diagnostics/'state/qualification.json').read_text())['qualification']['self_test']
                saved_evidence('self-test-passed')
                assert request(origin,'/api/command','NitrogenTest',token)[0]==200;stage='nitrogen'
            elif stage=='nitrogen'and status['qualification']['nitrogen_test']:
                document=request(origin,'/api/checklists',token=token)[1];assert all(item['checked']for item in document['checklists'][0]['items'])
                saved_evidence('nitrogen-passed')
                assert request(origin,'/api/command','StartFill',token)[0]==200;stage='fill'
            elif stage=='fill'and phase=='filling':
                assert status['mission_stage']=='fill'and not status['qualification']['fill_target']
                assert request(origin,'/api/command','Launch',token)[0]==403,'Fire admitted with unmet mass target'
                if args.positive_fill:stage='fill-target'
                else:assert request(origin,'/api/command','PauseFill',token)[0]==200;stage='pause'
            elif stage=='fill-target'and status['qualification']['fill_target']:
                assert status['mission_stage']=='armed'and status['valves']==[False]*5
                assert json.loads((diagnostics/'state/qualification.json').read_text())['qualification']['fill_target']
                summary['milestones'].append({'virtual_ms':now,'phase':'real_mass_target_armed','mass_input_kg':mass})
                saved_evidence('mass-target-armed')
                if args.fire:
                    if args.flight:
                        assert request(origin,'/api/command','Launch',token)[0]==403,'Launch admitted before QD dispatch'
                        assert request(origin,'/api/command','RetractPlumbing',token)[0]==403,'QD admitted without actual RF GPS lock'
                        atomic(diagnostics/'gps.fix','1');stage='flight-gps'
                    else:assert request(origin,'/api/command','Launch',token)[0]==200;stage='fire'
                else:
                    summary.update(phase='passed',virtual_ms=now,checks='realselftest/nitrogen;automaticdurablechecklists;actualDAQmass;Idle→Fill→Armed');progress();stage='complete'
            elif stage=='flight-gps':
                code,antenna=request(origin,'/api/antenna/status',token=token)
                fix=antenna.get('rocket')if code==200 else None
                if fix and fix['source']=='RF' and not fix['stale'] and not fix.get('manual',False) and fix['altitude_m']is not None:
                    saved_evidence('actual-rf-gps-locked')
                    (out/'actual-rf-gps-lock.json').write_text(json.dumps({'virtual_ms':now,'fix':fix},indent=2)+'\n')
                    assert request(origin,'/api/command','RetractPlumbing',token)[0]==200
                    qd_started_at=now;stage='qd'
            elif stage=='qd':
                assert not status['qualification']['fire_started'],'Fire before real QD confirmation'
                if status['qd'].startswith('Settling'):
                    qd_ack_at=now-(5000-int(re.search(r'remaining_ms: (\d+)',status['qd'])[1]))
                    assert request(origin,'/api/command','Launch',token)[0]==200
                    summary['qd']={'dispatched_model_ms':qd_started_at,'first_observed_ack_model_ms':now,'ack_model_interval_ms':[qd_ack_at,None if status_clock_upper is None else qd_ack_at+status_clock_upper-now],'settlement_contract_ms':5000,'status':status['qd']};progress();stage='qd-settling'

            elif stage=='qd-gps-loss':
                assert not status['qualification']['fire_started'],'Fire committed after live GPS was deliberately lost'
                if not status['launch_queued']and status['qd']=='NotStarted':
                    code,antenna=request(origin,'/api/antenna/status',token=token)
                    fix=antenna.get('rocket')if code==200 else None
                    assert fix is None or fix['source']!='RF' or fix['stale'],'Queue cancelled without observed RF lock loss'
                    saved_evidence('gps-loss-cancelled')
                    summary.update(phase='passed',virtual_ms=now,checks='actualrawNMEAfixloss→RFno-fix→queuedQDLaunchcancelled;noFire');progress();stage='complete'
            elif stage=='qd-settling':
                if status['qualification']['fire_started']:
                    assert status['qd']=='Ready','Fire without the authoritative five-second QD settlement gate'
                    summary['qd']['fire_observed_model_ms']=now;progress();stage='fire'
                elif status['launch_queued']:
                    if not qd_queue_seen:summary['qd']['queue_observed_model_ms']=now;progress()
                    qd_queue_seen=True
                    if args.gps_loss_during_qd:
                        atomic(diagnostics/'gps.fix','0');summary['qd']['gps_loss_stimulus_model_ms']=now;progress();stage='qd-gps-loss'
                elif qd_queue_seen:
                    assert status['qd']=='Ready','Launch queue cancelled before QD settled'
            elif stage=='fire'and status['qualification']['fire_started']:
                if not fire_saved:saved_evidence('fire-committed');fire_saved=True
                clock=request(origin,'/api/launch_clock',token=token)[1]
                assert status['mission_stage']=='fire'and status['qualification']['fire_started']
                if clock['kind']=='t_minus':
                    assert clock['anchor_timestamp_ms']is None and 0<=clock['duration_ms']<=10000
                    fire_clock=True
                elif clock['kind']=='t_plus':
                    assert fire_clock,'Countdown was never observed'
                    assert clock['anchor_timestamp_ms']is None and clock['duration_ms']>=0
                    if args.flight:
                        proof=flight_launch_evidence(diagnostics)
                        if proof is None:time.sleep(.05);continue
                        (out/'actual-fc-launch.json').write_text(json.dumps(proof,indent=2)+'\n')
                    saved_evidence('pilot-tplus')
                    document=request(origin,'/api/checklists',token=token)[1]
                    assert not document['checklists'][0]['items'][1]['checked'],'Cold flow did not clear after real Fire'
                    summary.update(phase='passed',virtual_ms=now,checks='actualADC/DAQmassandACKs;Idle→Fill→Armed→Fire;countdowntopilotTplus;automaticchecklistsreset');progress();print('GSE_REAL_FIRMWARE_POSITIVE_FIRE_PASS',flush=True);stage='complete'
            elif stage=='pause'and phase=='paused':
                assert status['valves']==[False]*5 and not status['qualification']['fill_target']
                assert request(origin,'/api/command','Launch',token)[0]==403
                summary.update(phase='passed',virtual_ms=now,checks='realselftest/nitrogen;durableautomaticchecklists;Idle→Fill;unmetmassblocksArmed/Fire;Pauseallclosed');progress();print('GSE_REAL_FIRMWARE_SELFTEST_NITROGEN_FILL_GATE_PASS',flush=True);stage='complete'
            time.sleep(.05)
        assert process.returncode==0,'Simulator/firmware gate failed'
        assert stage=='complete','Sequence gate incomplete: '+stage
    except BaseException as error:
        summary.update(phase='failed',error=str(error));progress();raise
    finally:
        if process.poll()is None:subprocess.run(['docker','stop','--time','5',owned_container_name(command)],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL);process.wait(timeout=30)
        log.close()

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--cohort',type=Path,required=True);p.add_argument('--template',type=Path,required=True);p.add_argument('--image',required=True);p.add_argument('--evidence',type=Path,required=True);p.add_argument('--duration-ms',type=int,default=90000);p.add_argument('--timeout',type=int,default=2400);p.add_argument('--prepare-only',action='store_true');p.add_argument('--positive-fill',action='store_true');p.add_argument('--fire',action='store_true');p.add_argument('--flight',action='store_true');p.add_argument('--gps-loss-during-qd',action='store_true');p.add_argument('--radio-trace',action='store_true');a=p.parse_args();assert not a.fire or a.positive_fill;assert not a.gps_loss_during_qd or(a.flight and a.fire);assert a.duration_ms>=60000 and a.duration_ms%1000==0;run(a)
