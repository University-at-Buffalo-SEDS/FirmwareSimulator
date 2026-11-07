import argparse,importlib.util,unittest
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('kg50_scene',ROOT/'scripts/test-kg50-input-sync.py');scene=importlib.util.module_from_spec(spec);spec.loader.exec_module(scene)

def checkpoint(name,active,selected=None,state='synced',ui=None):
    want=scene.EXPECT[name];selected=selected or want
    body={'selected':selected,'board_input':want if state=='synced' else None,'state':state}
    return {'checkpoint':name,'virtual_s':1,'daq_input_active':active,'gs_selected':selected,'gs_file':selected,'sync_api':{'status':200,'body':body},'ui':ui or {'value':want,'board_attr':want+':synced'}}

def passing():
    rows=[]
    for name,want in scene.EXPECT.items():
        offline=name=='daq-rebooted-gs-unreachable'
        rows.append(checkpoint(name,scene.WIRE[want],state='no_readback' if offline else 'synced'))
    return rows

class Kg50SceneTests(unittest.TestCase):
    args=argparse.Namespace(baseline=False,ui=True)
    def test_inputs_carry_codes_that_identify_the_physical_channel(self):
        self.assertNotEqual(scene.CODES[1],scene.CODES[9])
        self.assertEqual(scene.CODES[9],(scene.CODES[2]-scene.CODES[3])&0xffffff)
    def test_every_step_has_a_verdict_and_stays_in_the_run(self):
        names=[s[2] for s in scene.STEPS if s[1]=='checkpoint']
        self.assertEqual(set(names),set(scene.EXPECT))
        self.assertTrue(all(s[0]<scene.SAMPLES for s in scene.STEPS))
        self.assertTrue(all(r['after_sample']<scene.SAMPLES for r in scene.REBOOTS))
    def test_full_agreement_passes(self):
        self.assertTrue(scene.evaluate(passing(),self.args)['passed'])
    def test_missing_checkpoint_fails(self):
        self.assertFalse(scene.evaluate(passing()[:-1],self.args)['passed'])
    def test_wrong_board_input_fails_even_when_the_station_agrees_with_itself(self):
        rows=passing();rows[1]['daq_input_active']=0
        self.assertFalse(scene.evaluate(rows,self.args)['passed'])
    def test_guessing_while_the_board_is_unreachable_fails(self):
        rows=passing();offline=next(r for r in rows if r['checkpoint']=='daq-rebooted-gs-unreachable')
        offline['sync_api']['body']['state']='synced'
        self.assertFalse(scene.evaluate(rows,self.args)['passed'])
    def test_a_ui_showing_only_the_selection_fails(self):
        rows=passing();rows[1]['ui']['board_attr']=None
        self.assertFalse(scene.evaluate(rows,self.args)['passed'])

if __name__=='__main__':unittest.main()
