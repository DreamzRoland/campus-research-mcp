"""Explicit public-network smoke test. Does not deploy, download PDFs or load keys."""
import json
import argparse
import asyncio
import os
import sys
from pathlib import Path
import ai_search as ai

async def check_mcp():
    from mcp import ClientSession,StdioServerParameters
    from mcp.client.stdio import stdio_client
    path=os.environ.get('CAMPUS_SERVER_PATH',str(Path(__file__).with_name('server.py')))
    params=StdioServerParameters(command=sys.executable,args=[path],env=dict(os.environ))
    checks=[]
    async with stdio_client(params) as (read,write):
        async with ClientSession(read,write) as session:
            await session.initialize()
            queries=[('get_arxiv_record',{'arxiv_id':'1706.03762'}),
                ('search_arxiv',{'query':'ti:"Attention Is All You Need"','count':1}),
                ('search_ai_conferences',{'query':'"attention is all you need"','conferences':'neurips','count':1,'from_year':2017,'to_year':2017}),
                ('search_literature',{'arxiv_query':'ti:"Attention Is All You Need"','conference_query':'"attention is all you need"','conferences':'neurips','conference_year':2017,'count_per_source':1})]
            for name,args in queries:
                result=await session.call_tool(name,args)
                assert not result.is_error,name
                data=result.structured_content or json.loads(''.join(x.text for x in result.content if x.type=='text'))
                assert data['ok'],name
                if name=='get_arxiv_record':assert data['record']['title']=='Attention Is All You Need'
                elif name=='search_literature':
                    assert set(data['source_status'])=={'arxiv','dblp'} and not data['partial_failure']
                    assert data['results']
                else:assert data['records']
                checks.append({'tool':name,'ok':True,'returned':len(data.get('records',data.get('results',[data.get('record')]))),
                    'sources':list(data.get('source_status',{}))})
    return {'ok':True,'transport':'actual_mcp_stdio','cases':checks,'api_keys_used':False,'production_changed':False}

def main():
    parser=argparse.ArgumentParser();parser.add_argument('output',nargs='?');parser.add_argument('--mcp',action='store_true')
    args=parser.parse_args()
    if args.mcp:
        report=asyncio.run(check_mcp())
        if args.output:Path(args.output).write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
        print(json.dumps(report,ensure_ascii=False),flush=True);return
    results=[]
    record=ai.get_arxiv_record('1706.03762')
    assert record['ok'] and record['record']['title']=='Attention Is All You Need'
    results.append({'case':'arxiv_id','identifier':record['record']['arxiv_id'],'title':record['record']['title'],'ok':True})
    search=ai.search_arxiv('ti:"attention"',count=2,categories='cs.LG,cs.CL',from_year=2017,to_year=2017)
    assert search['ok'] and search['records'] and all(r['abstract_available'] for r in search['records'])
    results.append({'case':'arxiv_search','ok':True,'returned':len(search['records']),'total_results':search['total_results']})
    for query,codes,year in [('"attention is all you need"','neurips',2017),('diffusion','icml,iclr',2024),('transformer','cvpr,acl,aaai',2023)]:
        result=ai.search_ai_conferences(query,codes,count=3,from_year=year,to_year=year)
        assert result['ok'] and result['records'],(query,codes)
        assert all(r['main_venue_filter_applied'] and r['year']==year for r in result['records'])
        results.append({'case':'conference_search','query':query,'conferences':codes,'year':year,'ok':True,
            'records':[{k:r[k] for k in ('identifier','title','conference','venue','year')} for r in result['records']]})
    report={'ok':True,'cases':results,'api_keys_used':False,'production_changed':False}
    if args.output:Path(args.output).write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps(report,ensure_ascii=False),flush=True)

if __name__=='__main__':main()
