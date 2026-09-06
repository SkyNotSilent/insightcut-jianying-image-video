import pytest
from src.api.models import CreateBatchRequest
from src.database import sqlite_client as module
from src.database.sqlite_client import SQLiteClient

@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(module, 'DB_PATH', tmp_path / 'local.db')
    c = SQLiteClient()
    c._init_db()
    return c

def test_batch_accepts_full_scripts_and_ten_concurrent():
    r = CreateBatchRequest(input_mode='script', concurrency=10,
        items=[{'content':'第一段。\n\n第二段。' * 20}, {'content':'另一篇。'}])
    assert r.input_mode == 'script'
    assert r.items[0].content.startswith('第一段。\n\n')
    assert CreateBatchRequest(items=[{'theme':'甲'},{'theme':'乙'}]).concurrency == 3

def test_archive_is_persistent_and_preserves_batch_items(db):
    items = [{'item_id':str(i),'theme':str(i),'normalized_theme':str(i)} for i in range(2)]
    before = db.create_batch('archive-test', items, {}, 3)
    archived = db.set_batch_archived('archive-test', True)
    assert archived['archived_at']
    assert archived['items'] == before['items']
    assert archived['status'] == before['status']
    assert db.list_batches(archived=False) == []
    assert len(db.list_batches(archived=True)) == 1
    reopened = SQLiteClient()
    assert reopened.get_batch('archive-test')['archived_at'] == archived['archived_at']
    assert reopened.set_batch_archived('archive-test', False)['archived_at'] is None
    assert len(reopened.list_batches(archived=False)) == 1
    assert reopened.list_batches(archived=True) == []
    assert reopened.get_batch('archive-test')['items'] == before['items']
    assert reopened.set_batch_archived('missing', True) == {}

def test_create_persists_mode_before_first_generation(db):
    assert db.create_task('theme', '主题', 'style', 0, input_mode='theme')
    task = db.get_task('theme')
    assert task['input_mode'] == 'theme'
    assert task['input_mode_known'] == 1
    assert task['original_input'] == '主题'

def test_migration_preserves_batches_and_allows_ten(db):
    items = [{'item_id':str(i),'theme':str(i),'normalized_theme':str(i)} for i in range(12)]
    db.create_batch('ten', items, {}, 10)
    assert len([db.claim_next_batch_item(global_concurrency=10) for _ in range(10)]) == 10
    assert db.claim_next_batch_item(global_concurrency=10) == {}
    assert db.get_batch('ten')['total_count'] == 12

def test_template_zero_length_round_trip(db):
    t = db.create_production_template({'name':'自动','length':0})
    assert t['length'] == 0
    assert db.update_production_template(t['template_id'], {'length':150})['length'] == 150

def ready_task(db, tid='task'):
    db.create_task(tid, '原文', '知识科普|电影质感', 0, input_mode='script')
    db.save_segments(tid,[{'segment_index':0,'text':'原文','image_prompt':'a scene','prompt_status':'completed'}])
    db.update_task_workflow(tid,'awaiting_confirmation',status='awaiting_confirmation')
    return db.get_task(tid)

def test_approval_is_idempotent_and_conflicting_versions_are_rejected(db):
    row=ready_task(db)
    first,created=db.production_approve('task','same',row['plan_version'])
    second,created_again=db.production_approve('task','same',row['plan_version'])
    assert created and not created_again and first['flow_id']==second['flow_id']
    with pytest.raises(ValueError):db.production_approve('task','other',row['plan_version'])
    with pytest.raises(ValueError):db.production_approve('task','same',row['plan_version']+1)

def test_cancellation_cannot_be_overwritten_by_late_stage_update(db):
    row=ready_task(db);f,_=db.production_approve('task','key',row['plan_version'])
    db.production_update(f['flow_id'],cancel_requested=1,state='cancelling')
    db.production_update(f['flow_id'],state='completed')
    assert db.production_list(task_id='task')[-1]['state']=='cancelling'
    assert not db.production_claim_assets(f['flow_id'])

def test_restart_preserves_queued_but_does_not_repeat_external_generation(db):
    for tid in ['queued','running','rendering']:
        row=ready_task(db,tid);f,_=db.production_approve(tid,tid,row['plan_version'])
        if tid=='running':db.production_claim_assets(f['flow_id'])
        if tid=='rendering':db.production_update(f['flow_id'],state='rendering',job_id='persisted-render')
    db.production_recover()
    states={f['task_id']:f['state'] for f in db.production_list()}
    assert states=={'queued':'queued','running':'failed','rendering':'rendering'}
    # rendering reconnects to its persisted export: pending resumes, processing becomes failed.
    assert db.get_segments('running')[0]['text']=='原文'

def test_planning_and_assets_share_the_same_batch_allowance(db):
    db.create_batch('shared',[{'item_id':str(i),'theme':str(i),'normalized_theme':str(i)} for i in range(3)],{},1)
    planning=db.claim_next_batch_item(global_concurrency=10)
    row=ready_task(db)
    with db.get_connection() as c:
        c.execute("UPDATE task_batch_items SET task_id='task',status='awaiting_confirmation' WHERE item_id='1'")
        c.commit()
    f,_=db.production_approve('task','key',row['plan_version'])
    assert not db.production_claim_assets(f['flow_id'])
    db.update_batch_item_status(planning['item_id'],'awaiting_confirmation')
    assert db.production_claim_assets(f['flow_id'])
    assert db.claim_next_batch_item(global_concurrency=10)=={}

@pytest.fixture
def flow_routes(db,monkeypatch,tmp_path):
    from src.api import routes,task_manager as managers
    from src.api.task_manager import TaskManager
    from src.api.task_runtime import TaskRuntimeRegistry
    from src.config import Config
    monkeypatch.setattr(routes,'mysql_client',db)
    monkeypatch.setattr(managers,'db_client',db)
    monkeypatch.setattr(routes,'task_manager',TaskManager())
    monkeypatch.setattr(routes,'task_runtime',TaskRuntimeRegistry())
    monkeypatch.setattr(Config,'BASE_DIR',tmp_path)
    monkeypatch.setattr(routes,'_config_readiness',lambda *a,**k:{'status':'ready','items':[]})
    monkeypatch.setattr(routes,'_resolve_new_task_voice',lambda *a,**k:'mimo:冰糖')
    return routes

def test_confirm_batch_accepts_valid_items_without_a_preview_gate(db,flow_routes):
    import asyncio
    r=flow_routes
    db.create_batch('batch',[{'item_id':str(i),'theme':str(i),'normalized_theme':str(i)} for i in range(3)],{},3)
    selected=[]
    for i in range(3):
        tid=str(i);row=ready_task(db,tid)
        with db.get_connection() as c:
            c.execute('UPDATE task_batch_items SET task_id=? WHERE item_id=?',(tid,str(i)));c.commit()
        selected.append({'task_id':tid,'snapshot_key':r._plan_fingerprint(row,db.get_segments(tid)),'plan_version':row['plan_version']})
    selected[1]['snapshot_key']='old'
    db.save_segments('2',[{'segment_index':0,'text':'waiting','image_prompt':''}])
    result=asyncio.run(r.confirm_batch_production('batch',{'items':selected}))
    assert [i['outcome'] for i in result['items']]==['accepted','conflict','conflict']
    assert len(db.production_list())==1

def test_cancel_pending_render_waits_for_job_and_retains_assets(db,flow_routes,monkeypatch):
    from src.api.production_flow import ProductionScheduler
    row=ready_task(db);f,_=db.production_approve('task','key',row['plan_version'])
    db.production_update(f['flow_id'],state='rendering',job_id='job',cancel_requested=1)
    updates=[]
    monkeypatch.setattr(flow_routes,'_export_job_snapshot',lambda _: {'status':'pending'})
    monkeypatch.setattr(flow_routes,'_update_export_job',lambda *a,**kw:updates.append(kw))
    ProductionScheduler().tick()
    assert updates==[{'cancel_requested':True}]
    assert db.production_list()[0]['state']=='rendering'
    assert db.get_segments('task')[0]['text']=='原文'

def test_new_scripts_expose_exact_body_until_the_user_edits_segments(db,flow_routes):
    row=ready_task(db)
    body='  第一段。\n\n第二段。\n'
    db.save_task_checkpoint('task',script_text=body)
    assert flow_routes._workspace_script(db.get_task('task'),db.get_segments('task'))[0]==body

def test_upgrade_keeps_old_batch_items_indexes_and_links(tmp_path,monkeypatch):
    import sqlite3
    path=tmp_path/'old.db';monkeypatch.setattr(module,'DB_PATH',path)
    old=SQLiteClient();old._init_db()
    old.create_batch('kept',[{'item_id':str(i),'theme':str(i),'normalized_theme':str(i)} for i in range(2)],{},3)
    old.create_task('linked','theme','style',100,input_mode='theme')
    with old.get_connection() as c:
        c.execute("UPDATE task_batch_items SET task_id='linked' WHERE item_id='0'")
        c.execute("DELETE FROM schema_migrations WHERE version='20260906_batch_video'")
        c.execute('DROP TABLE production_flows')
        c.execute('ALTER TABLE tasks DROP COLUMN input_mode_known')
        c.execute('ALTER TABLE tasks DROP COLUMN original_input')
        c.execute('ALTER TABLE production_templates DROP COLUMN length')
        c.commit()
        indexes=c.execute("SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='task_batches'").fetchall()
    upgraded=SQLiteClient();upgraded._init_db();again=SQLiteClient();again._init_db()
    assert upgraded.get_batch('kept')['items'][0]['task_id']=='linked'
    with upgraded.get_connection() as c:
        assert c.execute("SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='task_batches'").fetchall()==indexes
    with sqlite3.connect(str(path)+'.before-batch-video.bak') as backup:
        assert backup.execute("SELECT task_id FROM task_batch_items WHERE item_id='0'").fetchone()[0]=='linked'

@pytest.mark.parametrize('limit',[3,10])
def test_fifty_items_use_batch_limit_not_batch_size(db,limit):
    db.create_batch('fifty',[{'item_id':str(i),'theme':str(i),'normalized_theme':str(i)} for i in range(50)],{},limit)
    assert all(db.claim_next_batch_item(global_concurrency=10) for _ in range(limit))
    assert db.claim_next_batch_item(global_concurrency=10)=={}
    batch=db.get_batch('fifty')
    assert batch['counts']['running']==limit
    assert batch['counts']['queued']==50-limit

def test_shared_pool_really_executes_ten_stages_and_queues_the_rest():
    import threading,time
    from src.api.work_limits import GenerationThread
    release=threading.Event();ready=threading.Event();lock=threading.Lock()
    stats={'running':0,'peak':0,'done':0}
    def work():
        with lock:
            stats['running']+=1;stats['peak']=max(stats['peak'],stats['running'])
            if stats['running']==10:ready.set()
        release.wait(3)
        with lock:stats['running']-=1;stats['done']+=1
    threads=[GenerationThread(work) for _ in range(15)]
    try:
        for thread in threads:thread.start()
        assert ready.wait(2)
        assert stats['peak']==10 and stats['done']==0
    finally:
        release.set()
        for thread in threads:thread.join(5)
    assert stats['done']==15 and stats['peak']==10

def test_failed_migration_rolls_back_ddl_and_is_safe_to_repeat(tmp_path,monkeypatch):
    import sqlite3
    from src.database import production
    path=tmp_path/'atomic.db';monkeypatch.setattr(module,'DB_PATH',path)
    migrate=production.migrate_batch_video
    def fail(cur):
        migrate(cur)
        raise RuntimeError('injected migration failure')
    monkeypatch.setattr(production,'migrate_batch_video',fail)
    failed=SQLiteClient();failed._init_db()
    assert not failed._initialized
    with sqlite3.connect(path) as c:
        assert 'input_mode_known' not in {r[1] for r in c.execute('PRAGMA table_info(tasks)')}
        assert not c.execute("SELECT 1 FROM sqlite_master WHERE name='production_flows'").fetchone()
        assert not c.execute("SELECT 1 FROM schema_migrations WHERE version=?",(production.VERSION,)).fetchone()
    monkeypatch.setattr(production,'migrate_batch_video',migrate)
    retried=SQLiteClient();retried._init_db();assert retried._initialized

def test_uncreated_items_are_counted_as_waiting_for_a_slot(db,flow_routes):
    db.create_batch('waiting',[{'item_id':str(i),'theme':str(i),'normalized_theme':str(i)} for i in range(50)],{},3)
    summary=flow_routes._batch_video_summary(db.get_batch('waiting'))
    assert summary['runtime_counts']=={'running':0,'queued':50,'provider_wait':0}

def test_render_only_retry_does_not_need_provider_credentials(db,flow_routes,monkeypatch):
    from src.api.production_flow import confirm
    row=ready_task(db);segments=db.get_segments('task')
    monkeypatch.setattr(flow_routes,'_workspace_health',lambda *a:{'assets_complete':True})
    def unavailable(*a,**k):raise AssertionError('Rendering must not consult a provider')
    monkeypatch.setattr(flow_routes,'_config_readiness',unavailable)
    monkeypatch.setattr(flow_routes,'_resolve_new_task_voice',unavailable)
    result=confirm('task',{'snapshot_key':flow_routes._plan_fingerprint(row,segments),'plan_version':row['plan_version']})
    assert result['outcome']=='accepted'
