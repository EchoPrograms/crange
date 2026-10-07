"""Bounded, resumable admin ISO uploads; no credentials reach the browser."""
from pathlib import Path
import fcntl
import hashlib
import http.client
import json
import os
import re
import shutil
import uuid
from urllib.parse import urlsplit

SPOOL=Path(os.environ.get('CRANGE_TEMPLATE_MEDIA','/var/lib/crange-template-media'))
CHUNK=8*1024*1024
MAX_SIZE=16*1024**3


def record(upload):
    if not isinstance(upload,str) or not re.fullmatch('[0-9a-f]{32}',upload):raise ValueError('Invalid ISO upload.')
    return SPOOL/(upload+'.json'),SPOOL/(upload+'.iso')


def begin(actor,name,size):
    if not isinstance(name,str) or not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_.-]{0,159}\.iso',name,re.IGNORECASE):raise ValueError('Choose an ISO filename without spaces.')
    if type(size) is not int or not 0 < size <= MAX_SIZE:raise ValueError('ISO size must be between 1 byte and 16 GiB.')
    SPOOL.mkdir(parents=True,exist_ok=True)
    if len(list(SPOOL.glob('*.iso')))>=8:raise ValueError('ISO upload storage is full; remove unused uploads first.')
    if shutil.disk_usage(SPOOL).free < size+2*1024**3:raise ValueError('Controller has insufficient free upload space.')
    upload=uuid.uuid4().hex;meta,file=record(upload)
    file.touch(mode=0o640);meta.write_text(json.dumps({'actor':actor,'name':name,'size':size,'status':'uploading'}));meta.chmod(0o660)
    return {'upload_id':upload,'chunk_size':CHUNK}


def chunk(actor,upload,offset,data):
    meta,file=record(upload);info=json.loads(meta.read_text())
    if info['actor']!=actor or info['status']!='uploading':raise ValueError('Upload is not available to this account.')
    if type(offset) is not int or not data or len(data)>CHUNK or offset+len(data)>info['size']:raise ValueError('Invalid ISO chunk.')
    with file.open('r+b') as stream:
        fcntl.flock(stream,fcntl.LOCK_EX)
        stream.seek(0,2)
        if stream.tell()!=offset:raise ValueError('Upload offset changed; retry from the current position.')
        stream.write(data)
    return {'offset':offset+len(data)}


def finish(actor,upload):
    meta,file=record(upload);info=json.loads(meta.read_text())
    if info['actor']!=actor or info['status']!='uploading' or file.stat().st_size!=info['size']:raise ValueError('ISO upload is incomplete or belongs to another account.')
    info['status']='ready';meta.write_text(json.dumps(info));return info


def transfer(upload,node,api=None):
    from crange import ProxmoxAPI
    from crange_templates import NODES
    if node not in NODES:raise ValueError('Choose a range node.')
    meta,file=record(upload);info=json.loads(meta.read_text())
    if info['status']!='ready' or file.stat().st_size!=info['size']:raise ValueError('ISO upload is not ready.')
    api=api or ProxmoxAPI()
    existing=api.request('GET',f'/nodes/{node}/storage/crange-library/content',{'content':'iso'})
    volid='crange-library:iso/'+info['name']
    if any(m.get('volid')==volid for m in existing):raise ValueError('An ISO with this filename already exists in the shared library; choose a unique name.')
    boundary='crange'+uuid.uuid4().hex
    head=(f'--{boundary}\r\nContent-Disposition: form-data; name="content"\r\n\r\niso\r\n'
          f'--{boundary}\r\nContent-Disposition: form-data; name="filename"; filename="{info["name"]}"\r\nContent-Type: application/octet-stream\r\n\r\n').encode()
    tail=f'\r\n--{boundary}--\r\n'.encode()
    target=urlsplit(api.endpoint)
    connection=http.client.HTTPSConnection(target.hostname,target.port or 443,context=api.ssl_context,timeout=600) if target.scheme=='https' else http.client.HTTPConnection(target.hostname,target.port or 80,timeout=600)
    try:
        connection.putrequest('POST',target.path+f'/nodes/{node}/storage/crange-library/upload')
        connection.putheader('Authorization','PVEAPIToken='+api.token)
        connection.putheader('Content-Type','multipart/form-data; boundary='+boundary)
        connection.putheader('Content-Length',str(len(head)+info['size']+len(tail)))
        connection.endheaders();connection.send(head);digest=hashlib.sha256()
        with file.open('rb') as stream:
            while data:=stream.read(1024*1024):connection.send(data);digest.update(data)
        connection.send(tail);response=connection.getresponse();body=response.read(65536)
        if response.status!=200:raise ValueError(f'ISO storage upload failed with HTTP {response.status}.')
        result=json.loads(body)['data']
        if isinstance(result,str) and result.startswith('UPID:'):api.task(node,result,timeout=900)
    finally:connection.close()
    info.update(status='transferred',node=node,volid=volid,sha256=digest.hexdigest());meta.write_text(json.dumps(info));file.unlink()
    print(f'Uploaded {info["name"]} to {node}: {volid}. SHA256 {info["sha256"]}')


def cancel(actor,upload):
    meta,file=record(upload);info=json.loads(meta.read_text())
    if info['actor']!=actor or info['status'] not in ('uploading','ready'):raise ValueError('Upload cannot be removed by this account.')
    file.unlink(missing_ok=True);meta.unlink()
    return {'removed':True}


def pending(actor):
    if not SPOOL.exists():return []
    result=[]
    for meta in SPOOL.glob('*.json'):
        info=json.loads(meta.read_text())
        if info['actor']==actor and info['status'] in ('uploading','ready'):
            result.append({'id':meta.stem,'name':info['name'],'size':info['size'],'status':info['status']})
    return result
