const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const { webcrypto } = require('node:crypto');
const { File } = require('node:buffer');
const script=fs.readFileSync(__dirname+'/download_widget.html','utf8').match(/<script>([\s\S]*?)<\/script>/)[1];
async function settle(predicate) {
  const until=Date.now()+3000;
  while (!predicate()) {
    if(Date.now()>until) throw new Error('Widget operation did not settle');
    await new Promise(r=>setTimeout(r,5));
  }
}
async function scenario(options={}) {
  const {platform='desktop',helpers=true,native=false,format='pdf',denied=false,missing=false,openLinks=false,rejectLink=false,clipboardDenied=false,priorUpload=false}=options;
  const bytes=Buffer.from(format==='xml'?'<?xml version="1.0"?><article>中文正文</article>':'%PDF-1.7\nwidget bytes\n%%EOF');
  const sha=Buffer.from(await webcrypto.subtle.digest('SHA-256',bytes)).toString('hex');
  const article={doi:'10.1016/test',format,file_name:'article.'+format,mime_type:'application/'+format,byte_size:bytes.length,chunk_size:512*1024,sha256:sha};
  if(options.pageReceipt) article.space_receipt={verified:true,sha256:sha,byte_size:bytes.length,page_url:'https://chatgpt.com/space/page_test'};
  const elements=new Map(),listeners=new Map(),uploads=[],downloads=[],opened=[],copied=[];
  const delivery=options.delivery?{url:'https://download.example.test/download/signed-ticket',expires_at:Date.now()/1000+(options.expired?-1:600)}:null;
  const el=id=>{
    if(!elements.has(id))elements.set(id,{textContent:'',className:'',disabled:false,hidden:['progress','download','saved','browser-download','copy-link','download-url'].includes(id),href:'',value:'',classList:{toggle(){}},events:{},
      removeAttribute(key){delete this[key];},select(){this.selected=true;},addEventListener(name,fn){this.events[name]=fn;}});
    return elements.get(id);
  };
  const document={getElementById:el,documentElement:{style:{setProperty(){}}},body:{scrollHeight:240}};
  function dispatch(data){listeners.get('message')?.({source:parent,data});}
  const parent={postMessage(m){queueMicrotask(()=>{
    const result=value=>dispatch({jsonrpc:'2.0',id:m.id,result:value});
    if(m.method==='ui/initialize')result({hostContext:platform?{platform}:{},hostCapabilities:{...(native?{downloadFile:{}}:{}),...(openLinks?{openLinks:{}}:{})}});
    else if(m.method==='ui/notifications/initialized')dispatch({jsonrpc:'2.0',method:'ui/notifications/tool-result',params:{structuredContent:{ok:true,destination:options.destination||'auto',article},_meta:{download_id:'a'.repeat(48),delivery}}});
    else if(m.method==='tools/call') {
      assert.equal(m.params.name,'read_article_chunk');
      result({structuredContent:{offset:0,byte_count:bytes.length},_meta:{data_base64:bytes.toString('base64')}});
    } else if(m.method==='ui/download-file') {
      downloads.push(m.params);
      const content=m.params.contents[0];assert.equal(content.type,'resource');assert.equal(content.resource.mimeType,article.mime_type);
      assert.equal(content.resource.uri,'file:///article.'+format);
      assert.deepEqual(Buffer.from(content.resource.blob,'base64'),bytes);
      if(missing)dispatch({jsonrpc:'2.0',id:m.id,error:{code:-32601,message:'Method not found'}});
      else result(denied?{isError:true}:{});
    } else if(m.method==='ui/open-link') {opened.push(m.params);result(rejectLink?{isError:true}:{});}
  });}};
  const openai={widgetState:priorUpload?{privateContent:{download_id:'a'.repeat(48),file_id:'file_test',library_requested:true}}:{},setWidgetState(s){this.widgetState=s;},openExternal(){throw new Error('Legacy link interception must never be used');}};
  if(helpers){
    openai.selectFiles=async()=>[];
    openai.uploadFile=async(file,opts)=>{assert.deepEqual(Buffer.from(await file.arrayBuffer()),bytes);uploads.push(opts);return {fileId:'file_test'};};
    openai.getFileDownloadUrl=async()=>({downloadUrl:'https://files.oaiusercontent.com/article.'+format});
  }
  const window={openai,addEventListener(name,fn){listeners.set(name,fn);}};
  const navigator={clipboard:{writeText:async value=>{if(clipboardDenied)throw Error('clipboard denied');copied.push(value);}}};
  vm.runInNewContext(script,{window,document,parent,navigator,crypto:webcrypto,File,URL,Uint8Array,atob,btoa,Math,Map,Promise,Error,Object,Array,Number,String,setTimeout,clearTimeout,ResizeObserver:class{observe(){}}});
  const status=()=>el('status').textContent;
  const click=async id=>{let prevented=false;await el(id).events.click?.({preventDefault(){prevented=true;}});return prevented;};
  const space=platform==='web'||options.destination==='space'||!platform;
  if(space){
    await settle(()=>!el('save').disabled&&status().includes('点击'));
    assert.equal(uploads.length,0);await click('save');
    await settle(()=>status().includes('尚未确认写入 Space')||el('status').className==='error');
    if(helpers){assert.equal(uploads[0]?.library,true);assert.ok(status().includes('尚未确认写入 Space'));}
    else assert.equal(el('status').className,'error');
    assert.equal(downloads.length,0);assert.equal(el('saved').hidden,true);
    return;
  }
  await settle(()=>!el('download').hidden||el('status').className==='error');
  if(native){
    assert.equal(uploads.length,0);assert.ok(el('download').textContent.includes('保存'));
    assert.equal(await click('download'),true);
    await settle(()=>!el('prepare').disabled&&downloads.length===1);
    if(missing){assert.equal(uploads[0]?.library,false);assert.equal(el('browser-download').hidden,false);assert.ok(status().includes('改为浏览器'));}
    else if(denied){assert.equal(el('status').className,'error');assert.ok(status().includes('取消'));assert.ok(!status().includes('接受'));}
    else assert.ok(status().includes('客户端已接受保存请求'));
    return;
  }
  if(delivery){
    assert.equal(uploads.length,0);
    if(options.expired){assert.equal(el('status').className,'error');assert.ok(status().includes('过期'));}
    else {assert.equal(el('download').href,delivery.url);assert.equal(el('copy-link').hidden,false);assert.ok(status().includes('10分钟'));}
    return;
  }
  if(!helpers){assert.equal(el('status').className,'error');assert.equal(el('download').hidden,true);return;}
  assert.equal(el('browser-download').hidden,false);assert.equal(el('download').target,'_blank');
  assert.equal(await click('download'),openLinks);
  if(openLinks){await settle(()=>opened.length===1&&status().includes(rejectLink?'未能打开':'请求打开'));}
  await click('copy-link');
  if(clipboardDenied){assert.equal(el('download-url').hidden,false);assert.equal(el('download-url').selected,true);}
  else assert.equal(copied[0],'https://files.oaiusercontent.com/article.'+format);
}
(async()=>{
  const cases=[{native:true},{native:true,format:'xml'},{native:true,denied:true},{native:true,missing:true},
    {},{format:'xml'},{openLinks:true},{openLinks:true,rejectLink:true},{clipboardDenied:true},{helpers:false},
    {platform:'web'},{platform:'web',helpers:false},{platform:'web',pageReceipt:true},
    {platform:'desktop',destination:'space',priorUpload:true},
    {helpers:false,delivery:true},{helpers:false,delivery:true,expired:true}];
  for(const c of cases)await scenario(c);
  console.log(cases.length+' widget scenarios passed: PDF/XML host saves transfer exact bytes; cancellation and unsupported methods are handled; link clicks are not silently swallowed; browser and copy fallbacks work; Space uploads remain unverified.');
})().catch(error=>{console.error(error);process.exitCode=1;});
