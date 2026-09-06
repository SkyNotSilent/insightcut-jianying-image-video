# 批次归档

批次详情提供“归档批次”和“恢复批次”。列表默认展示当前（未归档）批次，切换“已归档”可查看历史批次。归档只收起列表，不删除任务、文稿、素材、视频或导出文件，不取消运行中的任务，不改变生产状态。没有自动归档或自动清理。

`POST /ai/native/video/kepu/batches/{batch_id}/archive` 接受 `{"archived": true}`，恢复传 `false`。操作可重复执行；不存在返回 404。

批次列表接受 `archived=true|false`，省略时保持旧接口返回全部批次的兼容行为。

SQLite 增量迁移 `20260907_batch_archive` 增加可空的 `task_batches.archived_at`，旧批次默认未归档。迁移前使用 SQLite backup 保存 `local.db.before-batch-archive.bak`。归档状态与批次一起持久化，恢复只清空归档时间，不重建任务。
