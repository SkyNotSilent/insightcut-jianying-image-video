import io, json, time, asyncio, zipfile, threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import pytest
from fastapi.testclient import TestClient
import api_server
from src.api import routes
from src.api import task_manager as manager_module
from src.database import sqlite_client as sqlmodule
from src.database.sqlite_client import SQLiteClient
from src.api.task_manager import TaskManager
from src.config import Config
from src.utils import local_uploader
from PIL import Image

@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(Config, 'BASE_DIR', tmp_path)
    monkeypatch.setattr(Config, 'CONFIG_FILE', tmp_path/'config.json')
    monkeypatch.setattr(Config, 'LEGACY_CONFIG_FILE', tmp_path/'no-config.json')
    monkeypatch.setattr(sqlmodule, 'DB_PATH', tmp_path/'db.sqlite')
    monkeypatch.setattr(local_uploader, 'LOCAL_MEDIA_DIR', tmp_path/'data/media')
    db=SQLiteClient()
    manager=TaskManager()
    monkeypatch.setattr(routes, 'mysql_client', db)
    monkeypatch.setattr(manager_module, 'db_client', db)
    monkeypatch.setattr(routes, 'task_manager', manager)
    monkeypatch.setattr(api_server, 'output_dir', tmp_path/'output')
    monkeypatch.setattr(api_server, 'legacy_media_dir', tmp_path/'data/media')
    task=manager.create_task(theme='audit', style='知识科普|电影质感', length=100, execution_mode='review_first')
    db.save_segments(task,[{'segment_index':0,'text':'测试原文','image_prompt':'test prompt','image_status':'pending','audio_status':'pending'}])
    db.update_task_workflow(task,'awaiting_confirmation',status='awaiting_confirmation',current_step='awaiting_confirmation')
    manager.invalidate_task_cache(task)
    from src.api.task_runtime import TaskRuntimeRegistry
    from src.api import task_executor as executor_module
    runtime = TaskRuntimeRegistry()
    monkeypatch.setattr(routes, "task_runtime", runtime)
    monkeypatch.setattr(manager_module, "task_runtime", runtime)
    monkeypatch.setattr(executor_module, "task_runtime", runtime)
    monkeypatch.setattr(routes, "EXPORT_JOBS", {})
    return tmp_path, db, manager, task, TestClient(api_server.app,raise_server_exceptions=False)

def png(color):
    buf=io.BytesIO(); Image.new('RGB',(8,8),color).save(buf,format='PNG'); return buf.getvalue()

BASE='/ai/native/video/kepu'

def test_download_rejects_non_media_and_accepts_registered_external_image(env):
    root,db,manager,tid,c=env
    marker=root/'outside-storage-marker.txt'; marker.write_text('AUDIT_ONLY_MARKER')
    r=c.put(f'{BASE}/tasks/{tid}/segments/0',json={'image_path':str(marker)})
    assert r.status_code == 400, r.text
    assert not db.get_segments(tid)[0]['image_path']
    db.save_task_asset(tid, 'image', 'generated', path=str(marker), segment_index=0)
    r=c.get(f'{BASE}/tasks/{tid}/assets/download?type=image')
    assert r.status_code == 404, r.text
    image=root/'outside-storage.png'; image.write_bytes(png('red'))
    r=c.put(f'{BASE}/tasks/{tid}/segments/0',json={'image_path':str(image)})
    assert r.status_code == 200, r.text
    r=c.get(f'{BASE}/tasks/{tid}/assets/download?type=image')
    assert r.status_code == 200, r.text
    z=zipfile.ZipFile(io.BytesIO(r.content))
    assert png('red') in [z.read(n) for n in z.namelist()]
    assert '未包含素材说明.json' in z.namelist()

def test_non_image_upload_rejected(env):
    root,db,manager,tid,c=env
    r=c.post(f'{BASE}/tasks/{tid}/segments/0/upload-image',files={'file':('bad.png',b'not an image','image/png')})
    assert r.status_code==400,r.text
    assert db.get_segments(tid)[0]['image_status']=='pending'

def test_same_second_upload_preserves_history(env):
    root,db,manager,tid,c=env
    with patch('time.time',return_value=1788620000):
        a=c.post(f'{BASE}/tasks/{tid}/segments/0/upload-image',files={'file':('a.png',png('red'),'image/png')})
        b=c.post(f'{BASE}/tasks/{tid}/segments/0/upload-image',files={'file':('b.png',png('blue'),'image/png')})
    assert a.status_code==b.status_code==200,(a.text,b.text)
    assert a.json()['image_path']!=b.json()['image_path']
    assert Path(a.json()['image_path']).read_bytes()==png('red')
    assert a.json()['asset_id'] != b.json()['asset_id']

def test_image_import_consistently_waits_for_confirmation(env):
    root,db,manager,tid,c=env
    r=c.post(f'{BASE}/tasks/create-from-images',files={'images':('a.png',png('red'),'image/png')},data={'name':'image import'})
    print('IMAGE IMPORT immediate',r.status_code,r.json())
    if r.status_code==200:
        imported=r.json()['task_id']; follow=c.get(f'{BASE}/tasks/{imported}/workspace')
        print('IMAGE IMPORT after workspace',follow.status_code, follow.json().get('stage'),follow.json().get('status'),db.get_task(imported)['status'])
        assert r.json()['status']==follow.json()['status']==db.get_task(imported)['status']=='awaiting_confirmation'
    else: pytest.fail(r.text)

def test_local_draft_copy_failure_preserves_previous(env,monkeypatch):
    root,db,manager,tid,c=env
    source=root/'source'/'draft1'; source.mkdir(parents=True); (source/'draft_content.json').write_text('{}')
    targetroot=root/'jianying'; target=targetroot/'draft1'; target.mkdir(parents=True); marker=target/'user-edits.txt'; marker.write_text('prior manual edits')
    monkeypatch.setattr(routes,'_validate_local_draft_root',lambda *a:{'valid':True,'path':str(targetroot),'target_os':'mac','warnings':[]})
    monkeypatch.setattr(routes,'_build_editable_draft',lambda *a:source)
    monkeypatch.setattr(routes.shutil,'copytree',lambda *a,**k:(_ for _ in ()).throw(OSError('simulated disk failure')))
    with pytest.raises(OSError): routes._export_draft_local(SimpleNamespace(task_id=tid),[],{'draft_root':str(targetroot)})
    assert marker.read_text() == "prior manual edits"

def test_export_draft_uses_subtitle_snapshot(env,monkeypatch):
    root,db,manager,tid,c=env
    image=root/'output'/'one.png';image.parent.mkdir();image.write_bytes(png('red'))
    manager.set_task_result(tid,str(root/'output'/'draft'),1)
    db.update_task_plan_fields(tid,{'subtitle_options_json':json.dumps({'size':'large','position':'high','outline':'strong'})})
    manager.invalidate_task_cache(tid);task=manager.get_task(tid)
    captured={}
    def factory(**kw):
        captured.update(kw)
        return SimpleNamespace(build=lambda **kw:kw['output_dir'])
    monkeypatch.setattr('src.draft.builder.DraftBuilder',factory)
    routes._build_editable_draft(task,[{'text':'test','image_path':str(image)}])
    assert captured['subtitle_options'] == {'size':'large','position':'high','outline':'strong'}

def test_preview_releases_event_loop(env,monkeypatch):
    root,db,manager,tid,c=env
    monkeypatch.setattr(routes.VoicePreviewService,'generate',lambda *a,**k:(time.sleep(.35) or {'url':'/test'}))
    async def run():
        start=time.perf_counter()
        async def other_request():
            await asyncio.sleep(.02)
            return time.perf_counter()-start
        first=asyncio.create_task(other_request())
        await routes.preview_voice({'voice_type':'mimo:冰糖'})
        return await first
    lag=asyncio.run(run()); assert lag<.15

def test_clone_cannot_bypass_preview(env):
    root,db,manager,tid,c=env
    db.create_voice_clone({'clone_id':'audit-clone','name':'audit','reference_path':str(root/'reference.wav'),'status':'draft','is_enabled':False,'consent_confirmed':True})
    one=c.patch(f'{BASE}/voice-clones/audit-clone',json={'status':'ready'})
    two=c.patch(f'{BASE}/voice-clones/audit-clone',json={'is_enabled':True})
    assert one.status_code==two.status_code==400,(one.text,two.text)
    assert not db.get_voice_clone('audit-clone')['is_enabled']

def test_select_audio_updates_duration_and_subtitles(env):
    import wave
    root,db,manager,tid,c=env
    sound=root/'output'/'three_seconds.wav';sound.parent.mkdir()
    with wave.open(str(sound),'wb') as w:
        w.setnchannels(1);w.setsampwidth(2);w.setframerate(24000);w.writeframes(b'\0\0'*72000)
    db.update_segment(tid,0,{'duration':1.0})
    a=db.save_task_asset(tid,'audio','generated',path=str(sound),segment_index=0,text='测试原文',voice_type='mimo:冰糖')
    r=c.post(f'{BASE}/tasks/{tid}/segments/0/select-asset',json={'asset_type':'audio','asset_id':a['asset_id']})
    assert r.status_code==200,r.text
    assert db.get_segments(tid)[0]['duration']==3.0
    from src.draft.subtitle import SubtitleWriter
    srt=SubtitleWriter().render(db.get_segments(tid))
    assert '00:00:03,000' in srt

def test_resegment_uses_saved_segment_text(env,monkeypatch):
    root,db,manager,tid,c=env
    db.save_task_checkpoint(tid,script_text='这是拆分前的旧正文。',script_source='user_input')
    r=c.put(f'{BASE}/tasks/{tid}/segments/0',json={'text':'这是用户已保存的新正文。'})
    assert r.status_code==200,r.text
    w=c.get(f'{BASE}/tasks/{tid}/workspace').json()
    assert w['segments'][0]['text']=='这是用户已保存的新正文。'
    assert w['script_text']=='这是用户已保存的新正文。'
    monkeypatch.setattr(routes.task_executor,'resume_task',lambda *a:'started')
    r=c.post(f'{BASE}/tasks/{tid}/resegment',json={'script_text':w['script_text'],'expected_plan_version':w['plan_version']})
    assert r.status_code==200,r.text
    assert db.get_segments(tid)[0]['text']=='这是用户已保存的新正文。'

def test_media_content_type_does_not_read_whole_file(env,monkeypatch):
    root,*_=env
    p=root/'large.mp4';p.write_bytes(b'0000ftyp'+b'0'*1024)
    read_sizes=[]; original=Path.read_bytes
    def reader(self):
        data=original(self);read_sizes.append(len(data));return data
    monkeypatch.setattr(Path,'read_bytes',reader)
    api_server._media_type_for_file(p)
    assert read_sizes==[]

def test_task_rejects_whitespace_only_theme(env,monkeypatch):
    root,db,manager,tid,c=env
    monkeypatch.setattr(routes.task_executor,'execute_task',lambda **kw:True)
    r=c.post(f'{BASE}/tasks',json={'theme':'   ','execution_mode':'review_first'})
    assert r.status_code==400,r.text

def test_deletion_cancels_and_waits_for_export(env,monkeypatch):
    root,db,manager,tid,c=env
    output=root/'output'/tid/'draft';output.mkdir(parents=True)
    manager.set_task_result(tid,str(output),1)
    started=threading.Event();release=threading.Event()
    def render(task,segments,use_preview,should_cancel=None):
        started.set();assert release.wait(3)
        assert should_cancel()
        raise routes.ExportJobCancelled()
        p=root/'output'/tid/'recreated.mp4';p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(b'fixture')
        return {'video_path':str(p)}
    monkeypatch.setattr(routes,'_export_mp4',render)
    job=routes._create_export_job(tid,'mp4')
    worker=threading.Thread(target=routes._run_export_job,args=(job['job_id'],'mp4',False))
    worker.start();assert started.wait(3)
    timer=threading.Timer(.1, release.set);timer.start()
    r=c.delete(f'{BASE}/tasks/{tid}?delete_files=true')
    release.set();worker.join(3);timer.join()
    assert r.status_code==202,r.text
    for _ in range(100):
        if not routes.task_runtime.is_deleting(tid): break
        time.sleep(.01)
    assert db.get_task(tid) is None
    assert not (root/'output'/tid/'recreated.mp4').exists()
    assert routes._export_job_snapshot(job['job_id'])['status']=='cancelled'



def test_export_jobs_survive_cache_loss_and_missing_results_are_reported(env):
    root,db,manager,tid,c=env
    path=root/'export.mp4';path.write_bytes(b'previous output')
    job=routes._create_export_job(tid,'mp4')
    routes._update_export_job(job['job_id'],status='completed',result={'video_path':str(path)})
    routes.EXPORT_JOBS.clear()
    assert routes._export_job_snapshot(job['job_id'])['status']=='completed'
    path.unlink()
    r=c.get(f'{BASE}/tasks/{tid}/exports/{job["job_id"]}')
    assert r.status_code==200 and r.json()['status']=='failed'
    assert r.json()['retryable'] is True


def test_oversized_document_and_docx_expansion_rejected(env):
    from fastapi import HTTPException
    root,db,manager,tid,c=env
    zipped=io.BytesIO()
    with zipfile.ZipFile(zipped,'w',zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('word/document.xml',b' '* (40*1024*1024+1))
    with pytest.raises(HTTPException) as error:
        routes._extract_docx_text(zipped.getvalue())
    assert error.value.status_code==413
    r=c.post(f'{BASE}/documents/extract-text',files={'file':('large.txt',b'x'*(20*1024*1024+1),'text/plain')})
    assert r.status_code==413


def test_real_text_pdf_extraction():
    import shutil
    if not shutil.which('pdftotext'):
        pytest.skip('pdftotext unavailable; CI installs poppler-utils')
    # Minimal real PDF with a valid xref table, without a PDF-generation dependency.
    stream=b'BT /F1 12 Tf 20 100 Td (InsightCut PDF regression) Tj ET'
    objects=[b'<< /Type /Catalog /Pages 2 0 R >>',b'<< /Type /Pages /Kids [3 0 R] /Count 1 >>',
             b'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 200] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>',
             b'<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>',b'<< /Length '+str(len(stream)).encode()+b' >>\nstream\n'+stream+b'\nendstream']
    pdf=bytearray(b'%PDF-1.4\n'); offsets=[0]
    for i,obj in enumerate(objects,1):
        offsets.append(len(pdf)); pdf.extend(str(i).encode()+b' 0 obj\n'+obj+b'\nendobj\n')
    start=len(pdf);pdf.extend(b'xref\n0 6\n0000000000 65535 f \n')
    for offset in offsets[1:]:pdf.extend(f'{offset:010} 00000 n \n'.encode())
    pdf.extend(f'trailer\n<< /Root 1 0 R /Size 6 >>\nstartxref\n{start}\n%%EOF'.encode())
    assert 'InsightCut PDF regression' in routes._extract_pdf_text(bytes(pdf))


def test_audio_probe_timeout_keeps_current_selection(env,monkeypatch):
    import subprocess
    from src.utils import media_validation
    root,db,manager,tid,c=env
    db.update_segment(tid,0,{'audio_path':'prior.wav','duration':2})
    audio=root/'hung.wav';audio.write_bytes(b'fixture')
    asset=db.save_task_asset(tid,'audio','generated',path=str(audio),segment_index=0)
    def hung(*args,**kwargs):
        assert kwargs['timeout']==15
        raise subprocess.TimeoutExpired('ffprobe',15)
    monkeypatch.setattr(media_validation.subprocess,'run',hung)
    r=c.post(f'{BASE}/tasks/{tid}/segments/0/select-asset',json={'asset_type':'audio','asset_id':asset['asset_id']})
    assert r.status_code==400
    segment=db.get_segments(tid)[0]
    assert segment['duration']==2 and segment['audio_path']=='prior.wav'


def test_registered_external_media_is_available_in_both_package_types(env):
    from src.export.asset_package import build_material_package
    root,db,manager,tid,c=env
    external=root/'user selected folder/image.png';external.parent.mkdir();external.write_bytes(png('red'))
    assert c.put(f'{BASE}/tasks/{tid}/segments/0',json={'image_path':str(external)}).status_code==200
    package=build_material_package(tid,'External',db.get_segments(tid),root)
    assert package['image_count']==1
    assert c.get(f'{BASE}/tasks/{tid}/assets/download?type=image').status_code==200


def test_planning_readiness_does_not_require_image_or_tts_keys(env,monkeypatch):
    root,db,manager,tid,c=env
    from src.config import Config
    for key in ('SEEDREAM_API_KEY','MIMO_TTS_API_KEY','DOUBAO_TTS_API_KEY','DOUBAO_TTS_APPID','DOUBAO_TTS_TOKEN'):
        monkeypatch.setattr(Config,key,'')
    Config.save_model_config({'llm':{'provider':'openai','model':'openai/gpt-4o','api_key':'local-fixture'}})
    ready=c.get(f'{BASE}/config/readiness?phase=planning').json()
    assert ready['can_continue'] and {item['key'] for item in ready['items']}=={'llm'}
    assert not c.get(f'{BASE}/config/readiness?phase=assets').json()['can_continue']
    monkeypatch.setattr(routes.task_executor,'execute_task',lambda **kw: True)
    response=c.post(f'{BASE}/tasks',json={'theme':'Only planning','execution_mode':'review_first'})
    assert response.status_code==200


def test_rebuilt_draft_keeps_existing_mp4_manifest(env):
    root, db, manager, tid, client = env
    old = root/'old'; old.mkdir()
    new = root/'new'; new.mkdir()
    video = old/'video.mp4'; video.write_bytes(b'previous valid result')
    manager.set_task_result(tid, str(old), 1, video_url='/media/previous.mp4')
    task = manager.get_task(tid)
    manifest = routes._write_preview_manifest(task, video, '/media/previous.mp4', db.get_segments(tid))
    routes._set_task_result_preserving(task, 1, draft_path=new)
    current = manager.get_task(tid)
    assert routes._read_preview_manifest(current) == manifest
    assert routes._preview_state(current, db.get_segments(tid))['valid']
