// B1 先攻轨：横向排 order，高亮当前、标下一个、走过者淡化。
import type { Combatant } from './types'
import { isOut, sideColor } from './meta'

export function InitiativeTrack({ order, turn, myCharId }: { order: Combatant[]; turn: string | null; myCharId: string | null }) {
  // 找出「下一个」：当前之后第一个未出局者（环形）。
  const turnIdx = order.findIndex((c) => c.id === turn)
  let nextIdx = -1
  if (turnIdx >= 0) {
    for (let k = 1; k <= order.length; k++) {
      const i = (turnIdx + k) % order.length
      if (!isOut(order[i])) { nextIdx = i; break }
    }
  }
  return (
    <div className="flex gap-1 overflow-x-auto pb-1">
      {order.map((c, i) => {
        const out = isOut(c)
        const isActive = c.id === turn
        const isNext = i === nextIdx
        const passed = turnIdx >= 0 && i < turnIdx && !isActive   // 本轮已走过者淡化
        const mine = !!(myCharId && c.id === myCharId)
        const dot = sideColor(c)
        return (
          <div
            key={c.id}
            className="flex-shrink-0 rounded px-2 py-1 inline-flex items-center gap-1"
            style={{
              opacity: out ? 0.4 : passed ? 0.5 : 1,
              background: isActive ? 'var(--color-bg-tertiary)' : 'transparent',
              border: isActive ? '1px solid var(--color-accent)' : '1px solid var(--color-border)',
              boxShadow: isActive ? '0 0 8px color-mix(in srgb, var(--color-accent) 30%, transparent)' : 'none',
            }}
            title={isActive ? '当前行动' : isNext ? '下一个' : c.name}
          >
            <span className="inline-block rounded-full" style={{ width: 6, height: 6, background: dot, flexShrink: 0 }} />
            <span className="text-[11px] whitespace-nowrap" style={{
              color: mine ? 'var(--color-text-accent)' : isActive ? 'var(--color-text-primary)' : 'var(--color-text-secondary)',
              fontWeight: isActive ? 600 : 400,
            }}>
              {c.name}{mine ? '（我）' : ''}
            </span>
            {isNext && (
              <span className="text-[9px] px-1 rounded flex-shrink-0" style={{ color: 'var(--color-text-secondary)', border: '1px solid var(--color-border)' }}>
                下一个
              </span>
            )}
          </div>
        )
      })}
    </div>
  )
}
