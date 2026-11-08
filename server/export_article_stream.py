"""Private SSH export: JSON request on stdin, bounded metadata + bytes on stdout."""
import hashlib,json,sys
from pathlib import Path
sys.path.insert(0,'/opt/campus-scopus')
from server import secret
from article_downloads import DownloadError,MAX_FILE,cache_dir,prepare_article

def main():
    try:
        raw=sys.stdin.buffer.read(4097)
        if len(raw)>4096:raise DownloadError('请求过长。')
        request=json.loads(raw)
        doi=request['doi'];file_format=request.get('format','pdf')
        if not isinstance(doi,str) or not isinstance(file_format,str):raise DownloadError('请求参数无效。')
        info,token=prepare_article(doi,file_format,secret('SCIENCEDIRECT_API_KEY'),secret('ELSEVIER_INST_TOKEN'))
        body=(cache_dir()/(token+'.bin')).read_bytes()
        if not 0<len(body)<=MAX_FILE or len(body)!=info['byte_size'] or hashlib.sha256(body).hexdigest()!=info['sha256']:
            raise DownloadError('远端缓存完整性校验失败。')
        fields=['doi','format','file_name','mime_type','byte_size','sha256','pdf_page_count','fulltext_verified']
        reply={'ok':True,**{k:info[k] for k in fields if k in info}}
        output=sys.stdout.buffer
        output.write(json.dumps(reply,ensure_ascii=True,separators=(',',':')).encode()+b'\n')
        output.write(body);output.flush()
        return 0
    except DownloadError as error:
        message=str(error)
    except (OSError,ValueError,KeyError,TypeError):
        message='请求无效或校园主机文件不可用。'
    sys.stdout.buffer.write(json.dumps({'ok':False,'error':message},ensure_ascii=True).encode()+b'\n')
    return 1

if __name__=='__main__':raise SystemExit(main())
