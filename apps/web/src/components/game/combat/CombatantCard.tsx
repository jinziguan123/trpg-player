// 单张参战方卡片：名字 + HP 条/数字 + 状态/条件徽标 + 武器，带掉血/回血动画。
import { GiShield, GiCrosshair, GiDeathSkull } from 'react-icons/gi'
import type { Combatant, HpDiff } from './types'
import { CONDITION_META, STATUS_META, isOut, pctOf } from './meta'

export function CombatantCard({ c, mine, active, diff }: {
  c: Combatant
  mine: boolean
  active: boolean
  diff?: HpDiff
}) {
  const out = isOut(c)
  const hpColor = c.side === 'enemy' ? 'var(--color-danger)' : 'var(--color-accent)'
  const sm = c.status !== 'ok' ? STATUS_META[c.status] : null
  const conds = (c.conditions || []).filter((k) => CONDITION_META[k])
  // 动画类：delta<0 掉血（红闪+抖）、>0 回血（绿涨）。用 seq 做 key 让连续同向变化也重播。
  const dmg = diff && diff.delta < 0
  const heal = diff && diff.delta > 0

  return (
    <div
      className={`relative rounded px-2.5 py-2 ${dmg ? 'hp-hit' : ''}`}
      style={{
        opacity: out ? 0.42 : 1,
        filter: out ? 'grayscale(0.7)' : 'none',
        background: active ? 'var(--color-bg-tertiary)' : 'var(--color-bg-secondary)',
        border: active ? '1px solid var(--color-accent)' : '1px solid var(--color-border)',
        boxShadow: active ? '0 0 10px color-mix(in srgb, var(--color-accent) 34%, transparent)' : 'none',
      }}
      title={`${c.name} · ${c.hp}/${c.max_hp}`}
    >
      {/* 浮动伤害/治疗数字（key=seq 触发一次动画） */}
      {diff && (diff.delta !== 0) && (
        <span key={diff.seq} className={`hp-float ${dmg ? 'hp-float--dmg' : 'hp-float--heal'}`}>
          {diff.delta > 0 ? `+${diff.delta}` : diff.delta}
        </span>
      )}
      <div className="flex items-center gap-1 mb-1 flex-wrap">
        {out && c.status === 'dead' && <GiDeathSkull size={12} style={{ color: 'var(--color-danger-deep)', flexShrink: 0 }} />}
        <span className="text-xs font-semibold truncate" style={{ color: mine ? 'var(--color-text-accent)' : 'var(--color-text-primary)' }}>
          {c.name}{mine ? '（我）' : ''}
        </span>
        {sm && (
          <span className="text-[10px] px-1 rounded flex-shrink-0" style={{ color: sm.color, border: `1px solid ${sm.color}` }}>
            {sm.label}
          </span>
        )}
      </div>
      {/* 血条：底层填充始终平滑过渡宽度（不换 key，保住 transition:width）；
          红闪/绿涨的颜色脉冲另起一层叠加，只有它带 seq key 重挂、播一次动画 → 宽度不瞬跳。 */}
      <div className="relative h-1.5 rounded-full overflow-hidden" style={{ background: 'var(--color-input-bg)' }}>
        <div className="stat-bar-fill h-full" style={{ width: `${pctOf(c)}%`, background: hpColor }} />
        {(dmg || heal) && (
          <div
            key={diff?.seq}
            className={`stat-bar-fill absolute inset-y-0 left-0 h-full ${dmg ? 'hp-bar-dmg' : 'hp-bar-heal'}`}
            style={{ width: `${pctOf(c)}%`, background: hpColor }}
          />
        )}
      </div>
      <div className="flex items-center justify-between gap-1 mt-0.5">
        <span className="text-[10px] font-mono" style={{ color: 'var(--color-text-secondary)' }}>{c.hp}/{c.max_hp}</span>
        <div className="flex items-center gap-1 flex-wrap justify-end">
          {!!c.armor && c.armor > 0 && !out && (
            <span className="text-[10px] px-1 rounded inline-flex items-center gap-0.5 flex-shrink-0"
              style={{ color: 'var(--color-text-secondary)', border: '1px solid var(--color-border-strong)' }}
              title={`护甲 ${c.armor}：每次物理伤害先扣 ${c.armor} 点`}>
              <GiShield size={10} /> {c.armor}
            </span>
          )}
          {c.aim && !out && (
            <span className="text-[10px] px-1 rounded inline-flex items-center gap-0.5 flex-shrink-0"
              style={{ color: 'var(--color-text-accent)', border: '1px solid var(--color-border-strong)' }}>
              <GiCrosshair size={10} /> 瞄准
            </span>
          )}
          {conds.map((k) => {
            const { label, Icon } = CONDITION_META[k]
            return (
              <span key={k} className="text-[10px] px-1 rounded inline-flex items-center gap-0.5 flex-shrink-0"
                style={{ color: 'var(--color-danger)', border: '1px solid var(--color-danger)' }}>
                <Icon size={10} /> {label}
              </span>
            )
          })}
        </div>
      </div>
      <div className="text-[10px] mt-0.5 truncate" style={{ color: 'var(--color-text-secondary)', opacity: 0.75 }}>
        {c.weapon || ''}
      </div>
    </div>
  )
}
