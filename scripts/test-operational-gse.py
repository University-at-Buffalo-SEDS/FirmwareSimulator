#!/usr/bin/env python3
"""Ordinary GSE policy against real linked firmware and explicit assumed sensor inputs.

No qualification/ACK injection. All paths and credentials are fresh test fixtures.
The first gate covers self-test, nitrogen receipts and a deliberately unmet mass
fill/arm gate. It does not claim physical pneumatic or complete Fire validation.
"""
from pathlib import Path
import argparse,base64,hashlib,hmac,importlib.util,json,os,re,shutil,subprocess,time,urllib.request,urllib.error

def ordinary_recipe(text):
    """Fail closed if a simulator builder silently selects policy-bypassing modes."""
    commands=[part for part in text.replace('\\\n',' ').split('&&') if 'cargo build' in part and 'groundstation_backend' in part]
    if len(commands)!=1:raise ValueError('Expected one explicit backend release command')
    features=re.search(r'--features\s+([\w,]+)',commands[0])
    if not features or set(features[1].split(','))!={'test_fire_mode','firmware_sim'}:
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
    values[12]=305652;values[1]=raw_loadcell_kg(mass)
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

def prepare(args):
    out=args.evidence.resolve();assert not out.exists(),'Use a new additive evidence directory';out.mkdir(parents=True)
    config=out/'config';config.mkdir();diagnostics=out/'diagnostics';diagnostics.mkdir(mode=0o770);(diagnostics/'temp').mkdir(mode=0o770)
    diagnostics.chmod(0o770);(diagnostics/'temp').chmod(0o770)
    # Shared supplemental group permits this host and unprivileged simulator
    # to exchange only files inside the newly created fixture directory.
    (diagnostics/'state').mkdir(mode=0o770);(diagnostics/'state').chmod(0o770)
    atomic(diagnostics/'pressure.raw','0')
    if args.positive_fill:atomic(diagnostics/'loadcell.raw',mass_samples(0))
    shutil.copyfile(Path(__file__),out/'controller.py')
    key,users=fixture_user();(config/'users.json').write_text(json.dumps(users))
    (config/'gse.json').write_text(json.dumps({'nitrogen_target_psi':50,'pressure_step_psi':50,'pressure_ceiling_psi':150,'maximum_zero_offset_psi':20,'dry_self_test_confirmed':False,'grouped_panel':True}))
    (config/'fill-targets.json').write_text(json.dumps({'version':1,'fill_source':'kg50','nitrogen':{'target_mass_kg':10,'target_pressure_psi':50},'nitrous':{'target_mass_kg':5 if args.positive_fill else 10,'target_pressure_psi':100}}))
    items=[{'id':name,'text':name,'checked':False,'complete_on':kind,'completed_by':None,'completed_at_ms':None}for name,kind in [('self-test','valve_self_test'),('nitrogen','nitrogen_test')]]
    (config/'checklists.json').write_text(json.dumps({'revision':0,'enforce_before_launch':True,'checklists':[{'id':'checkout','title':'Checkout','required_before_launch':True,'items':items}]}))
    # Explicit affine calibration for a synthetic ADC fixture; no completion or
    # qualification state is preset. The mass target intentionally stays unmet.
    calibration={'version':1,'iadc':{'m':1,'b':0},'kg50_input':'amp2','extra_channels':{'kg50':{'linear':{'m':100 if args.positive_fill else 0.000001,'b':0},'zero_raw':None,'points':[],'fit':None}}}
    (config/'calibration.json').write_text(json.dumps(calibration))
    topology=json.loads(args.template.read_text());names={'gateway','actuator','valve','daq'}
    topology.update(name='ordinary-test-fire-real-firmware-sequence-acceptance',virtual_time_ms=args.duration_ms,sample_count=args.duration_ms//1000,reboots=[],can_ack_events=[],can_link_events=[],radio_link_events=[],assertions=[],host_log_assertions=[])
    topology['nodes']=[n for n in topology['nodes']if n['name']in names]
    topology['links']=[link for link in topology['links']if link['name']in('fill_pico','fill_can')]
    host=topology['host_nodes'][0];host['serial_links']=[s for s in host['serial_links']if s['link']=='fill_pico']
    host['env']={'RUST_LOG':'info','GS_DEBUG_PRINTS':'0','GS_SIMULATED_SERIAL_PTY':'1','GS_BIND_ADDRESS':'127.0.0.1:0','GS_HTTPS_BIND_ADDRESS':'off','GS_VIDEO_MANAGED':'0','GS_VIDEO_DISCOVERY':'0','GS_SIM_MODE':'0','GS_COMMS_LINK_CONFIG':'/simulation/comms.json','GS_LAYOUT_PATH':'/opt/groundstation/backend/layout/layout_test_fire.json','GS_FLIGHT_STATE_DEFAULT':'1','GS_HEARTBEAT_PERIOD_MS':'500','GS_SIM_NETWORK_EPOCH_MS':'1767225600000','GS_USERS_FILE':'/simulation/users.json','GS_GSE_CONFIG':'/simulation/gse.json','GS_GSE_QUALIFICATION':'/diagnostics/state/qualification.json','GS_FILL_TARGETS_CONFIG':'/simulation/fill-targets.json','GS_CHECKLISTS_FILE':'/diagnostics/state/checklists.json','GS_LOADCELL_CALIBRATION_PATH':'/diagnostics/state/calibration.json','GS_CALIBRATION_CONFIG_PATH':'/diagnostics/state/calibration-config.json'}
    shutil.copyfile(config/'checklists.json',diagnostics/'state/checklists.json');shutil.copyfile(config/'calibration.json',diagnostics/'state/calibration.json')
    for p in (diagnostics/'state').iterdir():p.chmod(0o660)
    topology['host_nodes']=[host]
    loader=args.cohort/'ActuatorBoard26/sim/run_full.py';spec=importlib.util.spec_from_file_location('layout_fixture',loader);suite=importlib.util.module_from_spec(spec);spec.loader.exec_module(suite)
    repos={'gateway':'gateway-board26','actuator':'ActuatorBoard26','valve':'ValveBoard26','daq':'DAQ-Board'}
    for node in topology['nodes']:
        board=args.cohort/repos[node['name']];layout=suite.load_layout_for_build(board,'Release_Script')
        layout['execution']['memory_probe_warmup_samples']=3
        layout['execution']['memory_probes']=[probe for probe in layout['execution'].get('memory_probes',[])if probe['name']in('panics','tlsf_failures','can_rx_frames','valve_commands_processed','actuator_commands_processed','uart_tx_frames','uart_rx_frames','fdcan_tx_ok','fdcan_rx')]
        if node['name']=='valve':
            sensor=next(p for p in layout['peripherals']if p.get('bus')=='adc3');sensor.update(channel_samples=[0],noise_lsb=0,stimulus_path='/diagnostics/pressure.raw')
        if node['name']=='daq' and args.positive_fill:
            next(p for p in layout['peripherals']if p.get('model')=='mcp3564r')['stimulus_path']='/diagnostics/loadcell.raw'
        (config/(node['name']+'.json')).write_text(json.dumps(layout))
    # Reuse the versioned Ethernet discovery configuration from the linked suite.
    suite.write_network_comms_config(config);generated=config/'comms-v2.json';assert generated.exists();generated.rename(config/'comms.json')
    (config/'topology.json').write_text(json.dumps(topology,indent=2))
    command=['docker','run','--rm','--name','seds-operational-gse-'+str(os.getpid()),'--network','host','--group-add',str(os.getgid()),'-e','TMPDIR=/diagnostics/temp','-e','FIRMWARE_SIM_DIAGNOSTICS_DIR=/diagnostics','-v',str(config)+':/simulation:ro','-v',str(diagnostics)+':/diagnostics']
    for name,repo in repos.items():command+=['-v',str((args.cohort/repo).resolve())+':/nodes/'+name+':ro']
    command+=[args.image,'bay','--topology','/simulation/topology.json']
    return out,diagnostics,key,command

def run(args):
    out,diagnostics,key,command=prepare(args)
    (out/'command.json').write_text(json.dumps(command,indent=2))
    if args.prepare_only:return
    summary={'phase':'running','scope':'Assumed sensor response;actual firmwareADC/network/ACK/operationalpolicy;no direct qualification writes','image':args.image,'milestones':[],'plant_assumptions':{'nitrogen_psi_per_second':35,'relief_psi_per_second':200,'closed_leak_psi_per_second':0,'mass_target_intentionally_unmet':not args.positive_fill,'nitrous_fill_kg_per_second':2 if args.positive_fill else None}}
    progress=lambda:(out/'result.json').write_text(json.dumps(summary,indent=2)+'\n')
    log=(out/'simulator.log').open('w');process=subprocess.Popen(command,stdout=log,stderr=subprocess.STDOUT);start=time.monotonic();origin=None;token=None;stage='ready';pressure=0.;previous_clock=0;phase=None;mass=0.;fire_clock=False
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
                assert 'features testing=false hitl_mode=false test_fire_mode=true'in text,'Wrong operational build features'
                origin='http://'+match[1];token=login(origin,key)
            try:now=int(clocks[0].read_text().strip())
            except ValueError:time.sleep(.02);continue
            if phase is not None and now == previous_clock:time.sleep(.05);continue
            try:code,status=request(origin,'/api/gse/status',token=token)
            except OSError:
                if process.poll()is not None:break
                time.sleep(.1);continue
            assert code==200
            assert status['request_gate']['hitl_mode']is False and 'qualification'in status,'Ordinary policy missing'
            delta=max(0,min(now-previous_clock,200));pressure=plant_pressure(pressure,delta,status['valves']);previous_clock=now;atomic(diagnostics/'pressure.raw',str(raw_pressure(pressure)))
            if args.positive_fill:
                if status['phase']=='filling'and status['valves'][4]is True:mass=min(5.2,mass+2*delta/1000)
                atomic(diagnostics/'loadcell.raw',mass_samples(mass))
            if status['phase']!=phase:
                phase=status['phase'];summary['milestones'].append({'virtual_ms':now,'phase':phase,'mission_stage':status['mission_stage'],'pressure_psi':status['pressure_psi']});progress()
            if status['qualification']['fault']or phase=='fault':raise AssertionError('Actual procedure fault: '+status['message'])
            if stage=='ready'and status['topology_stable']and status['valves']==[False,True,True,False,False]and phase in('idle','cancelled'):
                assert request(origin,'/api/command','StartFill',token)[0]==403,'Fill admitted before tests'
                assert request(origin,'/api/command','NitrogenTest',token)[0]==403,'Nitrogen admitted before self-test'
                assert request(origin,'/api/gse/self-test-confirmation',{'confirmed':True},token)[0]==200
                assert request(origin,'/api/command','ValveSelfTest',token)[0]==200;stage='self-test'
            elif stage=='self-test'and status['qualification']['self_test']:
                document=request(origin,'/api/checklists',token=token)[1];assert document['checklists'][0]['items'][0]['checked'];assert not document['checklists'][0]['items'][1]['checked']
                assert json.loads((diagnostics/'state/qualification.json').read_text())['qualification']['self_test']
                assert request(origin,'/api/command','NitrogenTest',token)[0]==200;stage='nitrogen'
            elif stage=='nitrogen'and status['qualification']['nitrogen_test']:
                document=request(origin,'/api/checklists',token=token)[1];assert all(item['checked']for item in document['checklists'][0]['items'])
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
                if args.fire:
                    assert request(origin,'/api/command','Launch',token)[0]==200;stage='fire'
                else:
                    summary.update(phase='passed',virtual_ms=now,checks='realselftest/nitrogen;automaticdurablechecklists;actualDAQmass;Idle→Fill→Armed');progress();stage='complete'
            elif stage=='fire':
                clock=request(origin,'/api/launch_clock',token=token)[1]
                assert status['mission_stage']=='fire'and status['qualification']['fire_started']
                if clock['kind']=='t_minus':
                    assert clock['anchor_timestamp_ms']is None and 0<=clock['duration_ms']<=10000
                    fire_clock=True
                elif clock['kind']=='t_plus':
                    assert fire_clock,'Countdown was never observed'
                    assert clock['anchor_timestamp_ms']is None and clock['duration_ms']>=0
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
        if process.poll()is None:subprocess.run(['docker','stop','--time','5',command[4]],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL);process.wait(timeout=30)
        log.close()

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--cohort',type=Path,required=True);p.add_argument('--template',type=Path,required=True);p.add_argument('--image',required=True);p.add_argument('--evidence',type=Path,required=True);p.add_argument('--duration-ms',type=int,default=90000);p.add_argument('--timeout',type=int,default=2400);p.add_argument('--prepare-only',action='store_true');p.add_argument('--positive-fill',action='store_true');p.add_argument('--fire',action='store_true');a=p.parse_args();assert not a.fire or a.positive_fill;assert a.duration_ms>=60000 and a.duration_ms%1000==0;run(a)
