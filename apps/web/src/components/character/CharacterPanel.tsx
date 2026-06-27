import { RadarChart } from './RadarChart'
import { Tabs, TabsList, TabsTrigger, TabsContent } from '@/components/ui/tabs'

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
}

const ATTR_LABELS: Record<string, string> = {
  STR: '力量', CON: '体质', SIZ: '体型', DEX: '敏捷',
  APP: '外貌', INT: '智力', POW: '意志', EDU: '教育',
}

const RADAR_KEYS = ['STR', 'DEX', 'POW', 'CON', 'APP', 'EDU', 'SIZ', 'INT', 'LUK']
const RADAR_LABELS = ['力量', '敏捷', '意志', '体质', '外貌', '教育', '体型', '智力', '幸运']

const TAB_KEYS = ['基本信息', '技能', '道具'] as const

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
          className="h-full rounded-full transition-all"
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

function BasicInfoTab({ character }: { character: CharacterData }) {
  const sd = character.system_data || {}
  const hp = sd.hitPoints as { current: number; max: number } | undefined
  const san = sd.sanity as { current: number; max: number } | undefined
  const mp = sd.magicPoints as { current: number; max: number } | undefined
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
      </div>

      {hp && <StatBar label="HP" current={hp.current} max={hp.max} />}
      {san && <StatBar label="SAN" current={san.current} max={san.max} />}
      {mp && <StatBar label="MP" current={mp.current} max={mp.max} />}

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

function SkillsTab({ character }: { character: CharacterData }) {
  const skills = character.skills || {}
  const sorted = Object.entries(skills).sort((a, b) => b[1] - a[1])

  return (
    <div className="space-y-0.5">
      {sorted.map(([name, value]) => (
        <div key={name} className="flex items-center justify-between py-1 px-1 rounded text-xs hover:bg-[var(--color-bg-tertiary)]">
          <span>{name}</span>
          <span className="font-mono font-bold" style={{
            color: value >= 50 ? 'var(--color-success)' : value >= 25 ? 'var(--color-text-primary)' : 'var(--color-text-secondary)',
          }}>
            {value}
          </span>
        </div>
      ))}
      {sorted.length === 0 && (
        <p className="text-xs text-center py-4" style={{ color: 'var(--color-text-secondary)' }}>暂无技能数据</p>
      )}
    </div>
  )
}

interface WeaponItem {
  name: string
  skill?: string
  damage?: string
  range?: string
  attacks?: number
  ammo?: string
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
            {weapons.map((w, i) => (
              <div key={`${w.name}-${i}`} className="text-xs px-2 py-1.5 rounded" style={{ background: 'var(--color-bg-tertiary)' }}>
                <div className="flex justify-between">
                  <span className="font-semibold">{w.name}</span>
                  {w.damage && <span className="font-mono" style={{ color: 'var(--color-text-secondary)' }}>{w.damage}</span>}
                </div>
                <div className="flex gap-2 mt-0.5" style={{ color: 'var(--color-text-secondary)', fontSize: '0.65rem' }}>
                  {w.skill && <span>技能 {w.skill}</span>}
                  {w.range && <span>射程 {w.range}</span>}
                  {w.attacks ? <span>攻击 {w.attacks}</span> : null}
                  {w.ammo && <span>弹药 {w.ammo}</span>}
                </div>
              </div>
            ))}
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

export function CharacterPanel({ character }: CharacterPanelProps) {
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
        <SkillsTab character={character} />
      </TabsContent>
      <TabsContent value="道具">
        <InventoryTab character={character} />
      </TabsContent>
    </Tabs>
  )
}
