import { WorkspaceSaveQueue } from './workspaceSaveQueue'
import { readPendingEdits, writePendingEdits } from './workspacePendingStorage'
import { getTaskWorkspace, updateSegment } from '../api/task'

// The global navigation can open Export while the workbench has pending edits.
// Rehydrate those edits before allowing an export action to capture its snapshot.
export async function flushStoredWorkspaceEdits(taskId) {
  const pending = readPendingEdits(taskId)
  if (!Object.keys(pending).length) return false
  const queue = new WorkspaceSaveQueue({
    pending,
    send: (index, patch) => updateSegment(taskId, index, patch),
    refresh: () => getTaskWorkspace(taskId),
    onChange: (_patches, serialized) => {
      writePendingEdits(taskId, serialized)
    },
  })
  try {
    queue.rebase(await getTaskWorkspace(taskId))
    await queue.flush()
    return true
  } finally { queue.stop() }
}
