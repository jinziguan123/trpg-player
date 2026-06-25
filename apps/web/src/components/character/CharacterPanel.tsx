import { useState } from 'react'
import { RadarChart } from './RadarChart'

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

const TABS = ['基本信息', '技能', '道具'] as const
type Tab = (typeof TABS)[number]

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

function BasicInfoTab({ character }: { character: CharacterData }) {
  const sd = character.system_data || {}
  const hp = sd.hitPoints as { current: number; max: number } | undefined
  const san = sd.sanity as { current: number; max: number } | undefined
  const mp = sd.magicPoints as { current: number; max: number } | undefined
  const luck = (sd.luck as number) || 0
  const mov = (sd.move as number) || 0
  const occupation = (sd.occupation as string) || ''

  const radarValues = RADAR_KEYS.map((k) =>
    k === 'LUK' ? luck : (character.base_attributes[k] || 0)
  )

  return (
    <div className="space-y-4">
      <div className="text-center">
        <div
          className="w-20 h-20 mx-auto rounded-full flex items-center justify-center text-2xl mb-2"
          style={{ background: 'var(--color-bg-tertiary)', color: 'var(--color-text-secondary)' }}
        >
          {character.name.charAt(0)}
        </div>
        <h3 className="font-semibold text-lg" style={{ color: 'var(--color-text-accent)', fontFamily: 'var(--font-title)' }}>
          {character.name}
        </h3>
        {occupation && (
          <span className="text-xs" style={{ color: 'var(--color-text-secondary)' }}>{occupation}</span>
        )}
      </div>

      {hp && <StatBar label="HP" current={hp.current} max={hp.max} />}
      {san && <StatBar label="SAN" current={san.current} max={san.max} />}
      {mp && <StatBar label="MP" current={mp.current} max={mp.max} />}

      <div className="flex justify-between text-xs" style={{ color: 'var(--color-text-secondary)' }}>
        <span>幸运 <strong className="font-mono">{luck}</strong></span>
        <span>移动力 <strong className="font-mono">{mov}</strong></span>
      </div>

      <div className="flex justify-center">
        <RadarChart labels={RADAR_LABELS} values={radarValues} size={200} />
      </div>

      <div className="grid grid-cols-3 gap-1 text-center text-xs">
        {Object.entries(ATTR_LABELS).map(([k, label]) => (
          <div key={k} className="py-1 rounded" style={{ background: 'var(--color-bg-tertiary)' }}>
            <div style={{ color: 'var(--color-text-secondary)' }}>{label}</div>
            <div className="font-mono font-bold">{character.base_attributes[k] || 0}</div>
          </div>
        ))}
        <div className="py-1 rounded" style={{ background: 'var(--color-bg-tertiary)' }}>
          <div style={{ color: 'var(--color-text-secondary)' }}>幸运</div>
          <div className="font-mono font-bold">{luck}</div>
        </div>
      </div>

      {character.backstory && (
        <div>
          <h4 className="text-xs font-semibold mb-1" style={{ color: 'var(--color-text-accent)' }}>背景故事</h4>
          <p className="text-xs leading-relaxed" style={{ color: 'var(--color-text-secondary)' }}>
            {character.backstory}
          </p>
        </div>
      )}
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

function InventoryTab() {
  return (
    <div className="text-center py-8">
      <p className="text-xs" style={{ color: 'var(--color-text-secondary)' }}>暂无物品</p>
    </div>
  )
}

export function CharacterPanel({ character }: CharacterPanelProps) {
  const [tab, setTab] = useState<Tab>('基本信息')

  return (
    <div className="flex flex-col h-full">
      <div className="flex border-b" style={{ borderColor: 'var(--color-border)' }}>
        {TABS.map((t) => (
          <button
            key={t}
            onClick={() => setTab(t)}
            className="flex-1 py-2 text-xs text-center transition-colors"
            style={{
              color: tab === t ? 'var(--color-text-accent)' : 'var(--color-text-secondary)',
              borderBottom: tab === t ? '2px solid var(--color-accent)' : '2px solid transparent',
              fontWeight: tab === t ? 600 : 400,
            }}
          >
            {t}
          </button>
        ))}
      </div>

      <div className="flex-1 overflow-auto p-3">
        {tab === '基本信息' && <BasicInfoTab character={character} />}
        {tab === '技能' && <SkillsTab character={character} />}
        {tab === '道具' && <InventoryTab />}
      </div>
    </div>
  )
}
