// 结算回显：掷骰落定后钉在战斗面板顶部——对抗时「敌方 | 我方」左右并排（数值/技能/成败 + 高亮胜方），
// 普通命中/单侧检定则一条带成败色的横幅。让玩家无需收起战斗面板即可看到本次结果。
import { GiCrossedSwords, GiRollingDices } from 'react-icons/gi'
import type { Combatant, CombatResultView, OppData, OppSide } from './types'
import { outcomeAccent, outcomeLabel } from './meta'

export function CombatResultReveal({ result, order }: { result: CombatResultView; order: Combatant[] }) {
  const meta = result.metadata
  const opp = meta.opposed as OppData | undefined
  const sideOf = (name: string): 'enemy' | 'mine' =>
    (order.find((o) => o.name === name)?.side === 'enemy' ? 'enemy' : 'mine')

  if (opp?.defender) {
    const sides = [
      { s: opp.attacker, who: 'attacker' as const },
      { s: opp.defender, who: 'defender' as const },
    ]
    const enemyEntry = sides.find((e) => sideOf(e.s.name) === 'enemy') ?? sides[0]
    const myEntry = enemyEntry === sides[0] ? sides[1] : sides[0]
    const resultAccent = opp.result === '命中' || opp.result === '反击得手'
      ? 'var(--color-danger)'
      : opp.result === '被闪开/防住' ? 'var(--color-success)' : 'var(--color-text-secondary)'

    const Cell = ({ label, s, won }: { label: string; s: OppSide; won: boolean }) => {
      const accent = outcomeAccent(s.outcome)
      return (
        <div
          className="flex-1 flex flex-col items-center px-2 py-1 rounded-md min-w-0"
          style={{
            background: won ? 'color-mix(in srgb, var(--color-bg-tertiary) 60%, transparent)' : 'transparent',
            border: won ? `1px solid ${accent}` : '1px solid transparent',
            opacity: won || opp.winner === null ? 1 : 0.6,
          }}
        >
          <span className="text-[10px]" style={{ color: 'var(--color-text-secondary)' }}>{label}</span>
          <span className="text-xs font-semibold truncate max-w-full" style={{ color: 'var(--color-text-primary)' }}>{s.name}</span>
          <span className="font-bold leading-none my-0.5" style={{ fontSize: '1.4rem', color: accent }}>{s.roll}</span>
          <span style={{ fontSize: '0.6rem', color: 'var(--color-text-secondary)' }}>{s.skill} / {s.target}</span>
          <span style={{ fontSize: '0.65rem', color: accent }}>{outcomeLabel(s.outcome)}</span>
        </div>
      )
    }
    return (
      <div className="mb-2 rounded-md px-2 py-1.5" style={{ borderLeft: `3px solid ${resultAccent}`, background: 'var(--color-bg-secondary)' }}>
        <div className="flex items-center gap-1.5 mb-1" style={{ color: 'var(--color-text-secondary)', fontSize: '0.62rem' }}>
          <GiCrossedSwords style={{ fontSize: '0.75rem' }} /> <span>本轮对抗结算</span>
          <span className="ml-auto font-semibold" style={{ color: resultAccent }}>{opp.result}</span>
        </div>
        <div className="flex items-stretch gap-1">
          <Cell label="敌方" s={enemyEntry.s} won={opp.winner === enemyEntry.who} />
          <div className="flex items-center px-0.5">
            <span className="text-[0.7rem] font-bold italic" style={{ color: 'var(--color-text-secondary)', opacity: 0.7 }}>VS</span>
          </div>
          <Cell label="我方" s={myEntry.s} won={opp.winner === myEntry.who} />
        </div>
      </div>
    )
  }

  // 普通结算（命中/未命中 或 单侧检定）：带成败色的一条横幅
  const hit = meta.hit
  const accent = typeof hit === 'boolean'
    ? (hit ? 'var(--color-danger)' : 'var(--color-text-secondary)')
    : outcomeAccent(String(meta.outcome ?? ''))
  return (
    <div className="mb-2 rounded-md px-2.5 py-1.5 flex items-center gap-2 text-xs" style={{ borderLeft: `3px solid ${accent}`, background: 'var(--color-bg-secondary)' }}>
      <GiRollingDices style={{ color: accent, fontSize: '1rem', flexShrink: 0 }} />
      <span className="whitespace-pre-wrap" style={{ color: 'var(--color-text-primary)' }}>{result.content.replace(/^🎲\s*/, '')}</span>
    </div>
  )
}
