#!/usr/bin/env python3
"""Explicit campus network/MCP evidence, separate from offline regressions."""
import argparse
import asyncio
import hashlib
import json
import os
import time
from pathlib import Path
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


def unpack(result):
    assert not result.is_error, 'MCP tool execution failed'
    return result.structured_content or json.loads(next(c.text for c in result.content if c.type == 'text'))


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', default='')
    parser.add_argument('--download', action='store_true')
    args = parser.parse_args()
    params = StdioServerParameters(command=os.sys.executable, args=[os.environ.get('CAMPUS_SERVER_PATH', '/opt/campus-scopus/server.py')], env=dict(os.environ))
    report = {'version': '1.13.0', 'cases': [], 'started_at': time.time(), 'work_task_created': False}
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            available = await session.list_tools()
            assert len(available.tools) == 40
            report['tool_count'] = len(available.tools)
            cat = unpack(await session.call_tool('list_research_sources', {}))
            report['sources'] = cat['sources']
            for name, tool, kw in [
                ('datacite_theses', 'search_theses', {'query': 'machine learning', 'sources': 'datacite', 'count_per_source': 2}),
                ('openaire_theses', 'search_theses', {'query': 'machine learning', 'sources': 'openaire', 'filters': {'degree': 'doctoral'}, 'count_per_source': 2}),
                ('thesesfr', 'search_theses', {'query': 'machine learning', 'sources': 'thesesfr', 'count_per_source': 2}),
                ('openalex', 'search_literature', {'query': 'diffusion transformer', 'sources': 'openalex', 'count_per_source': 2}),
                ('biomedical', 'search_literature', {'query': 'machine learning', 'profile': 'biomedical', 'sources': 'pubmed,europepmc', 'count_per_source': 1}),
                ('official_iclr', 'search_literature', {'query': 'diffusion', 'profile': 'ai', 'sources': 'openreview', 'filters': {'conferences': 'iclr', 'from_year': 2024, 'to_year': 2024}, 'count_per_source': 2}),
                ('official_pmlr', 'search_literature', {'query': 'diffusion', 'profile': 'ai', 'sources': 'proceedings', 'filters': {'conferences': 'icml', 'from_year': 2024, 'to_year': 2024}, 'count_per_source': 1}),
                ('official_neurips', 'search_literature', {'query': 'diffusion', 'profile': 'ai', 'sources': 'proceedings', 'filters': {'conferences': 'neurips', 'from_year': 2024, 'to_year': 2024}, 'count_per_source': 1}),
                ('official_cvf', 'search_literature', {'query': 'diffusion', 'profile': 'ai', 'sources': 'proceedings', 'filters': {'conferences': 'cvpr', 'from_year': 2024, 'to_year': 2024}, 'count_per_source': 1}),
                ('official_acl', 'search_literature', {'query': 'transformer', 'profile': 'ai', 'sources': 'proceedings', 'filters': {'conferences': 'acl', 'from_year': 2024, 'to_year': 2024}, 'count_per_source': 1}),
            ]:
                start = time.monotonic()
                out = unpack(await session.call_tool(tool, kw))
                report['cases'].append({'case': name, 'ok': out['ok'], 'record_count': out['record_count'], 'elapsed_seconds': round(time.monotonic() - start, 2),
                                        'source_status': out['source_status'], 'run_id': out['run_id'],
                                        'samples': [{k: r.get(k) for k in ('record_id', 'title', 'identifiers', 'thesis', 'evidence')} for r in (out['results'] + out['unknown_degree_results'])[:2]]})
                print(json.dumps(report['cases'][-1], ensure_ascii=False), flush=True)
            record = unpack(await session.call_tool('get_literature_record', {'identifier': '1706.03762', 'source': 'arxiv'}))
            assert record['ok'], record
            rid = record['record']['record_id']
            report['arxiv_record'] = {k: record['record'].get(k) for k in ('record_id', 'title', 'identifiers')}
            if args.download:
                start = unpack(await session.call_tool('download_literature', {'record_id': rid}))
                jobid = start['job_id']
                for _ in range(100):
                    result = await session.call_tool('get_research_job', {'job_id': jobid, 'wait_seconds': 20})
                    out = unpack(result)
                    print(json.dumps({'case': 'native_download_progress', 'job_id': jobid, 'state': out['state'], 'progress': out['progress']}), flush=True)
                    if out['state'] not in ('queued', 'running'):
                        break
                assert out['state'] == 'completed', out
                resource = next(c for c in result.content if c.type == 'resource_link')
                readback = await session.read_resource(resource.uri)
                data = __import__('base64').b64decode(readback.contents[0].blob)
                info = out['result']['file']
                assert len(data) == info['byte_size'] and hashlib.sha256(data).hexdigest() == info['sha256']
                report['download'] = {'ok': True, 'job_id': jobid, **info, 'resource_readback_verified': True}
            related = unpack(await session.call_tool('get_literature_record', {'identifier': '10.1038/s41586-023-06221-2', 'source': 'openalex'}))
            if related['ok']:
                edges = unpack(await session.call_tool('get_related_papers', {'record_id': related['record']['record_id'], 'count': 3}))
                report['citations'] = edges
    report['ok'] = all(c['ok'] and c['record_count'] > 0 for c in report['cases'])
    if args.output:
        Path(args.output).write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({'ok': report['ok'], 'cases': len(report['cases']), 'output': bool(args.output)}, ensure_ascii=False))


if __name__ == '__main__':
    asyncio.run(main())
