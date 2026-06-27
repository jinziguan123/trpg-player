import type { SessionParticipant } from '../../stores/sessionStore'

interface Props {
  participants: SessionParticipant[]
}

/** 游戏页顶部的队伍条：展示主角与在场 AI 队友。 */
export function PartyRoster({ participants }: Props) {
  if (!participants || participants.length === 0) return null
  const sorted = [...participants].sort((a, b) => a.seat_order - b.seat_order)

  return (
    <div className="flex flex-wrap items-center gap-1.5">
      {sorted.map((p) => {
        const isPrimary = p.is_primary
        return (
          <span
            key={p.character_id}
            className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-xs border"
            style={{
              borderColor: isPrimary ? 'var(--color-accent)' : 'var(--color-border)',
              background: isPrimary ? 'var(--color-accent)' : 'transparent',
              color: isPrimary ? '#fff' : 'var(--color-text-secondary)',
            }}
            title={isPrimary ? '主角（你）' : 'AI 队友'}
          >
            <span style={{ fontSize: '0.65rem' }}>{isPrimary ? '★' : '🤖'}</span>
            {p.character_name || '未知角色'}
          </span>
        )
      })}
    </div>
  )
}
