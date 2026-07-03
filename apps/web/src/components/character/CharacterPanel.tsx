import { useState, useRef, useEffect, type ReactNode } from 'react'
import { RadarChart } from './RadarChart'
import { Tabs, TabsList, TabsTrigger, TabsContent } from '@/components/ui/tabs'
import { ConfirmDialog } from '../ui/confirm-dialog'

/** 模糊匹配：先看子串，再退化为「按顺序出现的子序列」（如「图使」命中「图书馆使用」）。 */
function fuzzyMatch(query: string, target: string): boolean {
  const q = query.toLowerCase()
  const t = target.toLowerCase()
  if (!q) return true
  if (t.includes(q)) return true
  let i = 0
  for (const ch of t) {
    if (ch === q[i]) i++
    if (i >= q.length) return true
  }
  return false
}

interface CharacterData {
  id: string
  name: string
  base_attributes: Record<string, number>
  skills: Record<string, number>
  system_data: Record<string, unknown>
  backstory: string
  status: string
}

interface CharacterPanelProps {
  character: CharacterData
  /** 提供时（在场、查看自己的卡）技能可点击「申请检定」——难度由 KP 裁定，玩家不指定。
   * intent 是玩家顺带说明的检定目标（可选，如「书桌暗格」），场景里线索不止一处时帮 KP 判断具体目标 */
  onSkillCheck?: (skill: string, intent: string) => void
}

const ATTR_LABELS: Record<string, string> = {
  STR: '力量', CON: '体质', SIZ: '体型', DEX: '敏捷',
  APP: '外貌', INT: '智力', POW: '意志', EDU: '教育',
}

const RADAR_KEYS = ['STR', 'DEX', 'POW', 'CON', 'APP', 'EDU', 'SIZ', 'INT', 'LUK']
const RADAR_LABELS = ['力量', '敏捷', '意志', '体质', '外貌', '教育', '体型', '智力', '幸运']

const TAB_KEYS = ['基本信息', '技能', '道具', '档案'] as const

const STATUS_LABEL: Record<string, string> = {
  active: '正常', major_wound: '重伤', unconscious: '昏迷', dead: '死亡',
  temporary_insanity: '临时疯狂', indefinite_insanity: '不定期疯狂', permanent_insanity: '永久疯狂',
  incapacitated: '重伤',
}
// 非正常状态用醒目色
const STATUS_DANGER = new Set(['major_wound', 'unconscious', 'dead', 'temporary_insanity', 'indefinite_insanity', 'permanent_insanity', 'incapacitated'])

const BACKSTORY_SECTIONS: { key: string; label: string }[] = [
  { key: 'personalDescription', label: '个人描述' },
  { key: 'ideologyBeliefs', label: '思想/信念' },
  { key: 'significantPeople', label: '重要之人' },
  { key: 'meaningfulLocations', label: '意义非凡之地' },
  { key: 'treasuredPossessions', label: '宝贵之物' },
  { key: 'traits', label: '特点' },
]

function StatBar({ label, current, max }: { label: string; current: number; max: number }) {
  const pct = max > 0 ? (current / max) * 100 : 0
  const isLow = pct < 30
  return (
    <div className="mb-2">
      <div className="flex justify-between text-xs mb-0.5">
        <span>{label}</span>
        <span className="font-mono">{current}/{max}</span>
      </div>
      <div className="h-1.5 rounded-full" style={{ background: 'var(--color-bg-tertiary)' }}>
        <div
          className="h-full rounded-full stat-bar-fill"
          style={{
            width: `${pct}%`,
            background: isLow ? 'var(--color-danger)' : 'var(--color-accent)',
          }}
        />
      </div>
    </div>
  )
}

function InfoRow({ label, value }: { label: string; value: string | number }) {
  if (!value && value !== 0) return null
  return (
    <div className="flex justify-between text-xs py-0.5">
      <span style={{ color: 'var(--color-text-secondary)' }}>{label}</span>
      <span className="font-mono">{value}</span>
    </div>
  )
}

/** 监听 SAN/HP 变化，返回一次性反馈 class：
 *  SAN 大额降（≥5）→ 血红闪边；任意值回升 → 琥珀微光；轻微降/不变 → 无。
 *  首帧（挂载）不触发，避免打开面板即闪。用角色 id 变更时重置基线，切人不误报。 */
function useVitalFlash(charId: string, san?: number, hp?: number): string {
  const [flash, setFlash] = useState('')
  const prev = useRef<{ id: string; san?: number; hp?: number } | null>(null)
  useEffect(() => {
    const last = prev.current
    prev.current = { id: charId, san, hp }
    if (!last || last.id !== charId) return   // 首帧 / 切换角色：只记基线
    const sanDrop = last.san != null && san != null ? last.san - san : 0
    const hpDrop = last.hp != null && hp != null ? last.hp - hp : 0
    const sanUp = last.san != null && san != null && san > last.san
    const hpUp = last.hp != null && hp != null && hp > last.hp
    if (sanDrop >= 5) setFlash('panel-san-drop')
    else if (hpDrop >= 5) setFlash('panel-san-drop')
    else if (sanUp || hpUp) setFlash('panel-restore')
    else return
    const t = setTimeout(() => setFlash(''), 750)
    return () => clearTimeout(t)
  }, [charId, san, hp])
  return flash
}

function BasicInfoTab({ character }: { character: CharacterData }) {
  const sd = character.system_data || {}
  const hp = sd.hitPoints as { current: number; max: number } | undefined
  const san = sd.sanity as { current: number; max: number } | undefined
  const mp = sd.magicPoints as { current: number; max: number } | undefined
  const vitalFlash = useVitalFlash(character.id, san?.current, hp?.current)
  const luck = (sd.luck as number) || 0
  const mov = (sd.move as number) || 0
  const occupation = (sd.occupation as string) || ''
  const age = (sd.age as number) || 0
  const gender = (sd.gender as string) || ''
  const residence = (sd.residence as string) || ''
  const birthplace = (sd.birthplace as string) || ''
  const creditRating = (sd.creditRating as number) || character.skills?.['信用评级'] || 0
  const damageBonus = (sd.damageBonus as string) || '0'
  const build = sd.build as number
  const dodge = character.skills?.['闪避'] || 0

  const radarValues = RADAR_KEYS.map((k) =>
    k === 'LUK' ? luck : (character.base_attributes[k] || 0)
  )

  const hasBackstorySections = BACKSTORY_SECTIONS.some((s) => sd[s.key])

  return (
    <div className="space-y-3">
      <div className="text-center">
        <div
          className="w-16 h-16 mx-auto rounded-full flex items-center justify-center text-xl mb-1.5"
          style={{ background: 'var(--color-bg-tertiary)', color: 'var(--color-text-secondary)' }}
        >
          {character.name.charAt(0)}
        </div>
        <h3 className="font-semibold text-base" style={{ color: 'var(--color-text-accent)', fontFamily: 'var(--font-title)' }}>
          {character.name}
        </h3>
        <div className="text-xs space-x-2" style={{ color: 'var(--color-text-secondary)' }}>
          {occupation && <span>{occupation}</span>}
          {gender && <span>{gender}</span>}
          {age > 0 && <span>{age}岁</span>}
        </div>
        {character.status && character.status !== 'active' && (
          <div className="mt-1">
            <span
              className="text-xs px-2 py-0.5 rounded"
              style={{
                background: STATUS_DANGER.has(character.status) ? 'var(--color-danger-deep)' : 'var(--color-bg-tertiary)',
                color: STATUS_DANGER.has(character.status) ? 'var(--color-on-danger)' : 'var(--color-text-secondary)',
              }}
            >
              {STATUS_LABEL[character.status] || character.status}
            </span>
          </div>
        )}
      </div>

      <div className={`rounded ${vitalFlash}`} style={{ padding: '2px' }}>
        {hp && <StatBar label="HP" current={hp.current} max={hp.max} />}
        {san && <StatBar label="SAN" current={san.current} max={san.max} />}
        {mp && <StatBar label="MP" current={mp.current} max={mp.max} />}
      </div>

      <div className="rounded p-2 space-y-0.5" style={{ background: 'var(--color-bg-tertiary)' }}>
        <InfoRow label="幸运" value={luck} />
        <InfoRow label="移动力" value={mov} />
        <InfoRow label="伤害加值" value={damageBonus} />
        {build !== undefined && <InfoRow label="体格" value={build} />}
        <InfoRow label="闪避" value={dodge} />
        <InfoRow label="信用评级" value={creditRating} />
      </div>

      {(residence || birthplace) && (
        <div className="rounded p-2 space-y-0.5" style={{ background: 'var(--color-bg-tertiary)' }}>
          {residence && <InfoRow label="居住地" value={residence} />}
          {birthplace && <InfoRow label="出生地" value={birthplace} />}
        </div>
      )}

      <div className="flex justify-center">
        <RadarChart labels={RADAR_LABELS} values={radarValues} size={180} />
      </div>

      <div className="grid grid-cols-4 gap-1 text-center text-xs">
        {Object.entries(ATTR_LABELS).map(([k, label]) => (
          <div key={k} className="py-1 rounded" style={{ background: 'var(--color-bg-tertiary)' }}>
            <div style={{ color: 'var(--color-text-secondary)', fontSize: '0.65rem' }}>{label}</div>
            <div className="font-mono font-bold">{character.base_attributes[k] || 0}</div>
          </div>
        ))}
      </div>

      {hasBackstorySections ? (
        <div className="space-y-2">
          {BACKSTORY_SECTIONS.map(({ key, label }) => {
            const val = sd[key] as string | undefined
            if (!val) return null
            return (
              <div key={key}>
                <h4 className="text-xs font-semibold mb-0.5" style={{ color: 'var(--color-text-accent)' }}>{label}</h4>
                <p className="text-xs leading-relaxed" style={{ color: 'var(--color-text-secondary)' }}>{val}</p>
              </div>
            )
          })}
        </div>
      ) : character.backstory ? (
        <div>
          <h4 className="text-xs font-semibold mb-0.5" style={{ color: 'var(--color-text-accent)' }}>背景故事</h4>
          <p className="text-xs leading-relaxed whitespace-pre-wrap" style={{ color: 'var(--color-text-secondary)' }}>
            {character.backstory}
          </p>
        </div>
      ) : null}
    </div>
  )
}

/** 单个技能行：自己持有「检定目标」输入框的状态，互不影响。 */
function SkillCheckRow({
  name, value, onSkillCheck,
}: { name: string; value: number; onSkillCheck: (skill: string, intent: string) => void }) {
  const [intent, setIntent] = useState('')
  const row = (
    <>
      <span>{name}</span>
      <span className="font-mono font-bold" style={{
        color: value >= 50 ? 'var(--color-success)' : value >= 25 ? 'var(--color-text-primary)' : 'var(--color-text-secondary)',
      }}>
        {value}
      </span>
    </>
  )
  return (
    <ConfirmDialog
      title="申请检定"
      description={`就「${name}」（当前值 ${value}）向 KP 申请检定？难度由 KP 据情境裁定，随后你再投骰。`}
      confirmLabel="申请"
      onConfirm={() => {
        onSkillCheck(name, intent.trim())
        setIntent('')
      }}
      extra={
        <textarea
          value={intent}
          onChange={(e) => setIntent(e.target.value)}
          placeholder="（可选）想对什么做检定？如「书桌暗格」「他刚才那句话」——现场不止一处线索时能帮 KP 判断具体目标"
          rows={2}
          className="w-full px-2 py-1 rounded text-xs resize-none"
          style={{ background: 'var(--color-bg-tertiary)', border: '1px solid var(--color-border)' }}
        />
      }
    >
      {(open) => (
        <button
          onClick={open}
          title={`申请 ${name} 检定`}
          className="w-full flex items-center justify-between py-1 px-1 rounded text-xs hover:bg-[var(--color-accent)] hover:bg-opacity-10 cursor-pointer transition-colors"
        >
          {row}
        </button>
      )}
    </ConfirmDialog>
  )
}

function SkillsTab({
  character, onSkillCheck,
}: { character: CharacterData; onSkillCheck?: (skill: string, intent: string) => void }) {
  const skills = character.skills || {}
  const [query, setQuery] = useState('')
  const sorted = Object.entries(skills).sort((a, b) => b[1] - a[1])
  const filtered = query.trim()
    ? sorted.filter(([name]) => fuzzyMatch(query.trim(), name))
    : sorted

  return (
    <div className="space-y-1">
      <input
        value={query}
        onChange={(e) => setQuery(e.target.value)}
        placeholder="搜索技能…"
        className="w-full px-2 py-1 rounded text-xs"
        style={{ background: 'var(--color-bg-tertiary)', border: '1px solid var(--color-border)' }}
      />
      {onSkillCheck && (
        <p className="text-xs py-1" style={{ color: 'var(--color-text-secondary)', opacity: 0.7 }}>
          点击技能向 KP 申请检定（难度由 KP 裁定，之后再投骰）
        </p>
      )}
      <div className="space-y-0.5">
        {filtered.map(([name, value]) => {
          if (!onSkillCheck) {
            return (
              <div key={name} className="flex items-center justify-between py-1 px-1 rounded text-xs hover:bg-[var(--color-bg-tertiary)]">
                <span>{name}</span>
                <span className="font-mono font-bold" style={{
                  color: value >= 50 ? 'var(--color-success)' : value >= 25 ? 'var(--color-text-primary)' : 'var(--color-text-secondary)',
                }}>
                  {value}
                </span>
              </div>
            )
          }
          return <SkillCheckRow key={name} name={name} value={value} onSkillCheck={onSkillCheck} />
        })}
        {filtered.length === 0 && (
          <p className="text-xs text-center py-4" style={{ color: 'var(--color-text-secondary)' }}>
            {sorted.length === 0 ? '暂无技能数据' : '无匹配技能'}
          </p>
        )}
      </div>
    </div>
  )
}

interface WeaponItem {
  name: string
  skill?: string
  success?: number
  dam?: string
  damage?: string   // 兼容历史字段
  range?: string
  tho?: boolean
  round?: string
  attacks?: number  // 兼容历史字段
  num?: string
  ammo?: string     // 兼容历史字段
  err?: string
}

// 装备可能是字符串数组（AI 建卡/手动选装）或对象数组（兼容历史数据）
function equipmentName(item: unknown): string {
  if (typeof item === 'string') return item
  if (item && typeof item === 'object' && 'name' in item) return String((item as { name: unknown }).name ?? '')
  return ''
}

function InventoryTab({ character }: { character: CharacterData }) {
  const sd = character.system_data || {}
  const equipment = (Array.isArray(sd.equipment) ? sd.equipment : [])
    .map(equipmentName)
    .filter(Boolean)
  const weapons = (Array.isArray(sd.weapons) ? sd.weapons : []) as WeaponItem[]

  if (equipment.length === 0 && weapons.length === 0) {
    return (
      <div className="text-center py-8">
        <p className="text-xs" style={{ color: 'var(--color-text-secondary)' }}>暂无物品</p>
      </div>
    )
  }

  return (
    <div className="space-y-3">
      {weapons.length > 0 && (
        <div>
          <h4 className="text-xs font-semibold mb-1" style={{ color: 'var(--color-text-accent)' }}>武器</h4>
          <div className="space-y-1">
            {weapons.map((w, i) => {
              const dam = w.dam || w.damage
              const round = w.round || (w.attacks != null ? String(w.attacks) : '')
              const num = w.num || w.ammo
              return (
                <div key={`${w.name}-${i}`} className="text-xs px-2 py-1.5 rounded" style={{ background: 'var(--color-bg-tertiary)' }}>
                  <div className="flex justify-between">
                    <span className="font-semibold">{w.name}</span>
                    {dam && <span className="font-mono" style={{ color: 'var(--color-text-secondary)' }}>{dam}</span>}
                  </div>
                  <div className="flex flex-wrap gap-2 mt-0.5" style={{ color: 'var(--color-text-secondary)', fontSize: '0.65rem' }}>
                    {w.skill && <span>技能 {w.skill}</span>}
                    {w.success != null && <span>成功率 {w.success}</span>}
                    {w.range && <span>射程 {w.range}</span>}
                    {w.tho && <span>贯穿</span>}
                    {round && <span>次数 {round}</span>}
                    {num && <span>装弹 {num}</span>}
                    {w.err && <span>故障 {w.err}</span>}
                  </div>
                </div>
              )
            })}
          </div>
        </div>
      )}

      {equipment.length > 0 && (
        <div>
          <h4 className="text-xs font-semibold mb-1" style={{ color: 'var(--color-text-accent)' }}>随身物品</h4>
          <div className="flex flex-wrap gap-1">
            {equipment.map((name, i) => (
              <span key={`${name}-${i}`} className="text-xs px-2 py-1 rounded" style={{ background: 'var(--color-bg-tertiary)' }}>
                {name}
              </span>
            ))}
          </div>
        </div>
      )}
    </div>
  )
}

function Section({ title, children }: { title: string; children: ReactNode }) {
  return (
    <div>
      <h4 className="text-xs font-semibold mb-1" style={{ color: 'var(--color-text-accent)' }}>{title}</h4>
      {children}
    </div>
  )
}

// 资产 / 克苏鲁神话 / 人际关系 / 模组经历
function ProfileTab({ character }: { character: CharacterData }) {
  const sd = character.system_data || {}
  const creditRating = (sd.creditRating as number) ?? character.skills?.['信用评级'] ?? 0
  const cash = sd.cash as number | undefined
  const spendingLevel = sd.spendingLevel as number | undefined
  const assets = sd.assets as string | undefined
  const mythos = (sd.mythos || {}) as { spells?: string[]; tomes?: string[]; encounters?: string[] }
  const relations = (Array.isArray(sd.relations) ? sd.relations : []) as { name: string; relation: string }[]
  const history = (Array.isArray(sd.moduleHistory) ? sd.moduleHistory : []) as { module: string; experience: string }[]

  const fmt = (n?: number) => (n != null ? `$${n.toLocaleString()}` : '—')
  const hasMythos = (mythos.spells?.length || mythos.tomes?.length || mythos.encounters?.length)
  const empty = !hasMythos && relations.length === 0 && history.length === 0 && cash == null && spendingLevel == null && !assets

  return (
    <div className="space-y-3">
      <Section title="资产">
        <div className="rounded p-2 space-y-0.5" style={{ background: 'var(--color-bg-tertiary)' }}>
          <InfoRow label="信用评级" value={creditRating} />
          <InfoRow label="现金" value={fmt(cash)} />
          <InfoRow label="消费水平" value={fmt(spendingLevel)} />
          {assets && (
            <div className="text-xs pt-1" style={{ color: 'var(--color-text-secondary)' }}>资产：{assets}</div>
          )}
        </div>
      </Section>

      {hasMythos ? (
        <Section title="克苏鲁神话">
          <div className="space-y-1 text-xs" style={{ color: 'var(--color-text-secondary)' }}>
            {mythos.spells?.length ? <div>法术：{mythos.spells.join('、')}</div> : null}
            {mythos.tomes?.length ? <div>魔法物品与典籍：{mythos.tomes.join('、')}</div> : null}
            {mythos.encounters?.length ? <div>第三类接触：{mythos.encounters.join('、')}</div> : null}
          </div>
        </Section>
      ) : null}

      {relations.length > 0 && (
        <Section title="人际关系">
          <div className="space-y-0.5">
            {relations.map((r, i) => (
              <div key={i} className="flex justify-between text-xs">
                <span>{r.name}</span>
                <span style={{ color: 'var(--color-text-secondary)' }}>{r.relation}</span>
              </div>
            ))}
          </div>
        </Section>
      )}

      {history.length > 0 && (
        <Section title="模组经历">
          <div className="space-y-1.5">
            {history.map((m, i) => (
              <div key={i} className="text-xs rounded p-1.5" style={{ background: 'var(--color-bg-tertiary)' }}>
                <div className="font-semibold" style={{ color: 'var(--color-text-accent)' }}>{m.module}</div>
                {m.experience && <div className="mt-0.5 leading-relaxed" style={{ color: 'var(--color-text-secondary)' }}>{m.experience}</div>}
              </div>
            ))}
          </div>
        </Section>
      )}

      {empty && <p className="text-xs text-center py-4" style={{ color: 'var(--color-text-secondary)' }}>暂无档案信息</p>}
    </div>
  )
}

export function CharacterPanel({ character, onSkillCheck }: CharacterPanelProps) {
  return (
    <Tabs defaultValue="基本信息" className="flex flex-col h-full">
      <TabsList>
        {TAB_KEYS.map((t) => (
          <TabsTrigger key={t} value={t}>{t}</TabsTrigger>
        ))}
      </TabsList>
      <TabsContent value="基本信息">
        <BasicInfoTab character={character} />
      </TabsContent>
      <TabsContent value="技能">
        <SkillsTab character={character} onSkillCheck={onSkillCheck} />
      </TabsContent>
      <TabsContent value="道具">
        <InventoryTab character={character} />
      </TabsContent>
      <TabsContent value="档案">
        <ProfileTab character={character} />
      </TabsContent>
    </Tabs>
  )
}
