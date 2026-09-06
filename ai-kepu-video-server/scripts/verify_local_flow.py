"""Isolated local production/export smoke test. No paid providers or existing data."""
import os, json, time, io, wave, shutil, subprocess
from pathlib import Path
import sys, tempfile
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
ROOT=Path(tempfile.mkdtemp(prefix='insightcut-full-flow-'))
os.environ['INSIGHTCUT_SKIP_DOTENV']='1'
os.environ['INSIGHTCUT_DATA_ROOT']=str(ROOT)
os.environ['INSIGHTCUT_DB_PATH']=str(ROOT/'data/flow.db')
os.environ['INSIGHTCUT_FAKE_PROVIDERS']='1'
os.chdir(ROOT)
import requests
# All network adapter requests are denied. Fake providers below create only local fixtures.
def blocked(*a,**kw): raise AssertionError('EXTERNAL_NETWORK_NOT_ALLOWED')
requests.sessions.Session.request=blocked
from PIL import Image
from src.media.image_generator import ImageGenerator
from src.draft.voiceover import VoiceOverGenerator
from src.utils import local_uploader
local_uploader.LOCAL_MEDIA_DIR=ROOT/'data/media'
def image(self,prompt,index=0,filename=None,**kw):
 p=self.output_dir/((filename or f'segment_{index:03d}')+'.png');p.parent.mkdir(parents=True,exist_ok=True)
 Image.new('RGB',(96,64),(80,120,155)).save(p);return str(p)
def voice(self,text,filename=None,**kw):
 p=self.output_dir/((filename or 'voice')+'.wav');p.parent.mkdir(parents=True,exist_ok=True)
 with wave.open(str(p),'wb') as w:w.setnchannels(1);w.setsampwidth(2);w.setframerate(24000);w.writeframes(b'\0\0'*12000)
 return str(p)
ImageGenerator.generate=image; VoiceOverGenerator.generate=voice
from fastapi.testclient import TestClient
from src.api.task_runtime import task_runtime
from src.database import db_client
from src.config import Config
Config.save_model_config({'image': {'api_key':'local-fixture'}, 'tts': {'mimo': {'api_key':'local-fixture'}}})
import api_server
c=TestClient(api_server.app)
B='/ai/native/video/kepu'
def req(method,path,**kw):
 r=getattr(c,method)(B+path,**kw)
 if r.status_code>=400:raise AssertionError((path,r.status_code,r.text[:400]))
 return r

def wait_task(tid):
 deadline=time.monotonic()+30
 while task_runtime.is_running(tid) and time.monotonic()<deadline:time.sleep(.05)
 assert not task_runtime.is_running(tid)
 return req('get',f'/tasks/{tid}/workspace').json()

tid=req('post','/tasks',json={'theme':'这是第一句。\n这是第二句。','name':'隔离完整链路','execution_mode':'review_first','script_policy':'verbatim','voice_type':'mimo:冰糖'}).json()['task_id']
w=wait_task(tid);print('PHASE planning',w['stage'],len(w['segments']),flush=True)
assert w['stage']=='awaiting_confirmation'
req('patch',f'/tasks/{tid}/settings',json={'voice_type':'mimo:冰糖','voice_confirmed':True,'subtitle_options':{'size':'large','position':'high','outline':'strong'},'expected_plan_version':w['plan_version']})
w=req('get',f'/tasks/{tid}/workspace').json()
flow=req('post',f'/tasks/{tid}/confirm-production',json={'snapshot_key':w['snapshot_key'],'plan_version':w['plan_version']}).json()
from src.api.production_flow import scheduler
end=time.monotonic()+120
while time.monotonic()<end:
 scheduler.tick()
 f=db_client.production_list(task_id=tid)[-1]
 if f['state'] in ('completed','failed','cancelled','stale'):break
 time.sleep(.05)
print('AUTO PRODUCTION',f,flush=True)
assert f['state']=='completed', f
w=req('get',f'/tasks/{tid}/workspace').json()
assert w['health']['assets_complete']
assert not list(ROOT.rglob('draft_info.json')), 'MP4 production built an unsolicited draft'
for fmt in ['srt','vtt']:
 r=req('get',f'/tasks/{tid}/subtitle.{fmt}');(ROOT/f'subtitle.{fmt}').write_bytes(r.content)
 assert ('00:00:01,000' if fmt=='srt' else '00:00:01.000') in r.text, r.text
local_results=[]
for target in ['materials','mp4','draft','draft_local','draft_local','draft_local']:
 payload={'target':target,'open_output_directory':False}
 if target=='draft_local':payload.update(draft_root=str(ROOT/'other disk/Jianying'),target_os='mac',collision_policy='backup_replace' if len(local_results)==2 else 'copy')
 j=req('post',f'/tasks/{tid}/exports',json=payload).json();deadline=time.monotonic()+90
 while j['status'] in ['pending','processing'] and time.monotonic()<deadline:
  time.sleep(.1);j=req('get',f"/tasks/{tid}/exports/{j['job_id']}").json()
 print('EXPORT',target,j['status'],j.get('error'),flush=True)
 if j['status']=='completed':print('OUTPUT',json.dumps(j['result'],ensure_ascii=False),flush=True)
 assert j['status']=='completed', j
 if target=='draft_local':local_results.append(j['result'])
 if target=='mp4':
  actual=json.loads(subprocess.check_output(['ffprobe','-v','error','-show_format','-show_streams','-of','json',j['result']['video_path']]))
  assert float(actual['format']['duration'])>0
  assert {st['codec_type'] for st in actual['streams']} >= {'video','audio'}
assert Path(local_results[1]['draft_path']).name.endswith('（2）')
assert Path(local_results[2]['backup_path']).is_dir()
for result in local_results:
 draft_root=Path(result['draft_path'])
 content=json.loads((draft_root/'draft_info.json').read_text())
 text=json.loads(content['materials']['texts'][0]['content'])
 assert abs(text['styles'][0]['size']-8.12)<.001
 track=next(track for track in content['tracks'] if track['type']=='text')
 assert abs(track['segments'][0]['clip']['transform']['y'] + .71)<.001
 for group in ('videos','audios'):
  for material in content['materials'][group]:
   material_path=Path(material['path'])
   assert material_path.is_file() and material_path.is_relative_to(draft_root)

asset_zip=req('get',f'/tasks/{tid}/assets/download')
import zipfile
assert zipfile.ZipFile(io.BytesIO(asset_zip.content)).testzip() is None
state=req('get',f'/tasks/{tid}/export-state').json();print('FINAL OUTPUTS',json.dumps(state['outputs'],ensure_ascii=False),flush=True)
assert state['outputs']['mp4']['available'], 'Rebuilding a draft lost the existing rendered MP4'
assert state['outputs']['materials']['complete']
print('TASK_ID',tid,flush=True)
(ROOT/'flow-result.json').write_text(json.dumps({'task_id':tid,'workspace':w,'export_state':state},ensure_ascii=False,indent=2))

print("VERIFIED_RUNTIME", ROOT, flush=True)
