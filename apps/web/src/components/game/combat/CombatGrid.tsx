// 方格战场：CSS-grid 格子层（地形/高亮） + 绝对定位令牌层（战棋令牌，transform 补间滑动）。
// 令牌：阵营色环 + 衬线首字 + 微型 HP 条 + 状态徽标；当前行动者呼吸光环；选中目标叠准星。
// 移动模式下高亮可达格（Chebyshev ≤ 剩余移动力、避开占用/障碍），点格移动；点敌方令牌选目标。
// 纯 --color-* 变量，gothic/parchment 两主题自适应，不引第三方战棋库。
import { GiBrickWall, GiShield, GiCrackedShield, GiCrosshair, GiFlame } from 'react-icons/gi'
import type { Combatant, CombatGridInfo, HpDiff } from './types'
import { TOKEN_STATUS_META, isOut, pctOf, sideColor } from './meta'
import { useDeathFx, usePrefersReducedMotion } from './useHpDiff'

const CELL = 46           // 单格边长（px）——放大后的战棋格
const TOKEN = CELL - 8    // 令牌直径

// 单个战棋令牌：外层 wrapper 只管棋盘定位（transform 补间），内层 fx 壳播抖动/死亡动画，
// 两层 transform 分离，滑动补间与受击抖动互不打架。
function CombatToken({ c, isTurn, isTarget, moveActive, reduced, diff, dieSeq, onClick }: {
  c: Combatant
  isTurn: boolean
  isTarget: boolean
  moveActive: boolean
  reduced: boolean
  diff?: HpDiff
  dieSeq?: number
  onClick: () => void
}) {
  const out = isOut(c)
  const col = sideColor(c)
  const pct = pctOf(c)
  const dmg = diff && diff.delta < 0
  const sm = c.status !== 'ok' ? TOKEN_STATUS_META[c.status] : null
  const burning = !out && (c.conditions || []).includes('burning')
  // fx 壳的动画类与 key：死亡瞬间播 token-die；掉血播 token-hit（key 换才会重播）。
  const fxClass = dieSeq ? 'token-die' : dmg ? 'token-hit' : ''
  const fxKey = `${dieSeq ?? 0}-${diff?.seq ?? 0}`
  // 微型 HP 条颜色：满血为阵营色，随血量流失渐变到深血红。
  const hpBarColor = `color-mix(in srgb, ${col} ${Math.round(pct)}%, var(--color-danger-deep))`

  return (
    <div
      style={{
        position: 'absolute', left: 0, top: 0, width: CELL, height: CELL,
        transform: `translate(${c.pos!.x * CELL}px, ${c.pos!.y * CELL}px)`,
        transition: reduced ? 'none' : 'transform 0.35s ease',
        zIndex: isTurn ? 3 : 2,
        pointerEvents: 'none',
      }}
    >
      {/* 棋盘上的伤害/治疗飘字（key=seq 播一次） */}
      {diff && diff.delta !== 0 && (
        <span key={diff.seq} className={`token-float ${dmg ? 'token-float--dmg' : 'token-float--heal'}`}>
          {diff.delta > 0 ? `+${diff.delta}` : diff.delta}
        </span>
      )}
      <div key={fxKey} className={fxClass} style={{ width: '100%', height: '100%', display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
        <button
          onClick={onClick}
          title={`${c.name} ${c.hp}/${c.max_hp}`}
          className={isTurn && !out ? 'combat-token token-active' : 'combat-token'}
          style={{
            position: 'relative',
            width: TOKEN, height: TOKEN, borderRadius: '50%', padding: 0,
            background: 'radial-gradient(circle at 34% 28%, var(--color-bg-tertiary), var(--color-bg-secondary))',
            color: 'var(--color-text-primary)',
            border: `2px solid ${col}`,
            opacity: out ? 0.38 : 1,
            filter: out ? 'grayscale(1)' : 'none',
            cursor: moveActive ? 'default' : 'pointer',
            pointerEvents: 'auto',
            display: 'flex', alignItems: 'center', justifyContent: 'center',
            overflow: 'hidden',
            // 呼吸光环颜色走变量，token-active 动画引用。
            ['--token-glow' as string]: col,
          }}
        >
          <span style={{
            fontFamily: 'var(--font-title)', fontSize: '0.95rem', fontWeight: 600,
            lineHeight: 1, letterSpacing: 0, userSelect: 'none',
            textShadow: '0 1px 2px rgba(0, 0, 0, 0.45)',
          }}>
            {c.name.slice(0, 1)}
          </span>
          {/* 令牌底部微型 HP 条 */}
          {!out && (
            <span style={{
              position: 'absolute', left: '18%', right: '18%', bottom: 3, height: 3.5,
              borderRadius: 2, background: 'var(--color-input-bg)', overflow: 'hidden', display: 'block',
            }}>
              <span className="stat-bar-fill" style={{
                display: 'block', height: '100%', width: `${pct}%`, borderRadius: 2, background: hpBarColor,
              }} />
            </span>
          )}
        </button>
      </div>
      {/* 状态小徽标（右上角）：dead/dying/unconscious/major_wound/fled */}
      {sm && (
        <span title={sm.label} style={{
          position: 'absolute', top: -3, right: -1, width: 15, height: 15, borderRadius: '50%',
          background: 'var(--color-bg-primary)', border: `1px solid ${sm.color}`,
          display: 'flex', alignItems: 'center', justifyContent: 'center', zIndex: 4,
        }}>
          <sm.Icon size={9} style={{ color: sm.color }} />
        </span>
      )}
      {/* 着火条件徽标（左上角） */}
      {burning && (
        <span title="着火" style={{
          position: 'absolute', top: -3, left: -1, width: 15, height: 15, borderRadius: '50%',
          background: 'var(--color-bg-primary)', border: '1px solid var(--color-danger)',
          display: 'flex', alignItems: 'center', justifyContent: 'center', zIndex: 4,
        }}>
          <GiFlame size={9} style={{ color: 'var(--color-danger)' }} />
        </span>
      )}
      {/* 被选为攻击目标：叠准星标记（替代红描边） */}
      {isTarget && !out && (
        <GiCrosshair className="token-target" style={{
          position: 'absolute', left: '50%', top: '50%', width: CELL * 0.92, height: CELL * 0.92,
          color: 'var(--color-danger)', zIndex: 5, pointerEvents: 'none',
        }} />
      )}
    </div>
  )
}

export function CombatGrid({ grid, order, turn, myCharId, moveActive, budget, dash, targetId, fx, onCellMove, onPieceClick }: {
  grid: CombatGridInfo
  order: Combatant[]
  turn: string | null
  myCharId: string | null
  moveActive: boolean
  budget: number
  dash: boolean
  targetId: string
  fx: Record<string, HpDiff>   // useHpDiff 产出（与右侧卡片共用），驱动令牌受击/飘字
  onCellMove: (x: number, y: number) => void
  onPieceClick: (c: Combatant) => void
}) {
  const reduced = usePrefersReducedMotion()
  const deathFx = useDeathFx(order)
  const me = myCharId ? order.find((c) => c.id === myCharId) : null
  const occupied = new Set(order.filter((c) => c.pos && !isOut(c)).map((c) => `${c.pos!.x},${c.pos!.y}`))
  const blocked = new Set(grid.blocked || [])
  const reach = new Set<string>()
  const threat = new Set<string>()   // 与存活敌方相邻的格：移动到此会进入近战/被夹击
  if (moveActive && me?.pos && budget > 0) {
    const b = budget
    for (let y = 0; y < grid.rows; y++) {
      for (let x = 0; x < grid.cols; x++) {
        const k = `${x},${y}`
        if (k === `${me.pos.x},${me.pos.y}` || occupied.has(k) || blocked.has(k)) continue
        if (Math.max(Math.abs(x - me.pos.x), Math.abs(y - me.pos.y)) <= b) reach.add(k)
      }
    }
    const meEnemyCamp = me.side === 'enemy'
    for (const f of order) {
      if (!f.pos || isOut(f) || (f.side === 'enemy') === meEnemyCamp) continue
      for (let dx = -1; dx <= 1; dx++) for (let dy = -1; dy <= 1; dy++) {
        const nx = f.pos.x + dx, ny = f.pos.y + dy
        if (nx >= 0 && nx < grid.cols && ny >= 0 && ny < grid.rows) threat.add(`${nx},${ny}`)
      }
    }
  }
  return (
    <div className="overflow-x-auto py-1">
      <div className="relative mx-auto" style={{
        width: grid.cols * CELL, height: grid.rows * CELL,
        display: 'grid',
        gridTemplateColumns: `repeat(${grid.cols}, ${CELL}px)`,
        gridTemplateRows: `repeat(${grid.rows}, ${CELL}px)`,
        backgroundImage: 'linear-gradient(var(--color-border) 1px, transparent 1px), linear-gradient(90deg, var(--color-border) 1px, transparent 1px)',
        backgroundSize: `${CELL}px ${CELL}px`,
        border: '1px solid var(--color-border-strong)',
        background: 'var(--color-bg-secondary)',
      }}>
        {(grid.blocked || []).map((k) => {
          const [x, y] = k.split(',').map(Number)
          return (
            <div key={`b${k}`} title="障碍（阻挡移动与视线）"
              style={{ gridColumn: x + 1, gridRow: y + 1, background: 'var(--color-bg-tertiary)',
                boxShadow: 'inset 0 0 0 1px var(--color-border-strong)',
                display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
              <GiBrickWall style={{ color: 'var(--color-text-secondary)', fontSize: CELL * 0.55 }} />
            </div>
          )
        })}
        {Object.entries(grid.cover || {}).map(([k, kind]) => {
          const [x, y] = k.split(',').map(Number)
          const full = kind === 'full'
          const CoverIcon = full ? GiShield : GiCrackedShield
          return (
            <div key={`c${k}`} title={full ? '全掩体（阻挡视线）' : '半掩体（射击 -1）'}
              style={{ gridColumn: x + 1, gridRow: y + 1, position: 'relative',
                background: `repeating-linear-gradient(45deg, color-mix(in srgb, var(--color-text-secondary) ${full ? 40 : 22}%, transparent) 0 3px, transparent 3px 6px)`,
                boxShadow: 'inset 0 0 0 1px color-mix(in srgb, var(--color-border-strong) 60%, transparent)' }}>
              {/* 掩体等级角标：全掩体完整盾 / 半掩体裂纹盾 */}
              <CoverIcon size={12} style={{
                position: 'absolute', right: 2, bottom: 2, opacity: 0.85,
                color: full ? 'var(--color-text-secondary)' : 'color-mix(in srgb, var(--color-text-secondary) 75%, transparent)',
              }} />
            </div>
          )
        })}
        {[...reach].map((k) => {
          const [x, y] = k.split(',').map(Number)
          return (
            <button key={`r${k}`} onClick={() => onCellMove(x, y)}
              title={threat.has(k) ? (dash ? '冲刺到此格（进入敌方近战范围、独占本回合）' : '移动到此格（进入敌方近战范围）')
                : (dash ? '冲刺到此格（独占本回合）' : '移动到此格')}
              style={{ gridColumn: x + 1, gridRow: y + 1, border: 'none', cursor: 'pointer',
                background: threat.has(k)
                  ? 'color-mix(in srgb, var(--color-danger) 26%, transparent)'
                  : dash
                    ? 'color-mix(in srgb, var(--color-danger) 13%, transparent)'
                    : 'color-mix(in srgb, var(--color-accent) 22%, transparent)' }} />
          )
        })}
        {/* 令牌层：绝对定位 + transform 补间，pos 变化即平滑滑动 */}
        {order.filter((c) => c.pos).map((c) => (
          <CombatToken
            key={c.id}
            c={c}
            isTurn={c.id === turn}
            isTarget={c.id === targetId}
            moveActive={moveActive}
            reduced={reduced}
            diff={fx[c.id]}
            dieSeq={deathFx[c.id]}
            onClick={() => onPieceClick(c)}
          />
        ))}
      </div>
    </div>
  )
}
