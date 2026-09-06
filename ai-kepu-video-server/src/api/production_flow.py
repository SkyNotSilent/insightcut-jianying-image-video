"""Server-owned progression from explicit approval to assets and an MP4."""
import hashlib
import json
import logging
import threading
from fastapi import HTTPException

log = logging.getLogger(__name__)


def approval_key(row, segments):
    # Asset paths/status/durations change during generation; user-authored inputs do not.
    payload = {k: row.get(k) for k in ('style','ratio','voice_type','tts_options_json','subtitle_options_json','plan_version')}
    # audio voice snapshots are populated by generation; task plan_version protects edits.
    payload['segments'] = [{k:s.get(k) for k in ('segment_index','text','image_prompt')} for s in segments]
    return hashlib.sha256(json.dumps(payload,ensure_ascii=False,sort_keys=True).encode()).hexdigest()


def confirm(task_id, payload):
    from . import routes as r
    db = r.mysql_client
    row = db.get_task(task_id)
    if not row or row.get('status') == 'deleting':
        raise HTTPException(404, '项目不存在或正在删除')
    segments = db.get_segments(task_id)
    if payload.get('snapshot_key') != r._plan_fingerprint(row, segments) or payload.get('plan_version', row.get('plan_version',0)) != row.get('plan_version',0):
        raise HTTPException(409, '预案已变化，请确认最新内容')
    if not segments or any(not s.get('image_prompt') or s.get('prompt_status') in ('failed','processing') for s in segments):
        raise HTTPException(409, '分镜提示词尚未就绪')
    if row.get('workflow_phase') == 'planning' or row.get('status') == 'pending':
        raise HTTPException(409, '预案仍在生成中')
    if not r._workspace_health(row, segments)['assets_complete']:
        ready = r._config_readiness(row.get('voice_type'), 'assets')
        if ready['status'] == 'not_ready':
            raise HTTPException(409, {'code':'config_not_ready','message':'图片或配音配置不完整，预案已保留','items':ready['items']})
        r._resolve_new_task_voice(row.get('voice_type'))
    for previous in db.production_list(task_id=task_id):
        if previous['state'] == 'completed' and not r._preview_state(r.task_manager.get_task(task_id), segments)['valid']:
            db.production_update(previous['flow_id'], state='failed', error='视频结果缺失或已过期，请重试')
    try:
        flow, created = db.production_approve(task_id, approval_key(row,segments), int(row.get('plan_version') or 0))
    except ValueError as e:
        raise HTTPException(409, str(e)) from e
    r.task_manager.invalidate_task_cache(task_id)
    scheduler.wake()
    return {**flow, 'target':'mp4', 'outcome':'accepted' if created else 'already_running' if flow['state']!='completed' else 'already_completed'}


def cancel(task_id):
    from . import routes as r
    flows = r.mysql_client.production_list(task_id=task_id, active=True)
    for f in flows:
        r.mysql_client.production_update(f['flow_id'],cancel_requested=1,state='cancelling')
        r.task_runtime.request_cancel(task_id)
        if f.get('job_id'):
            r._update_export_job(f['job_id'],cancel_requested=True)
    scheduler.wake()
    return {'task_id':task_id,'outcome':'cancelling' if flows else 'not_running'}


class ProductionScheduler:
    def __init__(self):
        self.stop_event=threading.Event(); self.event=threading.Event(); self.thread=None
    def wake(self): self.event.set()
    def start(self):
        if self.thread and self.thread.is_alive(): return
        self.stop_event.clear()
        self.thread=threading.Thread(target=self.run,name='video-production',daemon=True);self.thread.start()
    def stop(self): self.stop_event.set();self.event.set()
    def join(self,timeout=30):
        if self.thread:self.thread.join(timeout)
    def run(self):
        while not self.stop_event.is_set():
            try:self.tick()
            except Exception:log.exception('成片调度暂时失败，将再次检查')
            self.event.wait(0.5);self.event.clear()
    def tick(self):
        from . import routes as r
        for flow in r.mysql_client.production_list(active=True):
            try:self.advance(flow,r)
            except Exception as e:
                safe=r.classify_exception(e,provider='production')
                r.mysql_client.production_update(flow['flow_id'],state='failed',error=safe.safe_message)
    def advance(self,f,r):
        db=r.mysql_client; tid=f['task_id']; fid=f['flow_id']
        row=db.get_task(tid); segments=db.get_segments(tid) if row else []
        changed=bool(row and approval_key(row,segments)!=f['approval_key'])
        stopping=f['cancel_requested'] or not row or row.get('status')=='deleting' or changed
        if stopping:
            r.task_runtime.request_cancel(tid)
            job=r._export_job_snapshot(f['job_id']) if f.get('job_id') else {}
            if job and job.get('status') in ('pending','queued','processing'):
                r._update_export_job(f['job_id'],cancel_requested=True)
                return
            if r.task_runtime.is_running(tid):return
            db.production_update(fid,state='stale' if changed else 'cancelled',error='预案已变化，请重新确认' if changed else None)
            return
        if f['state']=='queued':
            if r.task_runtime.is_running(tid):return
            if r._workspace_health(row,segments)['assets_complete']:
                db.production_update(fid,state='render_queued');return
            if not db.production_claim_assets(fid):return
            # review_first prevents the legacy executor from building an unsolicited draft.
            db.save_task_checkpoint(tid,execution_mode='review_first')
            if row.get('status') not in ('awaiting_confirmation','failed','interrupted'):
                db.update_task_workflow(tid,'awaiting_confirmation',status='awaiting_confirmation',current_step='awaiting_confirmation')
            outcome=r.task_executor.continue_task(tid)
            if outcome == 'already_running':
                db.production_update(fid,state='queued');return
            if outcome != 'started':
                db.production_update(fid,state='failed',error='素材生产无法启动，请重试')
        elif f['state']=='generating_assets':
            if r.task_runtime.is_running(tid):return
            if r._workspace_health(row,segments)['assets_complete']:
                db.production_update(fid,state='render_queued')
            else:db.production_update(fid,state='failed',error=row.get('error') or '部分素材未完成，请重试；成功内容已保留')
        elif f['state']=='render_queued':
            if r.task_runtime.is_running(tid):return
            payload={'target':'mp4','auto_download':False,'production_flow_id':fid,'approval_key':f['approval_key']}
            job,created=r._create_or_reuse_export_job(tid,'mp4',payload)
            db.production_update(fid,state='rendering',job_id=job['job_id'])
            if created:r._submit_export(job,True,payload)
        elif f['state']=='rendering':
            job=r._export_job_snapshot(f['job_id'])
            if job.get('status')=='completed':
                task=r.task_manager.get_task(tid)
                if not task or not r._preview_state(task,segments)['valid']:
                    db.production_update(fid,state='failed',error='视频结果缺失或已过期，请重试');return
                db.production_update(fid,state='completed',error=None)
                db.update_task_workflow(tid,'ready',status='completed',current_step='completed')
                r.task_manager.invalidate_task_cache(tid)
            elif not job:
                db.production_update(fid,state='failed',error='渲染记录缺失，请重试；素材已保留')
            elif job.get('status') in ('failed','cancelled'):
                db.production_update(fid,state=job['status'],error=job.get('error'))

scheduler=ProductionScheduler()
