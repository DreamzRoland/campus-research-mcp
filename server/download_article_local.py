"""Save campus article bytes locally over existing SSH; standard library only."""
import argparse,hashlib,json,os,re,subprocess,sys
import xml.etree.ElementTree as ET
from pathlib import Path

MAX_FILE=20*1024*1024
REMOTE_COMMAND=(
    'sudo -n systemd-run --quiet --wait --pipe --collect '
    '-p User=campus-scopus -p Group=campus-scopus '
    '-p WorkingDirectory=/opt/campus-scopus '
    '-p Environment=PYTHONDONTWRITEBYTECODE=1 '
    '-p LoadCredential=elsevier.json:/etc/campus-scopus/secrets.json '
    '/var/lib/campus-scopus/.venv/bin/python /opt/campus-scopus/export_article_stream.py'
)

class ExportError(Exception):pass

def decode_response(raw,doi,file_format):
    header,separator,body=raw.partition(b'\n')
    if not separator or len(header)>4096:raise ExportError('SSH未返回有效的文献交付结果。')
    try:info=json.loads(header)
    except ValueError:raise ExportError('SSH返回了无法识别的交付结果。') from None
    if info.get('ok') is not True:raise ExportError(str(info.get('error','校园主机未完成下载。'))[:500])
    filename=info.get('file_name','')
    if info.get('doi')!=doi or info.get('format')!=file_format:raise ExportError('返回的文献与请求不一致。')
    if not re.fullmatch(r'[A-Za-z0-9._-]{1,200}',filename) or not filename.endswith('.'+file_format):
        raise ExportError('返回的文件名无效。')
    if not 0<len(body)<=MAX_FILE or info.get('byte_size')!=len(body):raise ExportError('文件长度校验失败。')
    if hashlib.sha256(body).hexdigest()!=info.get('sha256'):raise ExportError('文件SHA256校验失败。')
    if file_format=='pdf':
        pages=info.get('pdf_page_count')
        if not body.startswith(b'%PDF-') or isinstance(pages,bool) or not isinstance(pages,int) or pages<2:
            raise ExportError('PDF未通过页数核验，可能只有首页预览。')
    else:
        try:root=ET.fromstring(body)
        except ET.ParseError:raise ExportError('XML结构无效。') from None
        if root.tag.rsplit('}',1)[-1] not in ('full-text-retrieval-response','doc'):raise ExportError('不是文献XML。')
        info['xml_sections']=sum(e.tag.rsplit('}',1)[-1]=='section' for e in root.iter())
        info['xml_references']=sum(e.tag.rsplit('}',1)[-1]=='bib-reference' for e in root.iter())
    return info,body

def save_verified(info,body,directory):
    directory=Path(directory).resolve();directory.mkdir(parents=True,exist_ok=True)
    filename=info['file_name']
    for number in range(100):
        stem=Path(filename).stem;suffix=Path(filename).suffix
        candidate=directory/(filename if number==0 else f'{stem}-{number}{suffix}')
        if candidate.exists():
            if candidate.is_file() and not candidate.is_symlink() and hashlib.sha256(candidate.read_bytes()).hexdigest()==info['sha256']:
                return candidate
            continue
        try:fd=os.open(candidate,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
        except FileExistsError:continue
        try:
            with os.fdopen(fd,'wb') as stream:stream.write(body);stream.flush();os.fsync(stream.fileno())
        except OSError:
            candidate.unlink(missing_ok=True);raise
        return candidate
    raise ExportError('目标目录中同名文件过多，请选择其他目录。')

def download(doi,file_format,directory):
    if not re.fullmatch(r'10\.\d{4,9}/[^\s\x00-\x1f]{1,250}',doi):raise ExportError('请提供有效DOI。')
    request=json.dumps({'doi':doi,'format':file_format}).encode()
    # Fixed SSH alias and remote command; DOI never enters shell command text.
    try:
        result=subprocess.run(['ssh','-T','-o','BatchMode=yes','-o','ConnectTimeout=15','research-host',REMOTE_COMMAND],
                              input=request,stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=150)
    except subprocess.TimeoutExpired:raise ExportError('SSH或文献下载超时，请稍后重试。') from None
    except OSError:raise ExportError('无法启动SSH，请确认本机已安装OpenSSH。') from None
    if not result.stdout:raise ExportError('SSH连接失败；请在本机终端确认 ssh research-host 可以免密连接且 sudo -n 可用。')
    info,body=decode_response(result.stdout,doi,file_format)
    if result.returncode!=0:raise ExportError('远端传输未正常完成，未保存文件。')
    path=save_verified(info,body,directory)
    return {'ok':True,'path':str(path),'file_name':path.name,'byte_size':len(body),'sha256':info['sha256'],
            **{k:info[k] for k in ['pdf_page_count','fulltext_verified','xml_sections','xml_references'] if k in info}}

def main():
    parser=argparse.ArgumentParser(description='通过现有校园SSH安全下载PDF或XML到本机，不需要公网文件服务。')
    parser.add_argument('doi');parser.add_argument('--format',choices=['pdf','xml'],default='pdf')
    parser.add_argument('--output-dir',type=Path,default=Path(__file__).resolve().parent/'downloads')
    args=parser.parse_args()
    try:result=download(args.doi,args.format,args.output_dir)
    except (ExportError,OSError) as error:
        result={'ok':False,'error':str(error) if isinstance(error,ExportError) else '本机文件无法写入，请检查目标目录。'}
    print(json.dumps(result,ensure_ascii=True))
    return 0 if result['ok'] else 1

if __name__=='__main__':raise SystemExit(main())
