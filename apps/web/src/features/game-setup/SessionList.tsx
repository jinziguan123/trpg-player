import { ConfirmDialog } from '@/components/ui/confirm-dialog'
import type { GameSetupState } from './useGameSetup'

function formatTime(timestamp?: string) {
  if (!timestamp) return ''
  const date = new Date(timestamp)
  return `${date.getMonth() + 1}/${date.getDate()} ${date.getHours().toString().padStart(2, '0')}:${date.getMinutes().toString().padStart(2, '0')}`
}

function statusBadge(status: string) {
  return status === 'setup' ? '大厅中' : status === 'active' ? '进行中' : '已暂停'
}

export function SessionList({ setup }: { setup: GameSetupState }) {
  const { activeSessions, openSession, deleteSession } = setup

  return (
    <section>
      <h3 className="card-title">我的房间</h3>
      {activeSessions.length === 0 && (
        <p className="mb-2 text-sm" style={{ color: 'var(--color-text-secondary)' }}>
          暂无进行中的房间。点右上角「新增游戏」开新局或加入房间。
        </p>
      )}
      {activeSessions.map((session) => (
        <div
          key={session.id}
          onClick={() => openSession(session)}
          role="button"
          tabIndex={0}
          onKeyDown={(event) => {
            if (event.key === 'Enter') openSession(session)
          }}
          className="card mb-2 w-full cursor-pointer text-left transition-colors hover:border-[var(--color-accent)]"
        >
          <div className="flex items-center justify-between">
            <div>
              <span className="font-semibold" style={{ color: 'var(--color-text-accent)' }}>
                {session.module_title || '未知模组'}
              </span>
              <span className="mx-2" style={{ color: 'var(--color-border)' }}>—</span>
              <span>{session.character_name || '未知角色'}</span>
            </div>
            <div className="flex items-center gap-2">
              <span className="text-xs" style={{ color: 'var(--color-text-secondary)' }}>
                {formatTime(session.created_at)}
              </span>
              <span className="badge">{statusBadge(session.status)}</span>
              <ConfirmDialog
                title="删除游戏"
                description="确定要删除该游戏存档吗？聊天记录将一并删除，此操作不可恢复。"
                confirmLabel="删除"
                onConfirm={() => deleteSession(session.id)}
              >
                {(open) => (
                  <button
                    onClick={(event) => {
                      event.stopPropagation()
                      open()
                    }}
                    className="rounded px-1.5 py-0.5 text-xs transition-colors hover:bg-[var(--color-danger-deep)] hover:text-white"
                    style={{
                      color: 'var(--color-danger)',
                      border: '1px solid var(--color-danger)',
                    }}
                  >
                    删除
                  </button>
                )}
              </ConfirmDialog>
            </div>
          </div>
        </div>
      ))}
    </section>
  )
}
