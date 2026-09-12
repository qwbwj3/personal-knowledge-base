"""Render one source PDF page and bounded crops; no OCR inference or network."""
from __future__ import annotations
import contextlib
import hashlib
import json
import math
from pathlib import Path
import socket
import sys


def hash_file(path):
    h=hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda:stream.read(1024*1024),b''): h.update(block)
    return h.hexdigest()


def render(job):
    import pypdfium2 as pdfium
    source, target = Path(job['pdf']),Path(job['output'])
    if hash_file(source) != job['expected_sha256'] or target.exists():
        raise ValueError('Wrong source revision or output already exists')
    images=[]
    with pdfium.PdfDocument(str(source)) as doc:
        if type(job['page']) is not int or not 1<=job['page']<=len(doc): raise ValueError('Invalid page')
        with contextlib.closing(doc[job['page']-1]) as page:
            w,h=page.get_size()
            if not all(math.isfinite(v) and v>0 for v in (w,h)):raise ValueError('Invalid dimensions')
            scale=min(200/72,4095/max(w,h),math.sqrt(7_950_000/(w*h)))
            with contextlib.closing(page.render(scale=scale)) as bitmap:
                image=bitmap.to_pil().convert('RGB')
                if image.width*image.height>8_000_000:raise ValueError('Image pixel budget')
                image.save(target,format='PNG')
                images.append({'path':str(target),'sha256':hash_file(target),'kind':'full_problem_page',
                               'width':image.width,'height':image.height})
                for crop in job.get('crops',[])[:8]:
                    box=crop['box']
                    if len(box)!=4 or not all(isinstance(v,(float,int)) and math.isfinite(v) for v in box):continue
                    x0,y0,x1,y1=box
                    if not 0<=x0<x1<=1 or not 0<=y0<y1<=1:continue
                    rect=(max(0,int(x0*image.width)-30),max(0,int(y0*image.height)-30),
                          min(image.width,math.ceil(x1*image.width)+30),min(image.height,math.ceil(y1*image.height)+30))
                    path=target.with_name('line-'+str(int(crop['index']))+'.png')
                    if path.exists():raise ValueError('Crop exists')
                    image.crop(rect).save(path,format='PNG')
                    images.append({'path':str(path),'sha256':hash_file(path),'kind':'low_confidence_context',
                                   'line_index':crop['index'],'page_pixel_box':list(rect)})
    if hash_file(source)!=job['expected_sha256']:raise ValueError('Source changed during rendering')
    return {'images':images,'ocr_performed':False,'network_used':False}


if __name__=='__main__':
    def blocked(*args,**kwargs):raise RuntimeError('Network disabled in local render worker')
    socket.create_connection=blocked;socket.socket.connect=blocked;socket.getaddrinfo=blocked
    try:
        job=json.loads(Path(sys.argv[1]).read_text(encoding='utf-8'))
        print(json.dumps(render(job),ensure_ascii=True))
    except Exception:
        print(json.dumps({'status':'render_failed'}));sys.exit(2)
