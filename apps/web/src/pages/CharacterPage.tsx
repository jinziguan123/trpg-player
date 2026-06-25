import { useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { api } from '../api/client'
import { useModuleStore } from '../stores/moduleStore'
import { GiDiceSixFacesSix, GiCharacter, GiReturnArrow } from 'react-icons/gi'

interface Character {
  id: string
  name: string
  module_id: string
  rule_system: string
  base_attributes: Record<string, number>
  skills: Record<string, number>
  system_data: Record<string, unknown>
  backstory: string
  status: string
}

interface AttrSet {
  sets: Array<Record<string, number>>
}

interface OccupationDef {
  name: string
  credit_min: number
  credit_max: number
  skill_formula: string
  skills: string[]
  choices: number
}

const ATTR_LABELS: Record<string, string> = {
  STR: '力量', CON: '体质', SIZ: '体型', DEX: '敏捷',
  APP: '外貌', INT: '智力', POW: '意志', EDU: '教育',
}

const STEPS = ['基本信息', '属性掷骰', '职业选择', '技能加点', '背景故事'] as const
type Step = (typeof STEPS)[number]

export function CharacterPage() {
  const { modules, fetchModules } = useModuleStore()
  const navigate = useNavigate()
  const [characters, setCharacters] = useState<Character[]>([])
  const [step, setStep] = useState<Step>('基本信息')
  const [creating, setCreating] = useState(false)

  // Step 1: 基本信息
  const [name, setName] = useState('')
  const [moduleId, setModuleId] = useState('')

  // Step 2: 属性
  const [attrSets, setAttrSets] = useState<Record<string, number>[] | null>(null)
  const [selectedAttrs, setSelectedAttrs] = useState<Record<string, number> | null>(null)

  // Step 3: 职业
  const [occupations, setOccupations] = useState<OccupationDef[]>([])
  const [selectedOcc, setSelectedOcc] = useState<OccupationDef | null>(null)
  const [occPoints, setOccPoints] = useState(0)
  const [intPoints, setIntPoints] = useState(0)

  // Step 4: 技能加点
  const [skillAlloc, setSkillAlloc] = useState<Record<string, number>>({})
  const [defaultSkills, setDefaultSkills] = useState<Record<string, number>>({})

  // Step 5: 背景
  const [backstory, setBackstory] = useState('')

  useEffect(() => {
    fetchModules()
    loadCharacters()
    api.get<OccupationDef[]>('/rules/coc/occupations').then(setOccupations)
  }, [fetchModules])

  const loadCharacters = async () => {
    const chars = await api.get<Character[]>('/characters')
    setCharacters(chars)
  }

  const rollAttributes = async () => {
    const data = await api.post<AttrSet>('/characters/roll-attributes?rule_system=coc')
    setAttrSets(data.sets)
    setSelectedAttrs(null)
  }

  const selectOccupation = async (occ: OccupationDef) => {
    setSelectedOcc(occ)
    if (selectedAttrs) {
      const data = await api.post<{ occupation_points: number; interest_points: number }>(
        '/rules/coc/calc-skill-points',
        { occupation: occ.name, base_attributes: selectedAttrs },
      )
      setOccPoints(data.occupation_points)
      setIntPoints(data.interest_points)
    }
  }

  const goToSkills = async () => {
    const schema = await api.get<{ default_skills: Record<string, number> }>('/rules/coc/character-schema')
    setDefaultSkills(schema.default_skills || {})
    setSkillAlloc({})
    setStep('技能加点')
  }

  const allocatedOccTotal = Object.entries(skillAlloc)
    .filter(([k]) => selectedOcc?.skills.includes(k))
    .reduce((s, [, v]) => s + v, 0)

  const allocatedIntTotal = Object.entries(skillAlloc)
    .filter(([k]) => !selectedOcc?.skills.includes(k))
    .reduce((s, [, v]) => s + v, 0)

  const remainingOcc = occPoints - allocatedOccTotal
  const remainingInt = intPoints - allocatedIntTotal

  const updateSkill = (skillName: string, delta: number) => {
    const isOccSkill = selectedOcc?.skills.includes(skillName) ?? false
    const remaining = isOccSkill ? remainingOcc : remainingInt
    const current = skillAlloc[skillName] || 0
    const newVal = Math.max(0, current + delta)
    if (delta > 0 && remaining <= 0) return
    if (delta > 0 && delta > remaining) return
    setSkillAlloc({ ...skillAlloc, [skillName]: newVal })
  }

  const createCharacter = async () => {
    if (!name || !moduleId || !selectedAttrs) return
    setCreating(true)
    try {
      const finalSkills: Record<string, number> = { ...defaultSkills }
      for (const [k, v] of Object.entries(skillAlloc)) {
        finalSkills[k] = (finalSkills[k] || 0) + v
      }
      await api.post('/characters', {
        name,
        module_id: moduleId,
        rule_system: 'coc',
        base_attributes: selectedAttrs,
        skills: finalSkills,
        backstory,
        system_data: selectedOcc ? { occupation: selectedOcc.name } : {},
      })
      await loadCharacters()
      resetForm()
    } finally {
      setCreating(false)
    }
  }

  const resetForm = () => {
    setStep('基本信息')
    setName('')
    setModuleId('')
    setAttrSets(null)
    setSelectedAttrs(null)
    setSelectedOcc(null)
    setSkillAlloc({})
    setBackstory('')
  }

  const stepIndex = STEPS.indexOf(step)

  const allSkillNames = Object.keys(defaultSkills).sort((a, b) => {
    const aIsOcc = selectedOcc?.skills.includes(a) ?? false
    const bIsOcc = selectedOcc?.skills.includes(b) ?? false
    if (aIsOcc !== bIsOcc) return aIsOcc ? -1 : 1
    return a.localeCompare(b, 'zh')
  })

  return (
    <div className="max-w-3xl">
      <div className="flex items-center gap-3 mb-6">
        <button onClick={() => navigate(-1)} className="btn-secondary flex items-center gap-1 !px-2 !py-1 text-sm">
          <GiReturnArrow /> 返回
        </button>
        <h2 className="page-title !mb-0">角色管理</h2>
      </div>

      <div className="card mb-8">
        <h3 className="card-title flex items-center gap-2">
          <GiCharacter /> 创建角色（CoC 七版）
        </h3>

        {/* 步骤指示器 */}
        <div className="flex items-center gap-1 mb-4 text-xs">
          {STEPS.map((s, i) => (
            <div key={s} className="flex items-center gap-1">
              {i > 0 && <span style={{ color: 'var(--color-border)' }}>›</span>}
              <span
                className="px-2 py-0.5 rounded"
                style={{
                  background: i === stepIndex ? 'var(--color-accent)' : i < stepIndex ? 'var(--color-bg-tertiary)' : 'transparent',
                  color: i === stepIndex ? '#f0e6d3' : i < stepIndex ? 'var(--color-text-primary)' : 'var(--color-text-secondary)',
                  fontWeight: i === stepIndex ? 600 : 400,
                }}
              >
                {s}
              </span>
            </div>
          ))}
        </div>

        {/* Step 1: 基本信息 */}
        {step === '基本信息' && (
          <div>
            <div className="flex gap-3 mb-3">
              <input value={name} onChange={(e) => setName(e.target.value)} placeholder="角色名" className="input flex-1" />
              <select value={moduleId} onChange={(e) => setModuleId(e.target.value)} className="input">
                <option value="">选择模组</option>
                {modules.map((m) => <option key={m.id} value={m.id}>{m.title}</option>)}
              </select>
            </div>
            <button
              onClick={() => setStep('属性掷骰')}
              disabled={!name || !moduleId}
              className="btn-primary"
            >
              下一步
            </button>
          </div>
        )}

        {/* Step 2: 属性掷骰 */}
        {step === '属性掷骰' && (
          <div>
            <button onClick={rollAttributes} className="btn-secondary flex items-center gap-2 mb-3">
              <GiDiceSixFacesSix /> 掷骰属性（三选一）
            </button>

            {attrSets && (
              <div className="space-y-2 mb-3">
                {attrSets.map((attrs, i) => {
                  const total = Object.values(attrs).reduce((s, v) => s + v, 0)
                  return (
                    <button
                      key={i}
                      onClick={() => setSelectedAttrs(attrs)}
                      className="card w-full text-left text-sm transition-colors"
                      style={{ borderColor: selectedAttrs === attrs ? 'var(--color-accent)' : undefined }}
                    >
                      <div className="flex flex-wrap gap-2">
                        {Object.entries(attrs).map(([k, v]) => (
                          <span key={k} className="inline-block mr-2">
                            <span style={{ color: 'var(--color-text-secondary)' }}>{ATTR_LABELS[k] || k}</span>{' '}
                            <span className="font-mono font-bold">{v}</span>
                          </span>
                        ))}
                      </div>
                      <div className="text-xs mt-1" style={{ color: 'var(--color-text-secondary)' }}>
                        合计：{total}
                      </div>
                    </button>
                  )
                })}
              </div>
            )}

            <div className="flex gap-2">
              <button onClick={() => setStep('基本信息')} className="btn-secondary">上一步</button>
              <button onClick={() => setStep('职业选择')} disabled={!selectedAttrs} className="btn-primary">下一步</button>
            </div>
          </div>
        )}

        {/* Step 3: 职业选择 */}
        {step === '职业选择' && (
          <div>
            <div className="grid grid-cols-2 gap-2 mb-3 max-h-64 overflow-auto">
              {occupations.map((occ) => (
                <button
                  key={occ.name}
                  onClick={() => selectOccupation(occ)}
                  className="card text-left text-sm transition-colors !p-2"
                  style={{ borderColor: selectedOcc?.name === occ.name ? 'var(--color-accent)' : undefined }}
                >
                  <div className="font-semibold" style={{ color: 'var(--color-text-accent)' }}>{occ.name}</div>
                  <div className="text-xs mt-0.5" style={{ color: 'var(--color-text-secondary)' }}>
                    信用 {occ.credit_min}-{occ.credit_max}
                  </div>
                  <div className="text-xs mt-0.5" style={{ color: 'var(--color-text-secondary)' }}>
                    {occ.skills.slice(0, 4).join('、')}{occ.skills.length > 4 ? '…' : ''}
                  </div>
                </button>
              ))}
            </div>

            {selectedOcc && (
              <div className="card mb-3 !bg-[var(--color-bg-tertiary)]">
                <div className="font-semibold mb-1" style={{ color: 'var(--color-text-accent)' }}>{selectedOcc.name}</div>
                <div className="text-xs" style={{ color: 'var(--color-text-secondary)' }}>
                  <p>职业技能点：<strong className="font-mono">{occPoints}</strong></p>
                  <p>兴趣技能点：<strong className="font-mono">{intPoints}</strong></p>
                  <p className="mt-1">本职技能：{selectedOcc.skills.join('、')}</p>
                  {selectedOcc.choices > 0 && <p>可自选 {selectedOcc.choices} 项技能</p>}
                </div>
              </div>
            )}

            <div className="flex gap-2">
              <button onClick={() => setStep('属性掷骰')} className="btn-secondary">上一步</button>
              <button onClick={goToSkills} disabled={!selectedOcc} className="btn-primary">下一步</button>
            </div>
          </div>
        )}

        {/* Step 4: 技能加点 */}
        {step === '技能加点' && (
          <div>
            <div className="flex gap-4 mb-3 text-sm">
              <span>
                职业点剩余：<strong className="font-mono" style={{ color: remainingOcc > 0 ? 'var(--color-success)' : 'var(--color-text-secondary)' }}>{remainingOcc}</strong>
              </span>
              <span>
                兴趣点剩余：<strong className="font-mono" style={{ color: remainingInt > 0 ? 'var(--color-success)' : 'var(--color-text-secondary)' }}>{remainingInt}</strong>
              </span>
            </div>

            <div className="max-h-72 overflow-auto space-y-0.5 mb-3">
              {allSkillNames.map((skillName) => {
                const base = defaultSkills[skillName] || 0
                const alloc = skillAlloc[skillName] || 0
                const isOcc = selectedOcc?.skills.includes(skillName) ?? false
                return (
                  <div
                    key={skillName}
                    className="flex items-center justify-between py-1 px-2 rounded text-sm"
                    style={{ background: isOcc ? 'rgba(139, 37, 0, 0.06)' : undefined }}
                  >
                    <div className="flex items-center gap-2">
                      {isOcc && <span className="text-xs" style={{ color: 'var(--color-accent)' }}>职</span>}
                      <span>{skillName}</span>
                      <span className="text-xs font-mono" style={{ color: 'var(--color-text-secondary)' }}>
                        ({base}{alloc > 0 ? `+${alloc}` : ''})
                      </span>
                    </div>
                    <div className="flex items-center gap-1">
                      <span className="font-mono font-bold w-8 text-right">{base + alloc}</span>
                      <button
                        onClick={() => updateSkill(skillName, -5)}
                        disabled={alloc <= 0}
                        className="w-6 h-6 rounded text-xs border flex items-center justify-center"
                        style={{ borderColor: 'var(--color-border)', opacity: alloc <= 0 ? 0.3 : 1 }}
                      >
                        -
                      </button>
                      <button
                        onClick={() => updateSkill(skillName, 5)}
                        disabled={(isOcc ? remainingOcc : remainingInt) < 5}
                        className="w-6 h-6 rounded text-xs border flex items-center justify-center"
                        style={{ borderColor: 'var(--color-border)', opacity: (isOcc ? remainingOcc : remainingInt) < 5 ? 0.3 : 1 }}
                      >
                        +
                      </button>
                    </div>
                  </div>
                )
              })}
            </div>

            <div className="flex gap-2">
              <button onClick={() => setStep('职业选择')} className="btn-secondary">上一步</button>
              <button onClick={() => setStep('背景故事')} className="btn-primary">下一步</button>
            </div>
          </div>
        )}

        {/* Step 5: 背景故事 */}
        {step === '背景故事' && (
          <div>
            <textarea
              value={backstory}
              onChange={(e) => setBackstory(e.target.value)}
              placeholder="描述你角色的背景故事、性格特点、重要经历等（可选）..."
              rows={6}
              className="input w-full mb-3"
              style={{ resize: 'vertical' }}
            />
            <div className="flex gap-2">
              <button onClick={() => setStep('技能加点')} className="btn-secondary">上一步</button>
              <button onClick={createCharacter} disabled={creating} className="btn-primary">
                {creating ? '创建中...' : '完成创建'}
              </button>
            </div>
          </div>
        )}
      </div>

      {/* 已有角色列表 */}
      <div className="space-y-3">
        {characters.map((c) => {
          const hp = (c.system_data?.hitPoints as { current: number; max: number }) || {}
          const san = (c.system_data?.sanity as { current: number; max: number }) || {}
          const occ = (c.system_data?.occupation as string) || ''
          return (
            <div key={c.id} className="card">
              <div className="flex items-center justify-between mb-2">
                <h3 className="card-title !mb-0 flex items-center gap-2">
                  <GiCharacter className="opacity-60" /> {c.name}
                </h3>
                <div className="flex gap-2">
                  {occ && <span className="badge">{occ}</span>}
                  <span className="badge">{c.rule_system.toUpperCase()}</span>
                </div>
              </div>
              <div className="flex flex-wrap gap-3 text-sm" style={{ color: 'var(--color-text-secondary)' }}>
                {Object.entries(c.base_attributes).map(([k, v]) => (
                  <span key={k}>
                    {ATTR_LABELS[k] || k} <strong className="font-mono">{v}</strong>
                  </span>
                ))}
              </div>
              {hp.max && (
                <div className="flex gap-4 mt-2 text-xs font-mono" style={{ color: 'var(--color-text-secondary)' }}>
                  <span>HP {hp.current}/{hp.max}</span>
                  <span>SAN {san.current}/{san.max}</span>
                </div>
              )}
            </div>
          )
        })}
      </div>
    </div>
  )
}
