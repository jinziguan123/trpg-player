import { useMemo, useState, type CSSProperties } from 'react'
import { toast } from 'sonner'
import { api } from '../../api/client'
import { GiCrossedSwords, GiShield, GiRun, GiBrickWall } from 'react-icons/gi'

// 参战方状态：与后端枚举对齐。
type CombatStatus = 'ok' | 'major_wound' | 'dying' | 'unconscious' | 'dead' | 'fled'

interface Combatant {
  id: string
  name: string
  side: 'player' | 'ally' | 'enemy'
  is_human: boolean
  hp: number
  max_hp: number
  status: CombatStatus
}

export interface CombatState {
  round: number
  turn: string | null   // 当前轮到的参战方 id
  order: Combatant[]
}

// 反应提示：NPC 攻击某真人时后端暂停并广播 combat_reaction_prompt 的 metadata。
// allowed 为 ['fight_back','dodge']（近战）或 ['dodge','cover']（火器）。
export interface PendingReaction {
  attacker_id: string
  defender_id: string
  weapon: string
  ranged: boolean
  allowed: string[]
  attacker_name: string
  defender_name: string
}

// 反应按钮：图标全走 react-icons/gi（game-icons 风格），禁 emoji。
const REACTION_META: Record<string, { label: string; Icon: typeof GiCrossedSwords }> = {
  fight_back: { label: '反击', Icon: GiCrossedSwords },
  dodge: { label: '闪避', Icon: GiShield },
  cover: { label: '扑掩体', Icon: GiBrickWall },
}

// 状态徽标：正常（ok）不显示；其余各给中文标签与语义色。
const STATUS_META: Record<Exclude<CombatStatus, 'ok'>, { label: string; color: string }> = {
  major_wound: { label: '重伤', color: 'var(--color-danger)' },
  dying: { label: '濒死', color: 'var(--color-danger-deep)' },
  unconscious: { label: '昏迷', color: 'var(--color-text-secondary)' },
  dead: { label: '死亡', color: 'var(--color-danger-deep)' },
  fled: { label: '逃离', color: 'var(--color-text-secondary)' },
}

// 武器预设：与后端约定的口径一致；「其它(手填)」切换成自由文本输入。
const WEAPON_PRESETS = ['徒手格斗', '大棒(棒球棒、拨火棍)', '小型刀(弹簧折叠刀等)', '手枪', '猎枪']
const WEAPON_OTHER = '__other__'

// 死亡/逃离：该参战方已出局，格子灰掉、不可作为目标。
function isOut(c: Combatant): boolean {
  return c.status === 'dead' || c.status === 'fled'
}

export function CombatPanel({ combat, myCharId, sessionId, pendingReaction }: { combat: CombatState; myCharId: string | null; sessionId: string; pendingReaction?: PendingReaction | null }) {
  // 目标：存活敌方（非死亡/逃离）。默认选第一个。
  const enemies = useMemo(() => combat.order.filter((c) => c.side === 'enemy' && !isOut(c)), [combat.order])
  const [targetId, setTargetId] = useState<string>('')
  const [weaponSel, setWeaponSel] = useState<string>(WEAPON_PRESETS[0])
  const [weaponCustom, setWeaponCustom] = useState<string>('')
  const [submitting, setSubmitting] = useState(false)

  // 当前轮到谁：从 order 里找 turn 对应的参战方。
  const active = combat.order.find((c) => c.id === combat.turn) || null
  const myTurn = !!(active && myCharId && active.id === myCharId && active.is_human)

  // 有效目标：优先本地选择，回退到第一个存活敌方（列表变动时兜底）。
  const effectiveTarget = enemies.some((e) => e.id === targetId) ? targetId : (enemies[0]?.id ?? '')

  type ActionBody = { type: 'attack' | 'dodge' | 'fight_back' | 'flee' | 'other'; target_id?: string; weapon?: string; defense?: string }
  const submit = async (body: ActionBody) => {
    if (submitting) return
    setSubmitting(true)
    try {
      await api.post(`/sessions/${sessionId}/combat/action`, body)
      // 成功后不手动刷新——后端会经 /live 广播新 combat_state。
    } catch (e: unknown) {
      toast.error(e instanceof Error ? e.message : '行动提交失败')
    } finally {
      // 短暂禁用防连点；广播的新状态会重置回合归属。
      setTimeout(() => setSubmitting(false), 600)
    }
  }

  const onAttack = () => {
    if (!effectiveTarget) { toast.error('没有可攻击的目标'); return }
    const weapon = weaponSel === WEAPON_OTHER ? weaponCustom.trim() : weaponSel
    if (!weapon) { toast.error('请填写武器'); return }
    void submit({ type: 'attack', target_id: effectiveTarget, weapon })
  }

  // 反应提示：NPC 攻击我时，提交 fight_back/dodge/cover。复用 submit 的防连点/错误提示模式。
  const reactionForMe = !!(pendingReaction && myCharId && pendingReaction.defender_id === myCharId)
  const reactAs = async (choice: string) => {
    if (submitting) return
    setSubmitting(true)
    try {
      await api.post(`/sessions/${sessionId}/combat/reaction`, { choice })
      // 成功后不手动刷新——后端 resolve_reaction 续跑会经 /live 广播新 combat_state。
    } catch (e: unknown) {
      toast.error(e instanceof Error ? e.message : '反应提交失败')
    } finally {
      setTimeout(() => setSubmitting(false), 600)
    }
  }

  return (
    <div className="card mx-3 mb-2 !px-3 !py-2.5">
      {/* 顶部：轮次 + 先攻序 */}
      <div className="flex items-center gap-2 mb-2">
        <GiCrossedSwords style={{ color: 'var(--color-danger)', fontSize: '1.05rem', flexShrink: 0 }} />
        <span className="text-sm font-semibold" style={{ color: 'var(--color-text-accent)' }}>战斗 · 第 {combat.round} 轮</span>
      </div>
      <div className="flex gap-2 overflow-x-auto pb-1">
        {combat.order.map((c) => {
          const out = isOut(c)
          const isActive = c.id === combat.turn
          const mine = !!(myCharId && c.id === myCharId)
          // 血条色：敌方危险血红，我方/友方琥珀强调色。
          const hpColor = c.side === 'enemy' ? 'var(--color-danger)' : 'var(--color-accent)'
          const pct = c.max_hp > 0 ? Math.max(0, Math.min(100, (c.hp / c.max_hp) * 100)) : 0
          const sm = c.status !== 'ok' ? STATUS_META[c.status] : null
          return (
            <div
              key={c.id}
              className="flex-shrink-0 rounded px-2.5 py-1.5"
              style={{
                minWidth: 128,
                opacity: out ? 0.45 : 1,
                background: isActive ? 'var(--color-bg-tertiary)' : 'var(--color-bg-secondary)',
                border: isActive ? '1px solid var(--color-accent)' : '1px solid var(--color-border)',
                boxShadow: isActive ? '0 0 10px rgba(212, 162, 78, 0.35)' : 'none',
              }}
              title={`${c.name} · ${c.hp}/${c.max_hp}`}
            >
              <div className="flex items-center gap-1 mb-1">
                <span className="text-xs font-semibold truncate" style={{ color: mine ? 'var(--color-text-accent)' : 'var(--color-text-primary)' }}>
                  {c.name}{mine ? '（我）' : ''}
                </span>
                {sm && (
                  <span className="text-[10px] px-1 rounded flex-shrink-0" style={{ color: sm.color, border: `1px solid ${sm.color}` }}>
                    {sm.label}
                  </span>
                )}
              </div>
              <div className="h-1.5 rounded-full overflow-hidden" style={{ background: 'var(--color-input-bg)' }}>
                <div className="stat-bar-fill h-full" style={{ width: `${pct}%`, background: hpColor }} />
              </div>
              <div className="text-[10px] mt-0.5 font-mono" style={{ color: 'var(--color-text-secondary)' }}>
                {c.hp}/{c.max_hp}
              </div>
            </div>
          )
        })}
      </div>

      {/* 底部：反应提示（被攻击时优先）/ 行动控件（仅轮到我时）/ 等待提示 */}
      <div className="mt-2 pt-2" style={{ borderTop: '1px solid var(--color-border)' }}>
        {pendingReaction ? (
          reactionForMe ? (
            <div className="flex flex-col gap-1.5">
              <span className="text-xs" style={{ color: 'var(--color-text-primary)' }}>
                {pendingReaction.attacker_name} 用 {pendingReaction.weapon} 攻击你，如何应对？
              </span>
              <div className="flex flex-wrap items-center gap-2">
                {pendingReaction.allowed.map((choice) => {
                  const meta = REACTION_META[choice]
                  if (!meta) return null
                  const { label, Icon } = meta
                  return (
                    <button
                      key={choice}
                      onClick={() => void reactAs(choice)}
                      disabled={submitting}
                      className={`${choice === 'fight_back' ? 'btn-primary' : 'btn-secondary'} text-xs !px-2.5 !py-1 flex items-center gap-1`}
                      style={submitting ? { opacity: 0.5 } : undefined}
                    >
                      <Icon size={13} /> {label}
                    </button>
                  )
                })}
              </div>
            </div>
          ) : (
            <div className="text-xs" style={{ color: 'var(--color-text-secondary)' }}>
              等待 {pendingReaction.defender_name} 反应…
            </div>
          )
        ) : myTurn ? (
          <div className="flex flex-wrap items-end gap-2">
            <label className="flex flex-col gap-0.5">
              <span className="text-[10px]" style={{ color: 'var(--color-text-secondary)' }}>目标</span>
              <select
                className="input !py-1 text-xs"
                value={effectiveTarget}
                onChange={(e) => setTargetId(e.target.value)}
                disabled={enemies.length === 0}
              >
                {enemies.length === 0 && <option value="">无存活敌方</option>}
                {enemies.map((e) => (
                  <option key={e.id} value={e.id}>{e.name}（{e.hp}/{e.max_hp}）</option>
                ))}
              </select>
            </label>
            <label className="flex flex-col gap-0.5">
              <span className="text-[10px]" style={{ color: 'var(--color-text-secondary)' }}>武器</span>
              <select
                className="input !py-1 text-xs"
                value={weaponSel}
                onChange={(e) => setWeaponSel(e.target.value)}
              >
                {WEAPON_PRESETS.map((w) => (
                  <option key={w} value={w}>{w}</option>
                ))}
                <option value={WEAPON_OTHER}>其它(手填)</option>
              </select>
            </label>
            {weaponSel === WEAPON_OTHER && (
              <input
                className="input !py-1 text-xs"
                style={{ width: 140 }}
                placeholder="填写武器…"
                value={weaponCustom}
                onChange={(e) => setWeaponCustom(e.target.value)}
              />
            )}
            <button
              onClick={onAttack}
              disabled={submitting}
              className="btn-primary text-xs !px-2.5 !py-1 flex items-center gap-1"
              style={submitting ? { opacity: 0.5 } : undefined}
            >
              <GiCrossedSwords size={13} /> 攻击
            </button>
            <button
              onClick={() => void submit({ type: 'dodge' })}
              disabled={submitting}
              className="btn-secondary text-xs !px-2.5 !py-1 flex items-center gap-1"
            >
              <GiShield size={13} /> 闪避
            </button>
            <button
              onClick={() => void submit({ type: 'flee' })}
              disabled={submitting}
              className="btn-secondary text-xs !px-2.5 !py-1 flex items-center gap-1"
              style={{ color: 'var(--color-danger)' }}
            >
              <GiRun size={13} /> 逃跑
            </button>
          </div>
        ) : (
          <div className="text-xs" style={{ color: 'var(--color-text-secondary)' }}>
            战斗中 · 轮到 {active?.name ?? '……'}
          </div>
        )}
      </div>
    </div>
  )
}
