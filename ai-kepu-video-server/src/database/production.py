"""Durable, explicitly approved video production. No provider calls from the store."""
import json
import uuid

VERSION = '20260906_batch_video'
ACTIVE = ('queued', 'generating_assets', 'render_queued', 'rendering', 'cancelling')


def migrate_batch_video(cur):
    if cur.execute('SELECT 1 FROM schema_migrations WHERE version=?', (VERSION,)).fetchone():
        return
    # SQLite cannot ALTER a CHECK constraint. Recreate the table preserving every column/index.
    sql = cur.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='task_batches'").fetchone()[0]
    indexes = [r[0] for r in cur.execute("SELECT sql FROM sqlite_master WHERE type='index' AND tbl_name='task_batches' AND sql IS NOT NULL").fetchall()]
    cur.execute(sql.replace('task_batches', 'task_batches_expanded', 1).replace('BETWEEN 1 AND 3', 'BETWEEN 1 AND 10'))
    cur.execute('INSERT INTO task_batches_expanded SELECT * FROM task_batches')
    cur.execute('DROP TABLE task_batches')
    cur.execute('ALTER TABLE task_batches_expanded RENAME TO task_batches')
    for index in indexes:
        cur.execute(index)
    cur.execute('ALTER TABLE tasks ADD COLUMN input_mode_known INTEGER NOT NULL DEFAULT 0')
    cur.execute('ALTER TABLE tasks ADD COLUMN original_input TEXT')
    cur.execute("UPDATE tasks SET original_input=theme, input_mode_known=CASE WHEN script_text IS NOT NULL OR input_mode='images' THEN 1 ELSE 0 END")
    cur.execute('ALTER TABLE production_templates ADD COLUMN length INTEGER NOT NULL DEFAULT 300')
    cur.execute('''CREATE TABLE production_flows (
        flow_id TEXT PRIMARY KEY, task_id TEXT NOT NULL, batch_id TEXT,
        approval_key TEXT NOT NULL, plan_version INTEGER NOT NULL,
        state TEXT NOT NULL, job_id TEXT, error TEXT,
        cancel_requested INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(task_id, approval_key))''')
    cur.execute('CREATE INDEX idx_production_state ON production_flows(state, batch_id)')
    cur.execute('INSERT INTO schema_migrations(version) VALUES (?)', (VERSION,))


class ProductionStore:
    def production_list(self, task_id=None, batch_id=None, active=False):
        with self.get_connection() as conn:
            sql, args = 'SELECT * FROM production_flows WHERE 1=1', []
            if task_id:
                sql += ' AND task_id=?'; args.append(task_id)
            if batch_id:
                sql += ' AND batch_id=?'; args.append(batch_id)
            if active:
                sql += ' AND state IN (?,?,?,?,?)'; args.extend(ACTIVE)
            sql += ' ORDER BY rowid'
            return [dict(r) for r in conn.execute(sql, args).fetchall()]

    def production_update(self, flow_id, **values):
        allowed = {'state', 'job_id', 'error', 'cancel_requested'}
        if not values or set(values) - allowed:
            raise ValueError('Invalid production update')
        with self.get_connection() as conn:
            guard = ' AND cancel_requested=0' if values.get('state') in ('queued','generating_assets','render_queued','rendering','completed') else ''
            conn.execute('UPDATE production_flows SET ' + ','.join(k+'=?' for k in values) + ",updated_at=strftime('%Y-%m-%d %H:%M:%f','now') WHERE flow_id=?" + guard, [*values.values(), flow_id])
            conn.commit()

    def production_approve(self, task_id, approval_key, plan_version):
        with self.get_connection() as conn:
            conn.execute('BEGIN IMMEDIATE')
            task = conn.execute('SELECT * FROM tasks WHERE task_id=?', (task_id,)).fetchone()
            if not task or task['status'] == 'deleting' or task['plan_version'] != plan_version:
                raise ValueError('预案已变化或项目正在删除')
            active = conn.execute("SELECT * FROM production_flows WHERE task_id=? AND state IN ('queued','generating_assets','render_queued','rendering','cancelling')", (task_id,)).fetchone()
            if active:
                if active['approval_key'] != approval_key:
                    raise ValueError('旧版本生产正在停止，请稍后确认新版本')
                return dict(active), False
            existing = conn.execute('SELECT * FROM production_flows WHERE task_id=? AND approval_key=?', (task_id, approval_key)).fetchone()
            if existing and existing['state'] == 'completed':
                return dict(existing), False
            batch = conn.execute('SELECT batch_id FROM task_batch_items WHERE task_id=? LIMIT 1', (task_id,)).fetchone()
            flow_id = existing['flow_id'] if existing else uuid.uuid4().hex
            if existing:
                conn.execute("UPDATE production_flows SET state='queued',error=NULL,cancel_requested=0,job_id=NULL,plan_version=?,updated_at=CURRENT_TIMESTAMP WHERE flow_id=?", (plan_version, flow_id))
            else:
                conn.execute('INSERT INTO production_flows(flow_id,task_id,batch_id,approval_key,plan_version,state) VALUES(?,?,?,?,?,?)', (flow_id,task_id,batch[0] if batch else None,approval_key,plan_version,'queued'))
            conn.execute('UPDATE tasks SET voice_confirmed=1 WHERE task_id=?', (task_id,))
            conn.commit()
            return dict(conn.execute('SELECT * FROM production_flows WHERE flow_id=?', (flow_id,)).fetchone()), True

    def production_claim_assets(self, flow_id):
        # Shares batch accounting with claim_next_batch_item in the same SQLite write lock.
        with self.get_connection() as conn:
            conn.execute('BEGIN IMMEDIATE')
            flow = conn.execute("SELECT * FROM production_flows WHERE flow_id=? AND state='queued' AND cancel_requested=0", (flow_id,)).fetchone()
            if not flow: return False
            if flow['batch_id']:
                batch = conn.execute('SELECT concurrency FROM task_batches WHERE batch_id=?', (flow['batch_id'],)).fetchone()
                planning = conn.execute("SELECT COUNT(*) FROM task_batch_items WHERE batch_id=? AND status='running'", (flow['batch_id'],)).fetchone()[0]
                assets = conn.execute("SELECT COUNT(*) FROM production_flows WHERE batch_id=? AND state='generating_assets'", (flow['batch_id'],)).fetchone()[0]
                if not batch or planning + assets >= batch[0]: return False
            conn.execute("UPDATE production_flows SET state='generating_assets',updated_at=CURRENT_TIMESTAMP WHERE flow_id=?", (flow_id,))
            conn.commit()
            return True

    def production_recover(self):
        with self.get_connection() as conn:
            conn.execute("UPDATE production_flows SET state='failed',error='服务重启中断了生产，请重试；已有素材已保留' WHERE state='generating_assets'")
            conn.execute("UPDATE production_flows SET state='cancelled' WHERE cancel_requested=1")
            conn.commit()
