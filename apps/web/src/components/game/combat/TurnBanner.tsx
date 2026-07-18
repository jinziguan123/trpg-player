// 回合/轮次横幅：turn 变化短暂显示「轮到 {name}」，round 变化显示「第 {N} 轮」。
// 由 CombatStage 的 useTurnBanner 驱动（1.8s 自动消失）；reduced-motion 时 CSS 兜底关掉扫入动画、静态显示。
import { useEffect, useRef, useState } from 'react'
import { GiCrossedSwords, GiHourglass, GiRollingDices } from 'react-icons/gi'
import type { Combatant } from './types'

export interface BannerState {
  text: string
  mine: boolean      // 轮到我：更醒目（骰子图标 + 强光）
  enemy: boolean     // 敌方回合：血红渐变；否则琥珀
  round: boolean     // 轮次横幅（沙漏图标）
  seq: number
}

// 监听 round/turn 变化产出横幅态：首帧只建基准不弹（重连/刚开面板不闪横幅）。
export function useTurnBanner(round: number, turn: string | null, order: Combatant[], myCharId: string | null): BannerState | null {
  const prev = useRef<{ round: number; turn: string | null } | null>(null)
  const seqRef = useRef(0)
  const [banner, setBanner] = useState<BannerState | null>(null)

  useEffect(() => {
    const before = prev.current
    prev.current = { round, turn }
    if (before === null) return                      // 首帧：建基准
    const roundChanged = round !== before.round
    const turnChanged = turn !== before.turn
    if (!roundChanged && !turnChanged) return
    const actor = turn ? order.find((c) => c.id === turn) ?? null : null
    const mine = !!(myCharId && turn === myCharId)
    const text = roundChanged
      ? (actor ? `第 ${round} 轮 · 轮到 ${actor.name}` : `第 ${round} 轮`)
      : `轮到 ${actor?.name ?? '……'}`
    seqRef.current += 1
    setBanner({ text, mine, enemy: actor?.side === 'enemy', round: roundChanged, seq: seqRef.current })
    const t = setTimeout(() => setBanner(null), 1800)
    return () => clearTimeout(t)
    // order/myCharId 只作查名用，不触发横幅；round/turn 才是变化源。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [round, turn])

  return banner
}

export function TurnBanner({ banner }: { banner: BannerState | null }) {
  if (!banner) return null
  const col = banner.enemy ? 'var(--color-danger)' : 'var(--color-accent)'
  const Icon = banner.round ? GiHourglass : banner.mine ? GiRollingDices : GiCrossedSwords
  return (
    <div
      key={banner.seq}
      className="turn-banner"
      style={{
        background: `linear-gradient(90deg, transparent, color-mix(in srgb, ${col} ${banner.mine ? 34 : 20}%, var(--color-bg-tertiary)) 50%, transparent)`,
        borderTop: `1px solid color-mix(in srgb, ${col} ${banner.mine ? 80 : 55}%, transparent)`,
        borderBottom: `1px solid color-mix(in srgb, ${col} ${banner.mine ? 80 : 55}%, transparent)`,
        color: banner.mine ? 'var(--color-text-accent)' : 'var(--color-text-primary)',
        textShadow: banner.mine ? `0 0 10px color-mix(in srgb, ${col} 55%, transparent)` : 'none',
      }}
    >
      <Icon size={banner.mine ? 16 : 14} style={{ color: col, flexShrink: 0 }} />
      <span>{banner.text}</span>
    </div>
  )
}
