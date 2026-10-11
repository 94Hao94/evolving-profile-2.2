import { createElement } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { NextIntlClientProvider } from 'next-intl';
import { readFileSync } from 'node:fs';
import { expect,it } from 'vitest';
import { projectSessionContextNode } from '@/lib/context-node';
import { ContextDetails } from '@/components/context-memory-view';

const raw={context_id:'session:test',session_id:'test',status:'raw_available_summary_failed',
 error_code:'scenario_state_invalid',summary_failure_detail:{reason:'state_contract_violations',
 fields:['goal/text'],violations:[{field:'goal/text',reason:'text_over_limit',actual:206,maximum:180}],
 secret:'do-not-show'}};
it('preserves safe processing diagnostics through the real selected-session projection',()=>{
 const node=projectSessionContextNode(raw as any) as any;
 expect(node.processing).toEqual({state:'failed',errorCode:'scenario_state_invalid',reason:'state_contract_violations',
  violations:[{field:'goal/text',reason:'text_over_limit',actual:206,maximum:180}]});
 expect(JSON.stringify(node)).not.toContain('do-not-show');
});
it.each(['en','zh-CN','de'])('shows failures in real ContextDetails with translated controls %s',locale=>{
 const messages=JSON.parse(readFileSync(new URL(`../../src/messages/${locale}.json`,import.meta.url),'utf8'));
 const node=projectSessionContextNode(raw as any) as any;
 const html=renderToStaticMarkup(createElement(NextIntlClientProvider,{locale,messages,timeZone:'Asia/Shanghai',
  children:createElement(ContextDetails,{node,typeName:()=> 'Session'})}));
 expect(html).toContain('scenario_state_invalid');expect(html).toContain('206');expect(html).toContain('180');
 expect(html).toContain(locale==='zh-CN' ? '摘要处理失败' : 'Summary processing failed');
 if(locale!=='zh-CN')expect(html).not.toMatch(/[\u3400-\u9fff]/u);
});
it('does not inherit old errors into a reviewed episode or expose arbitrary nested diagnostics',()=>{
 const node=projectSessionContextNode({...raw,status:'model_reviewed',episodes:[{episode_id:'episode:test',status:'model_reviewed'}]} as any) as any;
 expect(node.processing).toBeUndefined();expect(node.episodes[0].processing).toBeUndefined();
 const bad=projectSessionContextNode({...raw,error_code:'private-key-token',summary_failure_detail:{
  reason:'model-secret',violations:[{field:'private-key',reason:'text_over_limit',actual:999},
   {field:'goal/text',reason:{key:'secret'},actual:99}]}} as any) as any;
 expect(bad.processing).toEqual({state:'failed'});
});

it.each(['en','zh-CN'])('identifies a native review without calling it provider HTTP %s',locale=>{
 const messages=JSON.parse(readFileSync(new URL(`../../src/messages/${locale}.json`,import.meta.url),'utf8'));
 const node={id:'session:reviewed',type:'session',label:'reviewed',status:'model_reviewed',summary:{compact:'Navigation summary'},
  automatedSourceCoverage:{status:'reviewed',reviewedSourceMessageCount:2,reviewTransport:'native_agent_review_not_provider_http'}};
 const html=renderToStaticMarkup(createElement(NextIntlClientProvider,{locale,messages,timeZone:'Asia/Shanghai',children:createElement(ContextDetails,{node:node as any,typeName:()=> 'Session'})}));
 expect(html).toContain(locale==='zh-CN'?'独立智能体原文复核':'Independent native agent source review');
});
