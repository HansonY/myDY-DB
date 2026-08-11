/* 自动存链路自检 —— node scripts/autosave_selftest.js extension/background.js
 *
 * 为什么留着:这条链路的 bug 都很隐蔽(URL 不变、指纹只算了半页、
 * 剔除自变字眼后留了空格……),表现全是「就是不存」,没法靠看代码发现。
 * 改动 background.js / bridge.js 的指纹或去重逻辑之后跑一遍。
 *
 * ⚠️ 三处指纹口径必须一致,改一处就得改另两处:
 *     extension/bridge.js  fingerprint()
 *     extension/background.js  hashText()
 *     backend/boss_detect.py  dedupe_key()
 */
/* background.js 自动存链路的完整验证。
 * 桩件里 PAGE_TEXT 可变 —— 去重现在按**实际抓到的正文**算,
 * 所以测试必须真的改内容,而不是只改传进来的指纹。 */
const fs=require('fs'), vm=require('vm');
const STORE={}, msgL=[]; let fetchCalls=[]; let PAGE_TEXT='';
const chrome={
 runtime:{onInstalled:{addListener(){}},onMessage:{addListener:f=>msgL.push(f)},
   getPlatformInfo:()=>Promise.resolve({}),sendMessage:()=>Promise.resolve({})},
 storage:{local:{get:k=>Promise.resolve(typeof k==='string'?{[k]:STORE[k]}
   :Array.isArray(k)?Object.fromEntries(k.map(x=>[x,STORE[x]])):{...STORE}),
   set:o=>{Object.assign(STORE,o);return Promise.resolve()},
   remove:k=>{[].concat(k).forEach(x=>delete STORE[x]);return Promise.resolve()}},
   onChanged:{addListener(){}}},
 tabs:{onUpdated:{addListener(){}},onRemoved:{addListener(){}},
   get:id=>Promise.resolve({id,url:'https://www.zhipin.com/web/geek/recommend'}),
   create:()=>Promise.resolve({id:9}),update:()=>Promise.resolve(),
   remove:()=>Promise.resolve(),sendMessage:()=>Promise.resolve({ok:true})},
 webNavigation:{onHistoryStateUpdated:{addListener(){}}},
 scripting:{executeScript:()=>Promise.resolve([{result:{
   url:'https://www.zhipin.com/web/geek/recommend',title:'BOSS直聘',h1:'x',
   text:PAGE_TEXT,len:PAGE_TEXT.length}}])},
 sidePanel:{setPanelBehavior:()=>Promise.resolve()},
};
globalThis.chrome=chrome;
globalThis.fetch=(u,o)=>{fetchCalls.push(1);
  return Promise.resolve({ok:true,json:()=>Promise.resolve({queued:1,detect:{}})});};
vm.runInThisContext(fs.readFileSync(process.argv[2],'utf8'),{filename:'background.js'});

const LEFT='高级ios研发工程师 14-18K 乔山健康 iOS开发工程师 18-25K 谨福实业 '.repeat(8);
const JOB_A=' 【上海】抖音 iOS 开发工程师 40-70K 职位描述 负责移动产品研发';
const JOB_B=' 高级ios研发工程师 14-18K 职位描述 负责健康类 App 的 iOS 端开发';
const tab={tab:{id:1,url:'https://www.zhipin.com/web/geek/recommend'}};
const fire=m=>msgL.forEach(f=>{try{f(m,tab,()=>{})}catch(e){console.log('  ✗ 抛错',e.message)}});
const go=async(text)=>{PAGE_TEXT=text; fetchCalls=[];
  fire({type:'pageChanged',fp:'x'+Math.random(),url:tab.tab.url});
  await new Promise(r=>setTimeout(r,500)); return fetchCalls.length;};
const T=[];const ok=(c,t)=>T.push((c?'  ✓ ':'  ✗ ')+t);

(async()=>{
 STORE.auto=false;
 ok(await go(LEFT+JOB_A)===0,'开关关着 → 不存');
 STORE.auto=true;
 ok(await go(LEFT+JOB_A)===1,'开关打开 → 存');
 ok(await go(LEFT+JOB_A)===0,'同一份内容再来 → 不重复(整页加载+内容变化 各触发一次也只存一份)');
 ok(await go(LEFT+JOB_B)===1,'左右分栏换岗位(URL 不变、左列不变、右列变)→ 存');
 ok(await go(LEFT+JOB_B+' 刚刚')===0,'只多了「刚刚」这种自变字眼 → 不重复');
 ok(await go(LEFT+JOB_B+' 3分钟前')===0,'只多了「3分钟前」→ 不重复');
 ok(await go(LEFT+JOB_A)===1,'切回第一个岗位 → 存(内容确实不同)');
 ok(await go('短')===0,'页面太短 → 跳过');
 console.log(T.join('\n'));
 console.log('\n  '+T.filter(x=>x.includes('✓')).length+'/'+T.length+' 通过');
})();
