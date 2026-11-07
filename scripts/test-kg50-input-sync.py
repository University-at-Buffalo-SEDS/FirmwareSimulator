#!/usr/bin/env python3
"""DAQ 50 kg input selection against real linked firmware and the real backend.

Runs the actual DAQ and gateway ELFs and the actual GroundStation backend with
its bundled UI. Nothing writes board state: the input is chosen through the
operator API or the real UI (--ui), and the DAQ's own RAM and ADC stimulus show
which input it is using. CH1 and differential CH2-CH3 carry different codes, so
a report's value proves which physical input produced it.

Timeline, in virtual seconds after each step's settle window:
  boot on AMP2, select P6, reboot DAQ, restart GS, reboot DAQ while its CAN link
  is detached, restore the link, select AMP2, then make the GS file disagree
  with the DAQ and restart GS.
"""
from pathlib import Path
import argparse,importlib.util,json,os,re,shutil,subprocess,sys,time

HERE=Path(__file__).resolve().parent
spec=importlib.util.spec_from_file_location('operational_gse',HERE/'test-operational-gse.py');gse=importlib.util.module_from_spec(spec);spec.loader.exec_module(gse)

# Stimulus codes. ID 1 is the AMP2 input; ID 9 is P6, the signed CH2-CH3 difference.
CODES=[(i+1)*0x100000&0xffffff for i in range(16)]
CODES[0]=0;CODES[1]=0x080000;CODES[2]=0x300000;CODES[3]=0x100000
for i in range(8,12):CODES[i]=(CODES[2*(i-8)]-CODES[2*(i-8)+1])&0xffffff
CODES[12]=305652
# mcp3564r.c: CH1 raw=code*2.2104/2^24*2/16; ID 9 raw=code/2^23*2.4/2... measured by the model below.
AMP2_RAW=0x080000*2.2104/(1<<24)*2/16

STEPS=[ # (sample, action, argument)
    (30,'checkpoint','boot-amp2'),
    (32,'select','p6_differential'),
    (50,'checkpoint','selected-p6'),
    (52,'reboot-daq-marker',None),
    (70,'checkpoint','daq-rebooted-gs-up'),
    (72,'restart-gs-marker',None),
    (95,'checkpoint','gs-restarted-daq-up'),
    (97,'daq-offline-reboot-marker',None),
    (120,'checkpoint','daq-rebooted-gs-unreachable'),
    (150,'checkpoint','link-restored'),
    (152,'select','amp2'),
    (170,'checkpoint','selected-amp2'),
    (172,'write-gs-file','p6_differential'),
    (174,'restart-gs-marker',None),
    (200,'checkpoint','gs-restarted-with-disagreeing-file'),
]
REBOOTS=[{'node':'daq','after_sample':52},{'node':'groundstation','after_sample':72},{'node':'daq','after_sample':99},{'node':'groundstation','after_sample':174}]
LINK_EVENTS=[{'node':'daq','peripheral':'fdcan1','link':'fill_can','after_sample':97,'connected':False},{'node':'daq','peripheral':'fdcan1','link':'fill_can','after_sample':125,'connected':True}]
SAMPLES=205

def symbol_address(layout,board,symbol):
    elf=board/layout['artifacts']['elf'];cache=(elf.parent/'CMakeCache.txt').read_text()
    nm=re.search(r'^CMAKE_NM:FILEPATH=(.*)$',cache,re.M)[1]
    rows=[line.split() for line in subprocess.check_output([nm,str(elf)],text=True).splitlines()]
    found=[int(r[0],16) for r in rows if len(r)==3 and r[2]==symbol]
    if len(found)!=1:raise ValueError('Missing/ambiguous firmware symbol '+symbol)
    return found[0]

def prepare(args):
    out=args.evidence.resolve();assert not out.exists(),'Use a new evidence directory';out.mkdir(parents=True)
    config=out/'config';config.mkdir();diagnostics=out/'diagnostics';state=diagnostics/'state'
    for path in (diagnostics,diagnostics/'temp',state):path.mkdir(mode=0o770);path.chmod(0o770)
    gse.atomic(diagnostics/'pressure.raw','0');gse.atomic(diagnostics/'loadcell.raw',','.join(map(str,CODES)))
    shutil.copyfile(__file__,out/'controller.py')
    key,users=gse.fixture_user();(config/'users.json').write_text(json.dumps(users))
    (config/'gse.json').write_text(json.dumps({'nitrogen_target_psi':50,'pressure_step_psi':50,'pressure_ceiling_psi':150,'maximum_zero_offset_psi':20,'dry_self_test_confirmed':False,'grouped_panel':True,'turbocharge_max_added_mass_kg':0.3,'turbocharge_pressure_deadband_psi':12}))
    (config/'fill-targets.json').write_text(json.dumps({'version':1,'fill_source':'kg50','nitrogen':{'target_mass_kg':10,'target_pressure_psi':50},'nitrous':{'target_mass_kg':10,'target_pressure_psi':100}}))
    (config/'checklists.json').write_text(json.dumps({'revision':0,'enforce_before_launch':True,'checklists':[]}))
    calibration={'version':1,'ch1':{'m':100,'b':0},'iadc':{'m':1,'b':0},'kg50_input':'amp2','extra_channels':{'kg50':{'linear':{'m':100,'b':0},'zero_raw':None,'points':[],'fit':None}}}
    (config/'calibration.json').write_text(json.dumps(calibration))
    topology=json.loads(args.template.read_text());names={'gateway','daq'}
    topology.update(name='daq-kg50-input-sync',virtual_time_ms=SAMPLES*1000,sample_count=SAMPLES,reboots=REBOOTS,can_ack_events=[],can_link_events=LINK_EVENTS,radio_link_events=[],assertions=[],host_log_assertions=[])
    topology['nodes']=[n for n in topology['nodes'] if n['name'] in names]
    topology['links']=[l for l in topology['links'] if l['name'] in('fill_pico','fill_can')]
    for link in topology['links']:link['endpoints']=[e for e in link['endpoints'] if e['node'] in names|{'groundstation'}]
    host=topology['host_nodes'][0];host['cwd']='/opt/groundstation';host['serial_links']=[s for s in host['serial_links'] if s['link']=='fill_pico']
    host['env']={'RUST_LOG':'info','GS_DEBUG_PRINTS':'0','GS_SIMULATED_SERIAL_PTY':'1','GS_BIND_ADDRESS':'0.0.0.0:3000','GS_HTTPS_BIND_ADDRESS':'off','GS_VIDEO_MANAGED':'0','GS_VIDEO_DISCOVERY':'0','GS_STATION_DISCOVERY_BIND':'127.0.0.1:0','GS_VIDEO_DISCOVERY_BIND':'127.0.0.1:0','GS_SIM_MODE':'0','GS_COMMS_LINK_CONFIG':'/simulation/comms.json','GS_LAYOUT_PATH':'/opt/groundstation/backend/layout/layout_test_fire.json','GS_HEARTBEAT_PERIOD_MS':'500','GS_SIM_NETWORK_EPOCH_MS':'1767225600000','GS_NETWORK_VARIABLE_CACHE':'/diagnostics/state/network-variables.json','GS_USERS_FILE':'/simulation/users.json','GS_GSE_CONFIG':'/diagnostics/state/gse.json','GS_GSE_QUALIFICATION':'/diagnostics/state/qualification.json','GS_FILL_TARGETS_CONFIG':'/simulation/fill-targets.json','GS_CHECKLISTS_FILE':'/diagnostics/state/checklists.json','GS_LOADCELL_CALIBRATION_PATH':'/diagnostics/state/calibration.json','GS_CALIBRATION_CONFIG_PATH':'/diagnostics/state/calibration-config.json'}
    for name in ('checklists','calibration','gse'):shutil.copyfile(config/(name+'.json'),state/(name+'.json'))
    for path in state.iterdir():path.chmod(0o660)
    topology['host_nodes']=[host]
    loader=args.cohort/'ActuatorBoard26/sim/run_full.py';spec=importlib.util.spec_from_file_location('layout_fixture',loader);suite=importlib.util.module_from_spec(spec);spec.loader.exec_module(suite)
    repos={'gateway':'gateway-board26','daq':'DAQ-Board'}
    for node in topology['nodes']:
        board=args.cohort/repos[node['name']];layout=suite.load_layout_for_build(board,'Release_Script' if (board/'build/Release_Script/CMakeCache.txt').exists() else 'Release')
        layout['execution']['memory_probe_warmup_samples']=3
        required={e.get(k) for l in topology['links'] for e in l['endpoints'] if e.get('node')==node['name'] for k in('tx_probe','rx_probe')}
        layout['execution']['memory_probes']=[p for p in layout['execution'].get('memory_probes',[]) if p['name'] in required or p['name'] in('panics','tlsf_failures','uart_tx_frames','uart_rx_frames','fdcan_tx_ok','fdcan_rx')]
        if node['name']=='daq':
            # One word at a fixed address: byte 0 is the scanned input, byte 1 the
            # requested input. These are file-local symbols, so read them by address.
            layout['execution']['memory_probes'].append({'name':'kg50_input_word','address':symbol_address(layout,board,'g_kg50_input_active')})
            gse.append_probe_symbols(layout,board,[('kg50_reports','g_daq_kg50_publish_ok_count',0),('discovery_seen','g_telemetry_discovery_seen',0),('uptime_ticks','_tx_timer_system_clock',0)])
            if args.expect_restore_probes:gse.append_probe_symbols(layout,board,[('kg50_input_restores','g_daq_kg50_input_restores',0),('kg50_input_persist_writes','g_daq_kg50_input_persist_writes',0)])
            next(p for p in layout['peripherals'] if p.get('model')=='mcp3564r')['stimulus_path']='/diagnostics/loadcell.raw'
        (config/(node['name']+'.json')).write_text(json.dumps(layout))
    suite.write_network_comms_config(config);(config/'comms-v2.json').rename(config/'comms.json')
    (config/'topology.json').write_text(json.dumps(topology,indent=2))
    name='seds-kg50-sync-'+str(os.getpid())
    command=['docker','run','--rm','--name',name,'--cpus',str(args.cpu_limit),'--memory','16g','--memory-swap','16g','--pids-limit','256','--ulimit','core=0','--network',args.network,'--group-add',str(os.getgid()),'-e','TMPDIR=/diagnostics/temp','-e','FIRMWARE_SIM_DIAGNOSTICS_DIR=/diagnostics','-v',str(config)+':/simulation:ro','-v',str(diagnostics)+':/diagnostics']
    for node,repo in repos.items():command+=['-v',str((args.cohort/repo).resolve())+':/nodes/'+node+':ro']
    command+=[args.image,'bay','--topology','/simulation/topology.json']
    return out,diagnostics,key,command,name

def latest_probes(diagnostics):
    files=list(diagnostics.glob('bay-live-*/memory-probes.txt'))
    if not files:return None,{}
    rows={}
    for name,sample,value in re.findall(r'SEDS_BAY_PROBE daq ([\w_]+) (\d+)\n(0x[0-9a-fA-F]+)',files[0].read_text(errors='replace')):
        rows.setdefault(int(sample),{})[name]=int(value,16)
    if not rows:return None,{}
    sample=max(rows);return sample,rows[sample]

def kg50_rows(origin,token):
    code,rows=gse.request(origin,'/api/recent?display=true&max_rows=512',token=token)
    if code!=200 or not isinstance(rows,list):return {'error':code}
    picked={}
    for row in rows:
        if row.get('data_type') in('KG50','DAQ_KG50_SELECTED') and row.get('sender_id')=='DAQ':
            picked[row['data_type']]=row
    return {name:{'values':r.get('values'),'timestamp_ms':r.get('timestamp_ms')} for name,r in picked.items()}

class Ui:
    def __init__(self,args,origin,out):
        env=dict(os.environ,GS_ORIGIN=origin,GS_USER='sequence-fixture',GS_PASSWORD='fixture-only-password',PLAYWRIGHT_MODULE=args.playwright_module,UI_EVIDENCE=str(out))
        self.process=subprocess.Popen(['node',str(HERE/'kg50-ui-driver.mjs')],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=(out/'ui-driver.stderr').open('w'),env=env,text=True)
    def call(self,**message):
        self.process.stdin.write(json.dumps(message)+'\n');self.process.stdin.flush()
        line=self.process.stdout.readline()
        if not line:raise RuntimeError('UI driver exited')
        return json.loads(line)
    def close(self):
        try:self.call(cmd='close')
        except (RuntimeError,BrokenPipeError,ValueError):pass
        self.process.wait(timeout=10)

def run(args):
    out,diagnostics,key,command,name=prepare(args)
    (out/'command.json').write_text(json.dumps(command,indent=2))
    if args.prepare_only:return 0
    log=(out/'simulator.log').open('w');process=subprocess.Popen(command,stdout=log,stderr=subprocess.STDOUT)
    timeline=(out/'timeline.jsonl').open('a',buffering=1);checks=[];ui_refusals=[];origin=None;token=None;ui=None;steps=list(STEPS);start=time.monotonic();last_row=-1
    def api(path,body=None):
        nonlocal token
        for attempt in (0,1):
            try:
                code,reply=gse.request(origin,path,body,token)
            except OSError as error:
                return None,{'transport_error':str(error)}
            if code==401 and attempt==0:
                try:token=gse.login(origin,key)
                except (OSError,AssertionError) as error:return None,{'login_error':str(error)}
                continue
            return code,reply
    def snapshot(sample):
        probe_sample,probes=latest_probes(diagnostics)
        word=probes.get('kg50_input_word')
        code,sync=api('/api/calibration/kg50_input')
        ccode,cal=api('/api/calibration')
        row={'virtual_s':sample,'probe_sample':probe_sample,'daq_input_active':None if word is None else word&0xff,'daq_input_requested':None if word is None else (word>>8)&0xff,'probes':probes,'sync_api':{'status':code,'body':sync},'gs_selected':(cal or {}).get('kg50_input') if ccode==200 else None,'gs_file':json.loads((diagnostics/'state/calibration.json').read_text()).get('kg50_input'),'rows':kg50_rows(origin,token) if ccode==200 else None}
        if ui:
            try:row['ui']=ui.call(cmd='read')
            except (RuntimeError,ValueError) as error:row['ui']={'error':str(error)}
        return row
    try:
        while process.poll() is None:
            if time.monotonic()-start>args.timeout:raise TimeoutError('deadline')
            clocks=list((diagnostics/'temp').glob('*/virtual-clock.txt'));logs=list((diagnostics/'temp').glob('*/groundstation-*log'))
            if not clocks or not logs:time.sleep(.2);continue
            if origin is None:
                text='\n'.join(p.read_text(errors='replace')[:65536] for p in logs)
                if not re.search(r'HTTP listening on (?:0\.0\.0\.0|127\.0\.0\.1):3000',text):time.sleep(.2);continue
                info=json.loads(subprocess.check_output(['docker','inspect',name],text=True,timeout=5))[0]
                origin='http://'+info['NetworkSettings']['Networks'][args.network]['IPAddress']+':3000'
                token=gse.login(origin,key)
                if args.ui:ui=Ui(args,origin,out);ui.call(cmd='open')
            try:now=int(clocks[0].read_text().strip())
            except ValueError:time.sleep(.05);continue
            sample=now//1000
            if sample>=last_row+2:
                last_row=sample;timeline.write(json.dumps(snapshot(sample))+'\n')
            while steps and sample>=steps[0][0]:
                at,action,argument=steps.pop(0)
                if action=='checkpoint':
                    row=snapshot(sample);row['checkpoint']=argument;checks.append(row);timeline.write(json.dumps(row)+'\n')
                    (out/'checkpoints.json').write_text(json.dumps(checks,indent=2)+'\n')
                elif action=='select':
                    reply=ui.call(cmd='select',value=argument) if ui else None
                    if reply is None or reply.get('error'):
                        # A UI that refuses is itself a recorded result; the board path is still exercised.
                        code,_=api('/api/calibration/kg50_input',{'input':argument});reply={'ui':reply,'api_status':code}
                        if args.ui:ui_refusals.append({'virtual_s':sample,'input':argument,'reply':reply})
                    timeline.write(json.dumps({'virtual_s':sample,'action':'select','input':argument,'reply':reply})+'\n')
                elif action=='write-gs-file':
                    path=diagnostics/'state/calibration.json';data=json.loads(path.read_text());data['kg50_input']=argument;gse.atomic(path,json.dumps(data))
                    timeline.write(json.dumps({'virtual_s':sample,'action':action,'input':argument})+'\n')
                else:timeline.write(json.dumps({'virtual_s':sample,'action':action})+'\n')
            time.sleep(.25)
        rc=process.wait()
    finally:
        if ui:ui.close()
        if process.poll() is None:
            subprocess.run(['docker','stop','--time','5',name],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL);process.wait(timeout=30)
        log.close()
    verdict=evaluate(checks,args);verdict['ui_refusals']=ui_refusals;verdict['passed']=verdict['passed'] and not ui_refusals
    (out/'result.json').write_text(json.dumps({'simulator_exit':rc,'wall_seconds':time.monotonic()-start,**verdict},indent=2)+'\n')
    print(json.dumps(verdict,indent=2))
    return 0 if rc==0 and verdict['passed'] else 1

EXPECT={'boot-amp2':'amp2','selected-p6':'p6_differential','daq-rebooted-gs-up':'p6_differential','gs-restarted-daq-up':'p6_differential','daq-rebooted-gs-unreachable':'p6_differential','link-restored':'p6_differential','selected-amp2':'amp2','gs-restarted-with-disagreeing-file':'p6_differential'}
WIRE={'amp2':0,'p6_differential':1}
def evaluate(checks,args):
    rows=[]
    for row in checks:
        want=EXPECT[row['checkpoint']];failures=[]
        if row['daq_input_active']!=WIRE[want]:failures.append('DAQ scanning input %s, wanted %s'%(row['daq_input_active'],want))
        online=row['checkpoint']!='daq-rebooted-gs-unreachable'
        if not online and not args.baseline:
            # With the link down the station must admit it cannot see the board.
            if ((row['sync_api'] or {}).get('body') or {}).get('state')!='no_readback':failures.append('Unreachable board reported as %r'%(row['sync_api'],))
        if online:
            if row['gs_selected']!=want:failures.append('GS reports %s, wanted %s'%(row['gs_selected'],want))
            if row['gs_file']!=want:failures.append('GS file holds %s, wanted %s'%(row['gs_file'],want))
            sync=(row['sync_api'] or {}).get('body') or {}
            if not args.baseline and (sync.get('board_input')!=want or sync.get('state')!='synced'):failures.append('API board state %r'%(sync,))
            if args.ui and not args.baseline:
                ui=row.get('ui') or {}
                if ui.get('value')!=want or ui.get('board_attr')!=want+':synced':failures.append('UI %r'%(ui,))
        rows.append({'checkpoint':row['checkpoint'],'virtual_s':row['virtual_s'],'expected':want,'daq_active':row['daq_input_active'],'gs_selected':row['gs_selected'],'failures':failures})
    return {'passed':len(rows)==len(EXPECT) and not any(r['failures'] for r in rows),'checkpoints':rows}

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--network',required=True);p.add_argument('--cohort',type=Path,required=True);p.add_argument('--template',type=Path,required=True);p.add_argument('--image',required=True);p.add_argument('--evidence',type=Path,required=True)
    p.add_argument('--cpu-limit',type=int,default=4);p.add_argument('--timeout',type=int,default=3000);p.add_argument('--prepare-only',action='store_true')
    p.add_argument('--ui',action='store_true');p.add_argument('--playwright-module',default='playwright');p.add_argument('--baseline',action='store_true',help='Record failures on a build without readback, ignoring its absent API')
    p.add_argument('--expect-restore-probes',action='store_true')
    a=p.parse_args();sys.exit(run(a))
