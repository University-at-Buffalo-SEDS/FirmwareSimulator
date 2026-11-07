// Drives the real bundled UI against a running simulated station. One JSON
// command per stdin line, one JSON reply per stdout line.
import readline from 'node:readline';
import {pathToFileURL} from 'node:url';
const moduleName=process.env.PLAYWRIGHT_MODULE||'playwright';
const {chromium}=await import(moduleName.startsWith('/')?pathToFileURL(moduleName).href:moduleName);
const origin=process.env.GS_ORIGIN;
const browser=await chromium.launch({headless:true,executablePath:process.env.CHROME_BINARY||undefined});
const page=await (await browser.newContext({viewport:{width:1400,height:900}})).newPage();
const shot=async name=>{if(process.env.UI_EVIDENCE)await page.screenshot({path:`${process.env.UI_EVIDENCE}/ui-${name}.png`}).catch(()=>{});};
const selector=()=>page.locator('#kg50-input');
async function login(){
 const user=page.locator('#gs26-login-username');
 if(await user.isVisible().catch(()=>false)){await user.fill(process.env.GS_USER);await page.locator('#gs26-login-password').fill(process.env.GS_PASSWORD);await page.getByRole('button',{name:'Sign In',exact:true}).click();}
}
async function openSelector(){
 if(await selector().isVisible().catch(()=>false))return;
 await login();
 // A lost connection retires an open settings form; the operator reloads it.
 const reload=page.getByRole('button',{name:'Reload settings',exact:true});
 if(await reload.isVisible().catch(()=>false)){await reload.click({timeout:5000});await selector().waitFor({timeout:15000});return;}
 const scope=page.getByRole('navigation',{name:'Settings scope'});
 if(!await scope.isVisible().catch(()=>false))await page.getByRole('link',{name:'Settings',exact:true}).click({timeout:30000});
 await scope.getByRole('button',{name:'Ground Station',exact:true}).click({timeout:10000});
 await page.getByRole('navigation',{name:'Ground Station settings sections'}).getByRole('button',{name:'Loadcells',exact:true}).click({timeout:10000});
 await selector().waitFor({timeout:30000});
}
async function read(){
 const present=await selector().isVisible().catch(()=>false);
 if(!present)return {visible:false,body:(await page.locator('body').innerText().catch(()=>'')).slice(0,600)};
 return {visible:true,value:await selector().inputValue(),enabled:await selector().isEnabled(),
  board_attr:await page.locator('[data-kg50-board]').first().getAttribute('data-kg50-board').catch(()=>null),
  status:await page.locator('section:has(#kg50-input) [role=status]').allInnerTexts()};
}
const handlers={
 async open(){await page.goto(origin+'/dashboard');await page.waitForTimeout(1000);await login();await openSelector();await shot('open');return read();},
 async read(){try{await openSelector();}catch(e){return {visible:false,error:String(e).slice(0,300),body:(await page.locator('body').innerText().catch(()=>'')).slice(0,600)};}return read();},
 async select({value}){
  await openSelector();const before=await read();await shot('before-select-'+value);
  // Actions start disabled on every production layout; an operator enables them from the header.
  let enabledActions=false;
  if(!before.enabled){const toggle=page.getByRole('button',{name:'Actions Disabled',exact:true});
   const inline=page.getByRole('button',{name:'Enable actions',exact:true});
   await (await inline.isVisible().catch(()=>false)?inline:toggle).click({timeout:5000});enabledActions=true;await selector().waitFor({timeout:10000});await page.waitForTimeout(500);}
  await selector().selectOption(value,{timeout:15000});await page.waitForTimeout(500);const after=await read();await shot('select-'+value);return {before,enabledActions,after};
 },
 async close(){await browser.close();setImmediate(()=>process.exit(0));return {closed:true};},
};
for await(const line of readline.createInterface({input:process.stdin})){
 const message=JSON.parse(line);
 let reply;try{reply=await handlers[message.cmd](message);}catch(e){reply={error:String(e).slice(0,500)};}
 process.stdout.write(JSON.stringify(reply)+'\n');
}
