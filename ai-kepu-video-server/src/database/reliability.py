"""Durable edit history and export state, sharing SQLiteClient transactions."""
import json
import uuid
from contextlib import closing


class ReliabilityStore:
    def _snapshot_plan(self, cursor, task_id):
        task = dict(cursor.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone())
        segments = [dict(row) for row in cursor.execute(
            "SELECT * FROM task_segments WHERE task_id=? ORDER BY segment_index", (task_id,))]
        cursor.execute(
            "INSERT INTO task_plan_revisions(revision_id,task_id,snapshot_json) VALUES(?,?,?)",
            (uuid.uuid4().hex, task_id, json.dumps({"task": task, "segments": segments}, ensure_ascii=False)),
        )
        cursor.execute("UPDATE tasks SET original_script_text=COALESCE(original_script_text,script_text) WHERE task_id=?", (task_id,))

    def has_plan_revision(self, task_id):
        self._init_db()
        with closing(self._get_conn()) as conn:
            return bool(conn.execute("SELECT 1 FROM task_plan_revisions WHERE task_id=? AND restored=0 LIMIT 1", (task_id,)).fetchone())

    def restore_plan_revision(self, task_id, expected_plan_version=None):
        self._init_db()
        conn = self._get_conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            task = conn.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
            if not task:
                return None
            current = int(task["plan_version"] or 0)
            if expected_plan_version is not None and current != int(expected_plan_version):
                return -1
            revision = conn.execute("SELECT * FROM task_plan_revisions WHERE task_id=? AND restored=0 ORDER BY id DESC LIMIT 1", (task_id,)).fetchone()
            if not revision:
                return None
            snapshot = json.loads(revision["snapshot_json"])
            columns = {row[1] for row in conn.execute("PRAGMA table_info(task_segments)")} - {"id", "task_id"}
            conn.execute("DELETE FROM task_segments WHERE task_id=?", (task_id,))
            for segment in snapshot["segments"]:
                fields = {key: value for key, value in segment.items() if key in columns}
                fields["task_id"] = task_id
                conn.execute(f"INSERT INTO task_segments({','.join(fields)}) VALUES({','.join('?' for _ in fields)})", list(fields.values()))
            script = "\n".join(str(s.get("text") or "") for s in snapshot["segments"])
            conn.execute("""UPDATE tasks SET script_text=?, script_source='user_edited', plan_version=?,
                workflow_phase='awaiting_confirmation',status='awaiting_confirmation',error=NULL,
                error_code=NULL,error_meta_json=NULL,updated_at=datetime('now','localtime') WHERE task_id=?""",
                (script, current + 1, task_id))
            settings = {k: snapshot["task"][k] for k in ("template_id", "style", "ratio", "voice_type", "voice_confirmed", "tts_options_json", "generation_options_json", "subtitle_options_json", "summary", "input_mode", "script_policy") if k in snapshot["task"]}
            if settings:
                conn.execute("UPDATE tasks SET " + ",".join(k + "=?" for k in settings) + " WHERE task_id=?", [*settings.values(), task_id])
            conn.execute("UPDATE task_plan_revisions SET restored=1 WHERE revision_id=?", (revision["revision_id"],))
            conn.commit()
            return current + 1
        finally:
            conn.close()

    def save_export_job(self, job):
        self._init_db()
        with closing(self._get_conn()) as conn, conn:
            conn.execute("""INSERT INTO export_jobs(job_id,task_id,status,job_json) VALUES(?,?,?,?)
                ON CONFLICT(job_id) DO UPDATE SET status=excluded.status,job_json=excluded.job_json""",
                (job["job_id"], job["task_id"], job["status"], json.dumps(job, ensure_ascii=False)))

    def load_export_job(self, job_id):
        self._init_db()
        with closing(self._get_conn()) as conn:
            row = conn.execute("SELECT job_json FROM export_jobs WHERE job_id=?", (job_id,)).fetchone()
            return json.loads(row[0]) if row else None

    def load_export_jobs(self, task_id=None):
        self._init_db()
        with closing(self._get_conn()) as conn:
            rows = conn.execute("SELECT job_json FROM export_jobs" + (" WHERE task_id=?" if task_id else ""), (task_id,) if task_id else ())
            return [json.loads(row[0]) for row in rows]

    def project_catalog(self, *, page=1, limit=40, q="", status="all", style="", duration="", sort="updated"):
        self._init_db()
        cte = """WITH media AS (
            SELECT task_id,COUNT(*) AS segment_count,SUM(COALESCE(duration,0)) AS total_duration,
            SUM(CASE WHEN image_status='failed' OR audio_status='failed' OR
                (COALESCE(image_path,'')='' AND COALESCE(image_url,'')='') OR
                (COALESCE(audio_path,'')='' AND COALESCE(audio_url,'')='') THEN 1 ELSE 0 END) AS missing
            FROM task_segments GROUP BY task_id
        ), catalog AS (
            SELECT t.task_id,t.name,t.theme,t.style,t.voice_type,t.status,t.workflow_phase,t.updated_at,t.created_at,
                COALESCE(m.segment_count,0) AS segment_count,COALESCE(m.total_duration,0) AS total_duration,
                (SELECT COALESCE(NULLIF(s.image_url,''),NULLIF(s.image_path,'')) FROM task_segments s
                 WHERE s.task_id=t.task_id AND (COALESCE(s.image_url,'')!='' OR COALESCE(s.image_path,'')!='')
                 ORDER BY s.segment_index LIMIT 1) AS cover_image_url,
                CASE WHEN t.status='deleting' THEN 'deleting'
                     WHEN t.status IN ('pending','processing') THEN 'processing'
                     WHEN t.status IN ('awaiting_confirmation','awaiting_finalization') THEN 'waiting'
                     WHEN t.status='interrupted' OR (t.status='failed' AND (m.segment_count>0 OR COALESCE(t.script_text,'')!='')) THEN 'interrupted'
                     WHEN t.status='failed' OR m.missing>0 OR COALESCE(t.error,'')!='' THEN 'recoverable_assets'
                     ELSE 'completed' END AS display_state
            FROM tasks t LEFT JOIN media m ON t.task_id=m.task_id
        ) """
        clauses, params = ["1=1"], []
        if q.strip():
            clauses.append("instr(lower(COALESCE(name,'')||' '||theme||' '||COALESCE(voice_type,'')),lower(?))>0")
            params.append(q.strip())
        if style:
            clauses.append("substr(style,instr(style,'|')+1)=?")
            params.append(style)
        durations = {"under1": "total_duration>0 AND total_duration<60", "1to3": "total_duration>=60 AND total_duration<180",
                     "3to5": "total_duration>=180 AND total_duration<300", "over5": "total_duration>=300"}
        if duration in durations:
            clauses.append(durations[duration])
        where = " AND ".join(clauses)
        order = {"name": "COALESCE(NULLIF(name,''),theme) COLLATE NOCASE", "status": "display_state", "updated": "updated_at DESC"}.get(sort, "updated_at DESC")
        with closing(self._get_conn()) as conn:
            counts = {row[0]: row[1] for row in conn.execute(cte + f"SELECT display_state,COUNT(*) FROM catalog WHERE {where} GROUP BY display_state", params)}
            if status != "all":
                where += " AND display_state=?"
                params.append(status)
            total = conn.execute(cte + f"SELECT COUNT(*) FROM catalog WHERE {where}", params).fetchone()[0]
            items = [dict(row) for row in conn.execute(cte + f"SELECT * FROM catalog WHERE {where} ORDER BY {order},task_id LIMIT ? OFFSET ?", [*params, limit, (page-1)*limit])]
        labels = {"deleting": ("正在删除", "warning", "正在删除"), "processing": ("生成中", "info", "查看进度"), "waiting": ("待确认", "warning", "查看并继续"),
                  "interrupted": ("可继续", "warning", "查看并继续"), "recoverable_assets": ("失败可恢复", "danger", "查看已保存素材"),
                  "completed": ("素材已就绪", "success", "查看工作台")}
        for item in items:
            label, tone, action = labels[item["display_state"]]
            if item["status"] == "pending":
                label = "排队中"
            elif item["status"] == "awaiting_finalization":
                label = "待完成生产"
            item.update(display_label=label, display_tone=tone, action_label=action,
                        visual_style=item["style"].split("|", 1)[-1])
        return {"items": items, "total": total, "page": page, "limit": limit, "counts": {**counts, "all": sum(counts.values())}}
