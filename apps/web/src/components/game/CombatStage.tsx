import { useEffect, useMemo, useRef, useState } from 'react'
import { toast } from 'sonner'
import { api } from '../../api/client'
import {
  GiCrossedSwords, GiShield, GiRun, GiBrickWall, GiScrollUnfurled,
  GiFirstAidKit, GiBinoculars, GiGrab, GiBrokenAxe, GiAmmoBox, GiCrosshair,
  GiHandcuffs, GiDeathSkull, GiRollingDices, GiFlame, GiFireBottle,
} from 'react-icons/gi'
import { ChevronDown, ChevronRight } from 'lucide-react'

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
  weapon?: string         // 当前武器名（后端 order 投影透传）
  armor?: number          // 护甲值（每次物理伤害先扣它）
  conditions?: string[]   // 正交条件：grappled（被擒）/ disarmed（缴械）
  aim?: boolean           // 瞄准态（下一击加奖励骰）
  pos?: { x: number; y: number } | null   // 方格坐标
  mov?: number            // 移动力（格/轮）
  move_left?: number      // 本回合剩余移动预算
}

// 方格战场（尺寸 + 障碍/掩体）。MVP 只用 cols/rows/cell_m。
export interface CombatGridInfo {
  cols: number
  rows: number
  cell_m: number
  blocked?: string[]
  cover?: Record<string, string>
}

// 两段式投骰：真人攻击命中后，等该玩家亲自掷伤害。actor_id 为该掷骰的玩家。
export interface PendingRoll {
  actor_id: string
  kind: string          // 目前为 'damage'
  label: string         // 按钮/提示文案，如「投掷伤害（1D8+DB）」
  victim_id?: string
}

export interface CombatState {
  round: number
  turn: string | null   // 当前轮到的参战方 id
  order: Combatant[]
  started_seq?: number  // 本场战斗日志起点 seq：日志抽屉只收本场（seq>started_seq）的结算行
  pending_roll?: PendingRoll | null
  grid?: CombatGridInfo | null   // 方格战场
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

// 一条战斗日志（机械结算行）：由 GameSessionPage 从带 combat_log 的 chunk 分流而来。
export interface CombatLogEntry {
  id: string
  kind: 'dice' | 'system'
  content: string
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

// 条件徽标：被擒 / 缴械（gi 图标 + 中文），叠加渲染于卡片。
const CONDITION_META: Record<string, { label: string; Icon: typeof GiCrossedSwords }> = {
  grappled: { label: '被擒', Icon: GiHandcuffs },
  disarmed: { label: '缴械', Icon: GiBrokenAxe },
  burning: { label: '着火', Icon: GiFlame },
}

// 武器：拳头（徒手格斗）永远可选并置顶；其余从角色卡武器栏（system_data.weapons）来；
// 末尾「其它(手填)」切换成自由文本输入。UNARMED 与后端 resolve_weapon 的徒手口径一致。
const UNARMED = '徒手格斗'
const WEAPON_OTHER = '__other__'

// 主动动作元数据：图标 + 标签 + 目标类型（敌方/己方/无）。全部 gi 图标，已确认存在。
type ActionKey = 'attack' | 'first_aid' | 'observe' | 'grapple' | 'disarm' | 'reload' | 'aim' | 'extinguish' | 'flee'
const ACTIONS: Record<ActionKey, { label: string; Icon: typeof GiCrossedSwords; target: 'enemy' | 'ally' | 'none' }> = {
  attack: { label: '攻击', Icon: GiCrossedSwords, target: 'enemy' },
  first_aid: { label: '急救', Icon: GiFirstAidKit, target: 'ally' },
  observe: { label: '观察', Icon: GiBinoculars, target: 'none' },
  grapple: { label: '擒抱', Icon: GiGrab, target: 'enemy' },
  disarm: { label: '缴械', Icon: GiBrokenAxe, target: 'enemy' },
  reload: { label: '装填', Icon: GiAmmoBox, target: 'none' },
  aim: { label: '瞄准', Icon: GiCrosshair, target: 'none' },
  extinguish: { label: '灭火', Icon: GiFireBottle, target: 'none' },
  flee: { label: '逃跑', Icon: GiRun, target: 'none' },
}

// 死亡/逃离：该参战方已出局，格子灰掉、不可作为目标。
function isOut(c: Combatant): boolean {
  return c.status === 'dead' || c.status === 'fled'
}

function pctOf(c: Combatant): number {
  return c.max_hp > 0 ? Math.max(0, Math.min(100, (c.hp / c.max_hp) * 100)) : 0
}

// —— HP 变化动画驱动：记住上一帧各 id 的 hp，新态到达时 diff ——
// 返回每个 id 的 { delta, seq }：delta<0 掉血、>0 回血、0 无变化；seq 让同值连续变化也能重触发动画。
// 首次见到某 id 时只建基准、不产出 delta（防重连把满血误判为回血）。
function useHpDiff(order: Combatant[]): Record<string, { delta: number; seq: number }> {
  const prevHp = useRef<Map<string, number>>(new Map())
  const seqRef = useRef(0)
  const [diffs, setDiffs] = useState<Record<string, { delta: number; seq: number }>>({})

  useEffect(() => {
    const reduced = window.matchMedia?.('(prefers-reduced-motion: reduce)').matches === true
    const next: Record<string, { delta: number; seq: number }> = {}
    const seen = new Set<string>()
    for (const c of order) {
      seen.add(c.id)
      const before = prevHp.current.get(c.id)
      if (before === undefined) {
        // 首次见到：只建基准，不动画（重连首帧不误判为回血）。
        prevHp.current.set(c.id, c.hp)
        continue
      }
      if (!reduced && c.hp !== before) {
        seqRef.current += 1
        next[c.id] = { delta: c.hp - before, seq: seqRef.current }
      }
      prevHp.current.set(c.id, c.hp)
    }
    // 清掉已离场 id 的基准（避免同 id 复用时错乱）。
    for (const id of Array.from(prevHp.current.keys())) if (!seen.has(id)) prevHp.current.delete(id)
    if (Object.keys(next).length > 0) setDiffs((prev) => ({ ...prev, ...next }))
  }, [order])

  return diffs
}

// 单张参战方卡片：图标位省略（用状态色点区分阵营），名字 + HP 条/数字 + 状态/条件徽标 + 武器。
function CombatantCard({ c, mine, active, diff }: {
  c: Combatant
  mine: boolean
  active: boolean
  diff?: { delta: number; seq: number }
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

export function CombatStage({ combat, myCharId, sessionId, pendingReaction, log, myWeapons = [] }: {
  combat: CombatState
  myCharId: string | null
  sessionId: string
  pendingReaction?: PendingReaction | null
  log: CombatLogEntry[]
  myWeapons?: { name: string; dam?: string }[]
}) {
  const order = combat.order
  const diffs = useHpDiff(order)

  // 分栏：己方（player/ally）与敌方（enemy）。
  const allies = useMemo(() => order.filter((c) => c.side !== 'enemy'), [order])
  const enemies = useMemo(() => order.filter((c) => c.side === 'enemy'), [order])
  // 可作为目标的存活候选（按动作切己方/敌方）。
  const enemyTargets = useMemo(() => enemies.filter((c) => !isOut(c)), [enemies])
  const woundedAllies = useMemo(
    () => allies.filter((c) => !isOut(c) && (c.hp < c.max_hp || c.status !== 'ok')),
    [allies],
  )

  // 当前轮到谁 + 是否轮到我。
  const active = order.find((c) => c.id === combat.turn) || null
  const me = myCharId ? order.find((c) => c.id === myCharId) || null : null
  const myTurn = !!(active && myCharId && active.id === myCharId && active.is_human)
  const iAmGrappled = !!(me && (me.conditions || []).includes('grappled'))
  const iAmBurning = !!(me && (me.conditions || []).includes('burning'))

  const [action, setAction] = useState<ActionKey>('attack')
  const [targetId, setTargetId] = useState<string>('')
  const [weaponSel, setWeaponSel] = useState<string>(UNARMED)
  const [weaponCustom, setWeaponCustom] = useState<string>('')
  const [fireMode, setFireMode] = useState<'single' | 'burst' | 'sweep'>('single')   // 连发射击模式
  const [submitting, setSubmitting] = useState(false)
  const [collapsed, setCollapsed] = useState(false)
  const [logOpen, setLogOpen] = useState(false)
  const [moveMode, setMoveMode] = useState(false)   // 方格移动模式（高亮可达格、点格移动）
  useEffect(() => { setMoveMode(false) }, [combat.turn])   // 回合切换 → 退出移动模式

  // 武器下拉项：拳头置顶（永远有）+ 角色卡武器栏（去重、保留伤害提示）+ 其它(手填)。
  const weaponOptions = useMemo(() => {
    const seen = new Set<string>([UNARMED])
    const opts: { value: string; label: string }[] = [{ value: UNARMED, label: UNARMED }]
    for (const w of myWeapons) {
      const name = (w.name || '').trim()
      if (!name || seen.has(name)) continue
      seen.add(name)
      opts.push({ value: name, label: w.dam ? `${name}（${w.dam}）` : name })
    }
    return opts
  }, [myWeapons])
  // 角色卡换人/武器栏变化后，若当前所选武器已不在列表则回落拳头。
  useEffect(() => {
    if (weaponSel !== WEAPON_OTHER && !weaponOptions.some((o) => o.value === weaponSel)) {
      setWeaponSel(UNARMED)
    }
  }, [weaponOptions, weaponSel])

  // 当前动作的目标候选（按 target 类型）。
  const actionMeta = ACTIONS[action]
  const candidates = actionMeta.target === 'enemy' ? enemyTargets : actionMeta.target === 'ally' ? woundedAllies : []
  const effectiveTarget = candidates.some((c) => c.id === targetId) ? targetId : (candidates[0]?.id ?? '')

  type ActionBody = { type: string; target_id?: string; weapon?: string; kind?: string; defense?: string; shots?: string[] }
  const curWeapon = weaponSel === WEAPON_OTHER ? weaponCustom.trim() : weaponSel
  // 火器名匹配（连射仅火器可用；最终由后端按武器射速 round 校验、非火器自动降级单发）
  const isFirearm = /枪|步枪|左轮|冲锋|自动手枪|马格南|来复|卡宾|霰弹|散弹|沙漠之鹰|格洛克|贝瑞塔|鲁格/.test(curWeapon)
  const submit = async (body: ActionBody) => {
    if (submitting) return
    setSubmitting(true)
    try {
      await api.post(`/sessions/${sessionId}/combat/action`, body)
      // 成功后不手动刷新——后端会经 /live 广播新 combat_state。
    } catch (e: unknown) {
      toast.error(e instanceof Error ? e.message : '行动提交失败')
    } finally {
      setTimeout(() => setSubmitting(false), 600)   // 防连点；广播的新态会重置回合归属
    }
  }

  const runAction = () => {
    if (actionMeta.target !== 'none' && !effectiveTarget) {
      toast.error(actionMeta.target === 'enemy' ? '没有可选的目标' : '没有需要处理的己方目标')
      return
    }
    switch (action) {
      case 'attack': {
        if (!curWeapon) { toast.error('请填写武器'); return }
        if (isFirearm && fireMode === 'burst') {
          // 对同一目标连开 3 发（同目标不加惩罚骰）
          void submit({ type: 'attack', weapon: curWeapon, shots: [effectiveTarget, effectiveTarget, effectiveTarget] })
        } else if (isFirearm && fireMode === 'sweep' && candidates.length >= 2) {
          // 扫射：每个存活敌人 1 发（换目标累加惩罚骰）
          void submit({ type: 'attack', weapon: curWeapon, shots: candidates.map((c) => c.id) })
        } else {
          void submit({ type: 'attack', target_id: effectiveTarget, weapon: curWeapon })
        }
        break
      }
      case 'first_aid':
        void submit({ type: 'first_aid', target_id: effectiveTarget })
        break
      case 'observe':
        void submit({ type: 'observe' })
        break
      case 'grapple':
        void submit({ type: 'maneuver', target_id: effectiveTarget, kind: 'grapple' })
        break
      case 'disarm':
        void submit({ type: 'maneuver', target_id: effectiveTarget, kind: 'disarm' })
        break
      case 'reload':
        void submit({ type: 'reload' })
        break
      case 'aim':
        void submit({ type: 'aim' })
        break
      case 'extinguish':
        void submit({ type: 'extinguish' })
        break
      case 'flee':
        void submit({ type: 'flee' })
        break
    }
  }

  // 方格移动：点可达格 → 提交移动（不推进先攻），成功后退出移动模式。
  const doMove = async (x: number, y: number) => {
    if (submitting) return
    setSubmitting(true)
    try {
      await api.post(`/sessions/${sessionId}/combat/action`, { type: 'move', dest: { x, y } })
      setMoveMode(false)
    } catch (e: unknown) {
      toast.error(e instanceof Error ? e.message : '移动失败')
    } finally {
      setTimeout(() => setSubmitting(false), 400)
    }
  }
  // 点棋子：非移动模式下点敌方棋子 = 选为攻击目标（与目标下拉双向同步）。
  const onPieceClick = (c: Combatant) => {
    if (moveMode) return
    if (c.side === 'enemy' && !isOut(c)) { setAction('attack'); setTargetId(c.id) }
  }

  // 两段式投骰：我攻击命中后要亲自掷伤害。
  const pendingRoll = combat.pending_roll || null
  const rollForMe = !!(pendingRoll && myCharId && pendingRoll.actor_id === myCharId)
  const rollActorName = pendingRoll ? (order.find((c) => c.id === pendingRoll.actor_id)?.name ?? '……') : ''
  const rollDamage = async () => {
    if (submitting) return
    setSubmitting(true)
    try {
      await api.post(`/sessions/${sessionId}/combat/roll`, {})
    } catch (e: unknown) {
      toast.error(e instanceof Error ? e.message : '投掷失败')
    } finally {
      setTimeout(() => setSubmitting(false), 600)
    }
  }

  // 反应提示：NPC 攻击我时，提交 fight_back/dodge/cover。
  const reactionForMe = !!(pendingReaction && myCharId && pendingReaction.defender_id === myCharId)
  const reactAs = async (choice: string) => {
    if (submitting) return
    setSubmitting(true)
    try {
      await api.post(`/sessions/${sessionId}/combat/reaction`, { choice })
    } catch (e: unknown) {
      toast.error(e instanceof Error ? e.message : '反应提交失败')
    } finally {
      setTimeout(() => setSubmitting(false), 600)
    }
  }


  return (
    <div className="card mx-3 mb-2 !px-3 !py-2.5">
      {/* 顶部：轮次 + 收起/展开 */}
      <div className="flex items-center justify-between gap-2 mb-2">
        <div className="flex items-center gap-2 min-w-0">
          <GiCrossedSwords style={{ color: 'var(--color-danger)', fontSize: '1.05rem', flexShrink: 0 }} />
          <span className="text-sm font-semibold flex-shrink-0" style={{ color: 'var(--color-text-accent)' }}>战斗 · 第 {combat.round} 轮</span>
          {collapsed && (
            <span className="text-xs truncate" style={{ color: 'var(--color-text-secondary)' }}>· 轮到 {active?.name ?? '……'}</span>
          )}
          {collapsed && (myTurn || rollForMe || reactionForMe) && (
            <span className="text-[10px] px-1 rounded flex-shrink-0" style={{ color: 'var(--color-text-accent)', border: '1px solid var(--color-accent)' }}>待你操作</span>
          )}
        </div>
        <button
          onClick={() => setCollapsed((v) => !v)}
          className="flex-shrink-0"
          style={{ color: 'var(--color-text-secondary)' }}
          title={collapsed ? '展开战斗面板' : '收起战斗面板'}
        >
          {collapsed ? <ChevronRight size={16} /> : <ChevronDown size={16} />}
        </button>
      </div>
      {!collapsed && (<>
      {/* B1 先攻轨：横排，高亮当前、标下一个、走过者淡化 */}
      <InitiativeTrack order={order} turn={combat.turn} myCharId={myCharId} />

      {/* B1.5 方格战场：棋子 + 移动。移动模式仅本人回合可用。 */}
      {combat.grid && (
        <div className="mt-2">
          <div className="flex items-center justify-between mb-0.5">
            <span className="text-[10px] uppercase tracking-wide" style={{ color: 'var(--color-text-secondary)' }}>战场</span>
            {myTurn && (
              <button
                onClick={() => setMoveMode((v) => !v)}
                disabled={submitting}
                className="text-[11px] px-2 py-0.5 rounded inline-flex items-center gap-1"
                style={{
                  border: `1px solid ${moveMode ? 'var(--color-accent)' : 'var(--color-border-strong)'}`,
                  color: moveMode ? 'var(--color-text-accent)' : 'var(--color-text-secondary)',
                  ...(submitting ? { opacity: 0.5 } : {}),
                }}
              >
                <GiRun size={12} /> {moveMode ? `移动中（剩 ${me?.move_left ?? 0} 格）` : '移动'}
              </button>
            )}
          </div>
          <CombatGrid
            grid={combat.grid}
            order={order}
            turn={combat.turn}
            myCharId={myCharId}
            moveMode={moveMode}
            targetId={effectiveTarget}
            onCellMove={doMove}
            onPieceClick={onPieceClick}
          />
        </div>
      )}

      {/* B2 两栏参战方卡片（左己方 / 右敌方），带 HP 动画（B3） */}
      <div className="grid grid-cols-2 gap-2 mt-2">
        <div className="flex flex-col gap-1.5">
          <div className="text-[10px] uppercase tracking-wide" style={{ color: 'var(--color-text-secondary)' }}>我方</div>
          {allies.length === 0
            ? <div className="text-[11px]" style={{ color: 'var(--color-text-secondary)' }}>无</div>
            : allies.map((c) => (
              <CombatantCard key={c.id} c={c} mine={!!(myCharId && c.id === myCharId)} active={c.id === combat.turn} diff={diffs[c.id]} />
            ))}
        </div>
        <div className="flex flex-col gap-1.5">
          <div className="text-[10px] uppercase tracking-wide text-right" style={{ color: 'var(--color-text-secondary)' }}>敌方</div>
          {enemies.length === 0
            ? <div className="text-[11px] text-right" style={{ color: 'var(--color-text-secondary)' }}>无</div>
            : enemies.map((c) => (
              <CombatantCard key={c.id} c={c} mine={false} active={c.id === combat.turn} diff={diffs[c.id]} />
            ))}
        </div>
      </div>

      {/* B4 反应提示 / B5 主动动作栏 / 等待提示 */}
      <div className="mt-2 pt-2" style={{ borderTop: '1px solid var(--color-border)' }}>
        {pendingRoll ? (
          rollForMe ? (
            <div className="flex flex-wrap items-center gap-2">
              <span className="text-xs" style={{ color: 'var(--color-text-primary)' }}>
                命中！{pendingRoll.label || '投掷伤害'}
              </span>
              <button
                onClick={() => void rollDamage()}
                disabled={submitting}
                className="btn-primary text-xs !px-2.5 !py-1 flex items-center gap-1"
                style={submitting ? { opacity: 0.5 } : undefined}
              >
                <GiRollingDices size={13} /> {pendingRoll.label || '投掷伤害'}
              </button>
            </div>
          ) : (
            <div className="text-xs" style={{ color: 'var(--color-text-secondary)' }}>
              等待 {rollActorName} 投掷伤害…
            </div>
          )
        ) : pendingReaction ? (
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
          <div className="flex flex-col gap-2">
            {/* 动作选择：一排 gi 图标按钮 */}
            <div className="flex flex-wrap items-center gap-1.5">
              {(Object.keys(ACTIONS) as ActionKey[]).map((k) => {
                // 被擒抱时隐藏逃跑（无效）：擒抱状态下逃跑要先挣脱，暂不给该入口。
                if (k === 'flee' && iAmGrappled) return null
                // 灭火仅在自己着火时出现（否则是无效动作）。
                if (k === 'extinguish' && !iAmBurning) return null
                const { label, Icon } = ACTIONS[k]
                const on = action === k
                return (
                  <button
                    key={k}
                    onClick={() => setAction(k)}
                    className="text-xs !px-2 !py-1 rounded inline-flex items-center gap-1 transition-colors"
                    style={{
                      border: on ? '1px solid var(--color-accent)' : '1px solid var(--color-border)',
                      color: on ? 'var(--color-text-accent)' : 'var(--color-text-secondary)',
                      background: on ? 'var(--color-bg-tertiary)' : 'transparent',
                    }}
                  >
                    <Icon size={13} /> {label}
                  </button>
                )
              })}
            </div>
            {/* 参数：目标（按动作切候选）+ 武器（仅攻击） */}
            <div className="flex flex-wrap items-end gap-2">
              {actionMeta.target !== 'none' && (
                <label className="flex flex-col gap-1">
                  <span className="combat-field-label">
                    {actionMeta.target === 'enemy' ? '目标（敌方）' : '目标（己方）'}
                  </span>
                  <div className="combat-select-wrap">
                    <select
                      className="combat-select"
                      value={effectiveTarget}
                      onChange={(e) => setTargetId(e.target.value)}
                      disabled={candidates.length === 0}
                    >
                      {candidates.length === 0 && <option value="">{actionMeta.target === 'enemy' ? '无存活敌方' : '无需处理'}</option>}
                      {candidates.map((c) => (
                        <option key={c.id} value={c.id}>{c.name}（{c.hp}/{c.max_hp}）</option>
                      ))}
                    </select>
                    <ChevronDown className="combat-select-caret" size={13} />
                  </div>
                </label>
              )}
              {action === 'attack' && (
                <>
                  <label className="flex flex-col gap-1">
                    <span className="combat-field-label">武器</span>
                    <div className="combat-select-wrap">
                      <select
                        className="combat-select"
                        value={weaponSel}
                        onChange={(e) => setWeaponSel(e.target.value)}
                      >
                        {weaponOptions.map((o) => (<option key={o.value} value={o.value}>{o.label}</option>))}
                        <option value={WEAPON_OTHER}>其它（手填）</option>
                      </select>
                      <ChevronDown className="combat-select-caret" size={13} />
                    </div>
                  </label>
                  {weaponSel === WEAPON_OTHER && (
                    <input
                      className="input !py-1 text-xs"
                      style={{ width: 140 }}
                      placeholder="填写武器…"
                      value={weaponCustom}
                      onChange={(e) => setWeaponCustom(e.target.value)}
                      autoFocus
                    />
                  )}
                  {isFirearm && (
                    <label className="flex flex-col gap-1">
                      <span className="combat-field-label">射击</span>
                      <div className="combat-select-wrap">
                        <select
                          className="combat-select"
                          value={fireMode}
                          onChange={(e) => setFireMode(e.target.value as 'single' | 'burst' | 'sweep')}
                        >
                          <option value="single">单发</option>
                          <option value="burst">连射3发（同目标）</option>
                          <option value="sweep" disabled={candidates.length < 2}>扫射（每敌1发）</option>
                        </select>
                        <ChevronDown className="combat-select-caret" size={13} />
                      </div>
                    </label>
                  )}
                </>
              )}
              <button
                onClick={runAction}
                disabled={submitting}
                className={`${action === 'flee' ? 'btn-secondary' : 'btn-primary'} text-xs !px-2.5 !py-1 flex items-center gap-1`}
                style={{
                  ...(submitting ? { opacity: 0.5 } : {}),
                  ...(action === 'flee' ? { color: 'var(--color-danger)' } : {}),
                }}
              >
                <actionMeta.Icon size={13} /> {actionMeta.label}
              </button>
            </div>
          </div>
        ) : (
          <div className="text-xs" style={{ color: 'var(--color-text-secondary)' }}>
            战斗中 · 轮到 {active?.name ?? '……'}
          </div>
        )}
      </div>

      {/* B6 折叠战斗日志抽屉 */}
      {log.length > 0 && (
        <div className="mt-2 pt-2" style={{ borderTop: '1px solid var(--color-border)' }}>
          <button
            onClick={() => setLogOpen((v) => !v)}
            className="flex items-center gap-1.5 text-xs w-full"
            style={{ color: 'var(--color-text-secondary)' }}
            title={logOpen ? '收起战斗日志' : '展开战斗日志'}
          >
            {logOpen ? <ChevronDown size={13} /> : <ChevronRight size={13} />}
            <GiScrollUnfurled size={13} style={{ color: 'var(--color-text-accent)' }} />
            <span style={{ color: 'var(--color-text-accent)' }}>战斗日志 ({log.length})</span>
          </button>
          {logOpen && (
            <div className="mt-1.5 flex flex-col gap-1 max-h-56 overflow-y-auto chat-scroll">
              {log.map((e) => (
                <div
                  key={e.id}
                  className="text-[11px] px-2 py-1 rounded"
                  style={{
                    background: 'var(--color-bg-secondary)',
                    border: '1px solid var(--color-border)',
                    color: e.kind === 'dice' ? 'var(--color-dice-gold)' : 'var(--color-text-secondary)',
                    fontFamily: e.kind === 'dice' ? 'var(--font-body)' : undefined,
                  }}
                >
                  {e.content}
                </div>
              ))}
            </div>
          )}
        </div>
      )}
      </>)}
    </div>
  )
}

// B1 先攻轨：横向排 order，高亮当前、标下一个、走过者淡化。
function InitiativeTrack({ order, turn, myCharId }: { order: Combatant[]; turn: string | null; myCharId: string | null }) {
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
        const dot = c.side === 'enemy' ? 'var(--color-danger)' : 'var(--color-accent)'
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

// 方格战场：CSS-grid 棋盘 + 棋子（阵营色/当前行动者外发光/出局灰化/选中目标描边）。
// 移动模式下高亮可达格（Chebyshev ≤ 剩余移动力、避开占用/障碍），点格移动；点敌方棋子选目标。
// 纯 --color-* 变量，gothic/parchment 两主题自适应，不引第三方战棋库。
function CombatGrid({ grid, order, turn, myCharId, moveMode, targetId, onCellMove, onPieceClick }: {
  grid: CombatGridInfo
  order: Combatant[]
  turn: string | null
  myCharId: string | null
  moveMode: boolean
  targetId: string
  onCellMove: (x: number, y: number) => void
  onPieceClick: (c: Combatant) => void
}) {
  const CELL = 30
  const me = myCharId ? order.find((c) => c.id === myCharId) : null
  const occupied = new Set(order.filter((c) => c.pos && !isOut(c)).map((c) => `${c.pos!.x},${c.pos!.y}`))
  const blocked = new Set(grid.blocked || [])
  const reach = new Set<string>()
  const threat = new Set<string>()   // 与存活敌方相邻的格：移动到此会进入近战/被夹击
  if (moveMode && me?.pos && (me.move_left ?? 0) > 0) {
    const b = me.move_left ?? 0
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
              <GiBrickWall style={{ color: 'var(--color-text-secondary)', fontSize: CELL * 0.6 }} />
            </div>
          )
        })}
        {Object.entries(grid.cover || {}).map(([k, kind]) => {
          const [x, y] = k.split(',').map(Number)
          const full = kind === 'full'
          return (
            <div key={`c${k}`} title={full ? '全掩体（阻挡视线）' : '半掩体（射击 -1）'}
              style={{ gridColumn: x + 1, gridRow: y + 1,
                background: `repeating-linear-gradient(45deg, color-mix(in srgb, var(--color-text-secondary) ${full ? 40 : 22}%, transparent) 0 3px, transparent 3px 6px)`,
                boxShadow: 'inset 0 0 0 1px color-mix(in srgb, var(--color-border-strong) 60%, transparent)' }} />
          )
        })}
        {[...reach].map((k) => {
          const [x, y] = k.split(',').map(Number)
          return (
            <button key={`r${k}`} onClick={() => onCellMove(x, y)}
              title={threat.has(k) ? '移动到此格（进入敌方近战范围）' : '移动到此格'}
              style={{ gridColumn: x + 1, gridRow: y + 1, border: 'none', cursor: 'pointer',
                background: threat.has(k)
                  ? 'color-mix(in srgb, var(--color-danger) 26%, transparent)'
                  : 'color-mix(in srgb, var(--color-accent) 22%, transparent)' }} />
          )
        })}
        {order.filter((c) => c.pos).map((c) => {
          const out = isOut(c)
          const col = c.side === 'enemy' ? 'var(--color-danger)' : 'var(--color-accent)'
          const isTurn = c.id === turn
          const isTarget = c.id === targetId
          return (
            <button key={c.id} onClick={() => onPieceClick(c)} title={`${c.name} ${c.hp}/${c.max_hp}`}
              style={{ position: 'absolute', left: c.pos!.x * CELL + 3, top: c.pos!.y * CELL + 3,
                width: CELL - 6, height: CELL - 6, borderRadius: '50%', padding: 0,
                background: 'var(--color-bg-tertiary)', color: 'var(--color-text-primary)',
                border: `2px solid ${col}`, opacity: out ? 0.4 : 1, filter: out ? 'grayscale(1)' : 'none',
                boxShadow: isTurn ? `0 0 7px ${col}` : isTarget ? '0 0 0 2px var(--color-danger)' : 'none',
                cursor: 'pointer', fontSize: '0.68rem', lineHeight: 1,
                display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
              {c.name.slice(0, 1)}
            </button>
          )
        })}
      </div>
    </div>
  )
}
