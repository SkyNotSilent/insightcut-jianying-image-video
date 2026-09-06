import json
from pathlib import Path

import pytest

from src.database import sqlite_client as database_module
from src.database.sqlite_client import SQLiteClient


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(database_module, "DB_PATH", tmp_path / "local.db")
    client = SQLiteClient()
    client.create_task("editing", "Original text", "style", 100, execution_mode="review_first")
    client.save_task_checkpoint("editing", script_text="Original text")
    client.save_segments("editing", [{"segment_index": 0, "text": "Original text", "image_prompt": "saved prompt", "image_path": "old.png"}])
    return client


def test_edit_updates_current_script_and_preserves_original(db):
    assert db.update_segment_plan("editing", 0, {"text": "Edited words. Keep spaces!"}, 0) == 1
    task = db.get_task("editing")
    assert task["script_text"] == "Edited words. Keep spaces!"
    assert task["original_script_text"] == "Original text"


def test_resegment_snapshot_can_be_restored_without_losing_asset_pointers(db):
    version = db.replace_plan_segments("editing", "New", [{"segment_index": 0, "text": "New"}], 0)
    assert version == 1
    assert db.restore_plan_revision("editing", expected_plan_version=0) == -1
    assert db.restore_plan_revision("editing", expected_plan_version=1) == 2
    segment = db.get_segments("editing")[0]
    assert segment["text"] == "Original text"
    assert segment["image_prompt"] == "saved prompt"
    assert segment["image_path"] == "old.png"


def test_draft_publisher_keeps_old_content_and_defaults_to_copy(tmp_path):
    from src.export.draft_publication import publish_draft
    source = tmp_path / "source" / "video"
    source.mkdir(parents=True)
    (source / "draft_content.json").write_text("new")
    root = tmp_path / "arbitrary disk"
    old = root / "video"
    old.mkdir(parents=True)
    (old / "draft_content.json").write_text("manual edit")
    result = publish_draft(source, root, prepare=lambda *_: None, verify=lambda _: None)
    assert Path(result["draft_path"]).name == "video（2）"
    assert (old / "draft_content.json").read_text() == "manual edit"
    with pytest.raises(RuntimeError):
        publish_draft(source, root, policy="backup_replace", prepare=lambda *_: None,
                      verify=lambda _: (_ for _ in ()).throw(RuntimeError("preflight failed")))
    assert (old / "draft_content.json").read_text() == "manual edit"


def test_draft_replacement_retains_backup(tmp_path):
    from src.export.draft_publication import publish_draft
    source = tmp_path / "source" / "video"
    source.mkdir(parents=True)
    (source / "content").write_text("new")
    root = tmp_path / "destination"
    (root / "video").mkdir(parents=True)
    (root / "video" / "content").write_text("manual")
    result = publish_draft(source, root, policy="backup_replace", prepare=lambda *_: None, verify=lambda _: None)
    assert (Path(result["backup_path"]) / "content").read_text() == "manual"
    assert (Path(result["draft_path"]) / "content").read_text() == "new"


def test_media_validation_accepts_external_images_but_rejects_disguised_text(tmp_path):
    from PIL import Image
    from src.utils.media_validation import validate_asset_file
    image = tmp_path / "outside-project.png"
    Image.new("RGB", (10, 10)).save(image)
    assert validate_asset_file(image, "image")["format"] == "PNG"
    image.write_text("not an image")
    with pytest.raises(ValueError):
        validate_asset_file(image, "image")


def test_audio_duration_follows_selected_asset(db, tmp_path):
    import wave
    from src.utils.media_validation import validate_asset_file
    path = tmp_path / "long.wav"
    with wave.open(str(path), "wb") as audio:
        audio.setparams((1, 2, 24000, 0, "NONE", "not compressed"))
        audio.writeframes(b"\0\0" * 72000)
    asset = db.save_task_asset("editing", "audio", "generated", path=str(path), segment_index=0)
    asset.update(validate_asset_file(path, "audio"))
    asset["tts_options"] = {"speed_level": "slow"}
    assert db.select_segment_asset("editing", 0, asset, "audio")
    segment = db.get_segments("editing")[0]
    assert segment["duration"] == pytest.approx(3)
    assert json.loads(segment["audio_tts_options_json"])["speed_level"] == "slow"


def test_export_registry_blocks_new_generation_until_export_finishes():
    from src.api.task_runtime import TaskRuntimeRegistry
    registry = TaskRuntimeRegistry()
    assert registry.register_export("task", "export")
    assert registry.begin("task") is None
    assert registry.start_export("task", "export")
    registry.claim_delete("task")
    registry.request_cancel("task")
    assert registry.export_cancelled("task", "export")
    assert not registry.wait_until_stopped("task", 0)
    registry.finish_export("task", "export", active=True)
    assert registry.wait_until_stopped("task", 0)


def test_maintenance_protects_draft_contents_clone_and_missing_database(db, tmp_path, monkeypatch):
    import importlib.util
    script = Path(__file__).parents[1] / "scripts" / "maintenance_report.py"
    spec = importlib.util.spec_from_file_location("maintenance_test", script)
    maintenance = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(maintenance)
    monkeypatch.setattr(maintenance, "SERVER_ROOT", tmp_path)
    monkeypatch.setattr(maintenance, "DB_PATH", tmp_path / "local.db")
    monkeypatch.setattr(maintenance, "OUTPUT_DIR", tmp_path / "output")
    monkeypatch.setattr(maintenance, "MEDIA_DIR", tmp_path / "data" / "media")
    draft = tmp_path / "output" / "editing"
    draft.mkdir(parents=True)
    content = draft / "draft_content.json"
    content.write_text('{}')
    db.save_task_result("editing", str(draft), 1)
    reference = tmp_path / "data" / "media" / "_voice_clones" / "clone" / "reference.wav"
    reference.parent.mkdir(parents=True)
    reference.write_bytes(b"preserved reference")
    with db._get_conn() as conn:
        conn.execute("INSERT INTO tts_voice_clones(clone_id,name,reference_path,status,consent_confirmed) VALUES('clone','voice',?,'draft',1)", (str(reference),))
    unused = draft.parent / "unused.png"
    unused.write_bytes(b"orphan")
    candidates = maintenance.unreferenced_media_files(maintenance.collect_referenced_paths())
    assert candidates == [unused]
    monkeypatch.setattr(maintenance, "DB_PATH", tmp_path / "missing.db")
    with pytest.raises((RuntimeError, FileNotFoundError)):
        maintenance.collect_referenced_paths()


def test_catalog_searches_all_pages_with_stable_order_and_counts(db):
    for i in range(85):
        db.create_task(f'project-{i:03}', f'Theme {i:03}', '文稿|写实', 100, voice_type='mimo:冰糖')
    first = db.project_catalog(page=1)
    second = db.project_catalog(page=2)
    assert first['total'] == first['counts']['all'] == 86
    assert len(first['items']) == len(second['items']) == 40
    assert not {v['task_id'] for v in first['items']} & {v['task_id'] for v in second['items']}
    found = db.project_catalog(q='Theme 084')
    assert found['total'] == 1 and found['items'][0]['task_id'] == 'project-084'
    assert db.project_catalog(q='冰糖', style='写实')['total'] == 85


def test_config_conflict_and_failed_publish_keep_previous_file(tmp_path, monkeypatch):
    from src.config import Config, ConfigConflict
    import src.config as config_module
    monkeypatch.setattr(Config, 'CONFIG_FILE', tmp_path/'settings.json')
    monkeypatch.setattr(Config, 'LEGACY_CONFIG_FILE', tmp_path/'absent.json')
    first = Config.save_model_config({'llm': {'model': 'openai/test'}})
    old = Config.CONFIG_FILE.read_bytes()
    with pytest.raises(ConfigConflict):
        Config.save_model_config({'revision': 'obsolete', 'llm': {'model': 'stale'}})
    def fail(*args):
        raise OSError('disk full')
    monkeypatch.setattr(config_module.os, 'replace', fail)
    with pytest.raises(OSError):
        Config.save_model_config({'revision': first['revision'], 'llm': {'model': 'new'}})
    assert Config.CONFIG_FILE.read_bytes() == old


def test_recovery_rolls_back_interrupted_draft_switch(tmp_path):
    import os
    from src.export.draft_publication import _write_journal, _recover
    root = tmp_path.resolve()
    old = root/'film'; old.mkdir(); (old/'manual').write_text('edit')
    backup = root/'.insightcut/backups/old'; backup.parent.mkdir(parents=True)
    staging = root/'.insightcut/staging/new'; staging.mkdir(parents=True)
    (staging/'new').write_text('unverified')
    journal = root/'.insightcut/publications/switch.json'; journal.parent.mkdir(parents=True)
    _write_journal(journal, {'target':str(old),'backup':str(backup),'staging':str(staging),'phase':'publishing'})
    os.replace(old, backup); os.replace(staging, old)
    _recover(root)
    assert (old/'manual').read_text() == 'edit'
    assert not journal.exists()
    _recover(root)  # idempotent after a second restart


def test_cancel_during_draft_switch_restores_previous(tmp_path):
    from src.export.draft_publication import publish_draft
    source=tmp_path/'source/film'; source.mkdir(parents=True); (source/'content').write_text('new')
    target=tmp_path/'output/film'; target.mkdir(parents=True); (target/'content').write_text('old')
    calls=[]
    def check():
        calls.append(True)
        if len(calls) == 4: raise RuntimeError('cancelled after publishing')
    with pytest.raises(RuntimeError):
        publish_draft(source,target.parent,policy='backup_replace',prepare=lambda *_:None,verify=lambda _:None,should_cancel=check)
    assert (target/'content').read_text() == 'old'


def test_maintenance_lock_rejects_apply_while_service_owns_lock(tmp_path):
    from src.utils.file_lock import file_lock
    with file_lock(tmp_path/'maintenance.lock'):
        with pytest.raises(OSError):
            with file_lock(tmp_path/'maintenance.lock', blocking=False):
                pytest.fail('A concurrent cleanup acquired the service lock')


def test_queued_cancellation_does_not_wait_for_busy_workers():
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    from src.api.task_runtime import TaskCancellation
    release=Event(); done=[]
    with ThreadPoolExecutor(max_workers=1) as pool:
        busy=pool.submit(release.wait, 2)
        queued=pool.submit(lambda: pytest.fail('cancelled work executed'))
        token=TaskCancellation(); token.bind_future(queued, lambda: done.append(True))
        try:
            token.cancel()
            assert queued.cancelled() and done == [True]
        finally:
            release.set(); busy.result()


def test_hung_external_process_is_terminated_on_cancel_and_timeout():
    import sys, time, subprocess
    from src.export.ffmpeg_exporter import FFmpegExporter, RenderCancelled
    exporter=object.__new__(FFmpegExporter)
    start=time.monotonic()
    exporter._should_cancel=lambda: time.monotonic()-start>.1
    with pytest.raises(RenderCancelled):
        exporter._run_command([sys.executable,'-c','import time;time.sleep(60)'],capture_output=True,text=True,timeout=10)
    assert time.monotonic()-start < 2
    exporter._should_cancel=lambda: False
    with pytest.raises(subprocess.TimeoutExpired):
        exporter._run_command([sys.executable,'-c','import time;time.sleep(60)'],capture_output=True,text=True,timeout=.1)


def test_legacy_recovery_preserves_current_metadata_and_asset_selection(db, tmp_path):
    import importlib.util
    spec=importlib.util.spec_from_file_location('recovery',Path(__file__).parents[1]/'scripts/recover_failed_task.py')
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    root=tmp_path/'output'/'Original text';(root/'images').mkdir(parents=True)
    (root/'images/segment_000.png').write_bytes(b'local recovered fixture')
    (root/'images/segment_000_new-version.png').write_bytes(b'another version of same segment')
    log=tmp_path/'task.log';log.write_text('editing 段落 0 文案: outdated\n')
    db.update_segment('editing',0,{'selected_image_asset_id':'current-id','audio_tts_options_json':'{"speed_level":"fast"}'})
    assert module.recover_task_data('editing',tmp_path/'local.db',tmp_path/'output',log)
    assert len(db.get_segments('editing')) == 1
    segment=db.get_segments('editing')[0]
    assert segment['text']=='Original text'
    assert segment['selected_image_asset_id']=='current-id'
    assert segment['audio_tts_options_json']=='{"speed_level":"fast"}'


def test_snapshot_migration_is_repeatable_and_creates_consistent_backup(tmp_path, monkeypatch):
    import sqlite3
    path=tmp_path/'legacy.db'
    with sqlite3.connect(path) as connection:
        connection.execute('CREATE TABLE evidence(value TEXT)')
        connection.execute("INSERT INTO evidence VALUES('kept')")
    monkeypatch.setattr(database_module,'DB_PATH',path)
    first=SQLiteClient(); first._init_db()
    backup=path.with_name(path.name+'.before-batch-video.bak')
    with sqlite3.connect(backup) as connection:
        assert connection.execute('SELECT value FROM evidence').fetchone()[0]=='kept'
    original=backup.read_bytes()
    second=SQLiteClient(); second._init_db()
    assert backup.read_bytes()==original
    with second._get_conn() as connection:
        assert connection.execute("SELECT COUNT(*) FROM schema_migrations WHERE version='20260906_reliability'").fetchone()[0]==1


def test_shutdown_keeps_writers_registered_until_they_stop():
    from src.api.task_runtime import TaskRuntimeRegistry
    runtime = TaskRuntimeRegistry()
    token = runtime.begin('writer')
    runtime.register_export('queued', 'job')
    runtime.close()
    assert token.is_cancelled()
    assert runtime.export_cancelled('queued', 'job')
    assert runtime.begin('new') is None
    assert runtime.register_export('new', 'new-job') is None
    assert not runtime.wait_idle(.01)
    runtime.finish('writer', token)
    runtime.finish_export('queued', 'job')
    assert runtime.wait_idle(.01)
    runtime.open()
    assert runtime.begin('after-restart') is not None


def test_maintenance_apply_only_removes_temporary_unreferenced_files(db, tmp_path):
    import os, subprocess, sys
    draft = tmp_path/'output/film'
    draft.mkdir(parents=True)
    (draft/'draft_content.json').write_text('{}')
    db.save_task_result('editing', str(draft), 1)
    garbage = tmp_path/'output/orphan.png'; garbage.write_bytes(b'orphan')
    outside = tmp_path/'external'; outside.mkdir(); (outside/'keep').write_text('user file')
    (tmp_path/'output/link').symlink_to(outside, target_is_directory=True)
    env = {**os.environ, 'INSIGHTCUT_SERVER_ROOT':str(tmp_path), 'INSIGHTCUT_DB_PATH':str(tmp_path/'local.db')}
    script = Path(__file__).parents[1]/'scripts/maintenance_report.py'
    result = subprocess.run([sys.executable, str(script), '--apply'], env=env, capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report['deleted_count'] == 1 and report['failed_count'] == 0
    assert not garbage.exists()
    assert (draft/'draft_content.json').is_file()
    assert (outside/'keep').read_text() == 'user file'


def test_legacy_pipeline_runs_get_independent_work_directories(tmp_path):
    from types import SimpleNamespace
    from src.core.pipeline import VideoEditorPipeline
    pipeline = object.__new__(VideoEditorPipeline)
    pipeline._run_root = tmp_path
    pipeline.image_generator = SimpleNamespace()
    pipeline.voiceover_generator = SimpleNamespace()
    pipeline.generate_script = lambda **kwargs: None
    def build():
        path = pipeline.image_generator.output_dir/'same.png'
        path.write_text('original')
        return path
    pipeline.build_from_script = build
    first, second = pipeline.run(), pipeline.run()
    assert first != second and first.read_text() == second.read_text() == 'original'


def test_multipart_unknown_length_is_bounded_while_receiving():
    import asyncio
    from fastapi import HTTPException
    from src.api.upload_limits import UploadBodyLimit
    received = []
    async def parser(scope, receive, send):
        while True:
            received.append(await receive())
    async def receive():
        return {'type':'http.request', 'body':b'x' * (1024 * 1024), 'more_body':True}
    async def send(message):
        pass
    scope = {'type':'http','method':'POST','path':'/upload-image', 'headers':[(b'content-type',b'multipart/form-data; boundary=test')]}
    with pytest.raises(HTTPException) as error:
        asyncio.run(UploadBodyLimit(parser)(scope, receive, send))
    assert error.value.status_code == 413
    assert len(received) == 21


def test_image_byte_and_pixel_limits(tmp_path):
    import io, struct, zlib
    from src.utils.media_validation import validate_image, MAX_IMAGE_BYTES
    oversized = tmp_path/'oversized.png'
    with oversized.open('wb') as stream:
        stream.truncate(MAX_IMAGE_BYTES + 1)
    with oversized.open('rb') as stream, pytest.raises(ValueError, match='20 MiB'):
        validate_image(stream)
    # A real PNG header advertises 64,008,000 pixels; reject before allocating pixels.
    ihdr = struct.pack('>IIBBBBB', 8001, 8000, 8, 2, 0, 0, 0)
    chunk = b'IHDR' + ihdr
    header = b'\x89PNG\r\n\x1a\n' + struct.pack('>I',len(ihdr)) + chunk + struct.pack('>I',zlib.crc32(chunk))
    data = b'IDAT' + zlib.compress(b'\0')
    encoded = header + struct.pack('>I', len(data)-4) + data + struct.pack('>I', zlib.crc32(data))
    with pytest.raises(ValueError, match='6400'):
        validate_image(io.BytesIO(encoded))


@pytest.mark.parametrize('target_os,root,separator', [
    ('windows', r'E:\User videos\剪映', '\\'),
    ('windows', r'\\media-host\share\剪映', '\\'),
    ('mac', '/Volumes/Other disk/剪映', '/'),
])
def test_export_paths_keep_arbitrary_drives_unicode_and_copy_names(target_os, root, separator):
    from src.utils.path_fixer import apply_extract_path, apply_meta_info, validate_extract_path
    valid, normalized, issues = validate_extract_path(root, target_os)
    assert valid, issues
    draft = {'materials': {'videos':[{'path':'images/unique.png'}], 'audios':[{'path':'voiceovers/unique.wav'}]}}
    result = apply_extract_path(draft, normalized, '项目（2）', target_os=target_os, force=True)
    prefix = normalized + separator + '项目（2）' + separator
    assert result['materials']['videos'][0]['path'] == prefix + 'images' + separator + 'unique.png'
    assert result['materials']['audios'][0]['path'] == prefix + 'voiceovers' + separator + 'unique.wav'
    assert apply_meta_info({}, normalized, '项目（2）', target_os)['draft_fold_path'] == prefix.rstrip(separator)


def test_status_queries_do_not_queue_behind_busy_file_workers():
    import asyncio
    from threading import Barrier, Event
    from src.api.work_limits import io_pool, run_query
    ready, release = Barrier(5), Event()
    def slow_file_operation():
        ready.wait(timeout=2)
        release.wait(timeout=3)
    running = [io_pool.submit(slow_file_operation) for _ in range(4)]
    try:
        ready.wait(timeout=2)
        async def query():
            return await asyncio.wait_for(run_query(lambda: 'status available'), timeout=.5)
        assert asyncio.run(query()) == 'status available'
    finally:
        release.set()
        for future in running:
            future.result(timeout=2)
