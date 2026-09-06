"""Explicit isolated E2E entry point; never use this to launch the user workspace."""
import os,sys,wave
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
assert os.environ.get('INSIGHTCUT_FAKE_PROVIDERS')=='1'
root=Path(os.environ['INSIGHTCUT_DATA_ROOT']).resolve()
assert Path(os.environ['INSIGHTCUT_DB_PATH']).resolve().is_relative_to(root)
root.mkdir(parents=True,exist_ok=True);os.chdir(root)
os.environ['INSIGHTCUT_SKIP_DOTENV']='1'
import requests
requests.sessions.Session.request=lambda *a,**k: (_ for _ in ()).throw(AssertionError('Provider network is disabled in E2E'))
from PIL import Image
from src.media.image_generator import ImageGenerator
from src.draft.voiceover import VoiceOverGenerator
from src.config import Config
from src.utils import local_uploader
local_uploader.LOCAL_MEDIA_DIR=root/'data/media'
def image(self,prompt,index=0,filename=None,**kw):
 p=self.output_dir/((filename or f'segment_{index:03d}')+'.png');p.parent.mkdir(parents=True,exist_ok=True)
 Image.new('RGB',(96,64),(80,120,155)).save(p);return str(p)
def voice(self,text,filename=None,**kw):
 p=self.output_dir/((filename or 'voice')+'.wav');p.parent.mkdir(parents=True,exist_ok=True)
 with wave.open(str(p),'wb') as w:w.setnchannels(1);w.setsampwidth(2);w.setframerate(24000);w.writeframes(b'\0\0'*12000)
 return str(p)
ImageGenerator.generate=image;VoiceOverGenerator.generate=voice
Config.save_model_config({'image':{'api_key':'local-fixture'},'tts':{'provider':'mimo','mimo':{'api_key':'local-fixture'}}})
from api_server import app
import uvicorn
uvicorn.run(app,host='127.0.0.1',port=2002)
