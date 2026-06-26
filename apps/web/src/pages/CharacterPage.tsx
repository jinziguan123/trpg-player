import { useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { toast } from 'sonner'
import { api } from '../api/client'
import { useModuleStore } from '../stores/moduleStore'
import { CharacterPanel } from '../components/character/CharacterPanel'
import { ConfirmDialog } from '../components/ui/confirm-dialog'
import { Select, SelectTrigger, SelectValue, SelectContent, SelectItem } from '@/components/ui/select'
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

const ATTR_KEYS = ['STR', 'CON', 'SIZ', 'DEX', 'APP', 'INT', 'POW', 'EDU'] as const

const ATTR_RANGES: Record<string, { min: number; max: number }> = {
  STR: { min: 15, max: 90 }, CON: { min: 15, max: 90 },
  SIZ: { min: 40, max: 90 }, DEX: { min: 15, max: 90 },
  APP: { min: 15, max: 90 }, INT: { min: 40, max: 90 },
  POW: { min: 15, max: 90 }, EDU: { min: 40, max: 90 },
}

const POINT_POOL = 460

const STEPS = ['基本信息', '属性设定', '职业选择', '技能加点', '背景故事'] as const
type Step = (typeof STEPS)[number]

function initAttrs(): Record<string, number> {
  const attrs: Record<string, number> = {}
  for (const k of ATTR_KEYS) attrs[k] = ATTR_RANGES[k].min
  return attrs
}

interface EvalResult {
  compatible: boolean
  warnings: string[]
  suggestions: string[]
}

export function CharacterPage() {
  const navigate = useNavigate()
  const { modules, fetchModules } = useModuleStore()
  const [characters, setCharacters] = useState<Character[]>([])
  const [step, setStep] = useState<Step>('基本信息')
  const [creating, setCreating] = useState(false)
  const [selectedChar, setSelectedChar] = useState<Character | null>(null)

  // Step 1
  const [name, setName] = useState('')
  const [moduleId, setModuleId] = useState('')

  // Evaluation
  const [evaluating, setEvaluating] = useState(false)
  const [evalResult, setEvalResult] = useState<EvalResult | null>(null)

  // Step 2: 属性
  const [useDice, setUseDice] = useState(false)
  const [customAttrs, setCustomAttrs] = useState<Record<string, number>>(initAttrs)
  const [attrSets, setAttrSets] = useState<Record<string, number>[] | null>(null)
  const [selectedAttrs, setSelectedAttrs] = useState<Record<string, number> | null>(null)

  // Step 3: 职业
  const [occupations, setOccupations] = useState<OccupationDef[]>([])
  const [selectedOcc, setSelectedOcc] = useState<OccupationDef | null>(null)
  const [occPoints, setOccPoints] = useState(0)
  const [intPoints, setIntPoints] = useState(0)
  const [occSearch, setOccSearch] = useState('')

  // Step 4: 技能
  const [skillAlloc, setSkillAlloc] = useState<Record<string, number>>({})
  const [defaultSkills, setDefaultSkills] = useState<Record<string, number>>({})

  // Step 5
  const [backstory, setBackstory] = useState('')

  useEffect(() => {
    loadCharacters()
    fetchModules()
    api.get<OccupationDef[]>('/rules/coc/occupations').then(setOccupations)
  }, [fetchModules])

  const loadCharacters = async () => {
    const chars = await api.get<Character[]>('/characters')
    setCharacters(chars)
  }

  // ---- 属性点数分配 ----
  const usedPoints = Object.values(customAttrs).reduce((s, v) => s + v, 0)
  const remainingPoints = POINT_POOL - usedPoints

  const updateAttr = (key: string, delta: number) => {
    const range = ATTR_RANGES[key]
    const cur = customAttrs[key] || range.min
    const next = Math.max(range.min, Math.min(range.max, cur + delta))
    const diff = next - cur
    if (diff > 0 && diff > remainingPoints) return
    setCustomAttrs({ ...customAttrs, [key]: next })
  }

  const setAttrDirect = (key: string, val: number) => {
    const range = ATTR_RANGES[key]
    const clamped = Math.max(range.min, Math.min(range.max, val))
    const diff = clamped - (customAttrs[key] || range.min)
    if (diff > 0 && diff > remainingPoints) return
    setCustomAttrs({ ...customAttrs, [key]: clamped })
  }

  const effectiveAttrs = useDice ? selectedAttrs : customAttrs

  // ---- 掷骰 ----
  const rollAttributes = async () => {
    const data = await api.post<AttrSet>('/characters/roll-attributes?rule_system=coc')
    setAttrSets(data.sets)
    setSelectedAttrs(null)
  }

  // ---- 职业 ----
  const selectOccupation = async (occ: OccupationDef) => {
    setSelectedOcc(occ)
    if (effectiveAttrs) {
      const data = await api.post<{ occupation_points: number; interest_points: number }>(
        '/rules/coc/calc-skill-points',
        { occupation: occ.name, base_attributes: effectiveAttrs },
      )
      setOccPoints(data.occupation_points)
      setIntPoints(data.interest_points)
    }
  }

  const filteredOccs = occSearch.trim()
    ? occupations.filter((o) => {
        const q = occSearch.trim().toLowerCase()
        return o.name.toLowerCase().includes(q)
          || o.skills.some((s) => s.toLowerCase().includes(q))
      })
    : occupations

  // ---- 技能 ----
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

  // ---- 评估 + 创建 ----
  const evaluateAndCreate = async () => {
    if (!name || !effectiveAttrs || !moduleId) return
    setEvaluating(true)
    setEvalResult(null)
    try {
      const result = await api.post<EvalResult>('/characters/evaluate', {
        module_id: moduleId,
        name,
        occupation: selectedOcc?.name || '',
        backstory,
      })
      if (result.compatible && result.warnings.length === 0) {
        await doCreate()
      } else {
        setEvalResult(result)
      }
    } catch {
      await doCreate()
    } finally {
      setEvaluating(false)
    }
  }

  const doCreate = async () => {
    if (!name || !effectiveAttrs || !moduleId) return
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
        base_attributes: effectiveAttrs,
        skills: finalSkills,
        backstory,
        system_data: selectedOcc ? { occupation: selectedOcc.name } : {},
      })
      await loadCharacters()
      toast.success(`角色「${name}」创建成功`)
      resetForm()
    } catch {
      toast.error('角色创建失败')
    } finally {
      setCreating(false)
    }
  }

  const resetForm = () => {
    setStep('基本信息')
    setName('')
    setModuleId('')
    setUseDice(false)
    setCustomAttrs(initAttrs())
    setAttrSets(null)
    setSelectedAttrs(null)
    setSelectedOcc(null)
    setSkillAlloc({})
    setBackstory('')
    setOccSearch('')
    setEvalResult(null)
  }

  const deleteCharacter = async (id: string) => {
    try {
      await api.delete(`/characters/${id}`)
      if (selectedChar?.id === id) setSelectedChar(null)
      await loadCharacters()
      toast.success('角色已删除')
    } catch {
      toast.error('删除失败')
    }
  }

  const stepIndex = STEPS.indexOf(step)

  const allSkillNames = Object.keys(defaultSkills).sort((a, b) => {
    const aIsOcc = selectedOcc?.skills.includes(a) ?? false
    const bIsOcc = selectedOcc?.skills.includes(b) ?? false
    if (aIsOcc !== bIsOcc) return aIsOcc ? -1 : 1
    return a.localeCompare(b, 'zh')
  })

  return (
    <div className="flex h-full gap-0">
      <div className="flex-1 min-w-0 overflow-auto p-4">
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
                <div className="mb-3">
                  <label className="block text-sm mb-1" style={{ color: 'var(--color-text-secondary)' }}>所属模组</label>
                  <Select value={moduleId} onValueChange={setModuleId}>
                    <SelectTrigger className="w-full">
                      <SelectValue placeholder="— 选择模组 —" />
                    </SelectTrigger>
                    <SelectContent>
                      {modules.map((m) => (
                        <SelectItem key={m.id} value={m.id}>
                          {m.title}
                          {m.world_setting?.era ? ` (${m.world_setting.era})` : ''}
                        </SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                </div>
                <div className="mb-3">
                  <label className="block text-sm mb-1" style={{ color: 'var(--color-text-secondary)' }}>角色名</label>
                  <input value={name} onChange={(e) => setName(e.target.value)} placeholder="输入角色名" className="input w-full" />
                </div>
                <button onClick={() => setStep('属性设定')} disabled={!name || !moduleId} className="btn-primary">
                  下一步
                </button>
              </div>
            )}

            {/* Step 2: 属性设定 */}
            {step === '属性设定' && (
              <div>
                {/* 模式切换 */}
                <div className="flex items-center gap-3 mb-4">
                  <label className="flex items-center gap-2 cursor-pointer text-sm">
                    <input
                      type="checkbox"
                      checked={useDice}
                      onChange={(e) => { setUseDice(e.target.checked); setSelectedAttrs(null) }}
                      className="accent-[var(--color-accent)]"
                    />
                    <GiDiceSixFacesSix />
                    使用掷骰模式（三选一）
                  </label>
                </div>

                {!useDice ? (
                  /* ===== 自定义分配 ===== */
                  <div>
                    <div className="flex justify-between items-center mb-3 text-sm">
                      <span>总点数池：<strong className="font-mono">{POINT_POOL}</strong></span>
                      <span>
                        剩余：
                        <strong className="font-mono" style={{ color: remainingPoints > 0 ? 'var(--color-success)' : remainingPoints < 0 ? 'var(--color-danger)' : 'var(--color-text-secondary)' }}>
                          {remainingPoints}
                        </strong>
                      </span>
                    </div>
                    <div className="space-y-1.5 mb-3">
                      {ATTR_KEYS.map((k) => {
                        const range = ATTR_RANGES[k]
                        const val = customAttrs[k] || range.min
                        return (
                          <div key={k} className="flex items-center gap-2 py-1 px-2 rounded" style={{ background: 'var(--color-bg-tertiary)' }}>
                            <span className="w-16 text-sm">
                              <span style={{ color: 'var(--color-text-secondary)' }}>{ATTR_LABELS[k]}</span>
                            </span>
                            <button
                              onClick={() => updateAttr(k, -5)}
                              disabled={val <= range.min}
                              className="w-6 h-6 rounded text-xs border flex items-center justify-center"
                              style={{ borderColor: 'var(--color-border)', opacity: val <= range.min ? 0.3 : 1 }}
                            >
                              -
                            </button>
                            <input
                              type="number"
                              value={val}
                              onChange={(e) => setAttrDirect(k, parseInt(e.target.value) || range.min)}
                              className="input w-16 text-center font-mono font-bold !py-0.5 text-sm"
                              min={range.min}
                              max={range.max}
                              step={5}
                            />
                            <button
                              onClick={() => updateAttr(k, 5)}
                              disabled={val >= range.max || remainingPoints < 5}
                              className="w-6 h-6 rounded text-xs border flex items-center justify-center"
                              style={{ borderColor: 'var(--color-border)', opacity: (val >= range.max || remainingPoints < 5) ? 0.3 : 1 }}
                            >
                              +
                            </button>
                            <span className="text-xs ml-1" style={{ color: 'var(--color-text-secondary)' }}>
                              {range.min}-{range.max}
                            </span>
                          </div>
                        )
                      })}
                    </div>
                  </div>
                ) : (
                  /* ===== 掷骰模式 ===== */
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
                              <div className="text-xs mt-1" style={{ color: 'var(--color-text-secondary)' }}>合计：{total}</div>
                            </button>
                          )
                        })}
                      </div>
                    )}
                  </div>
                )}

                <div className="flex gap-2">
                  <button onClick={() => setStep('基本信息')} className="btn-secondary">上一步</button>
                  <button
                    onClick={() => setStep('职业选择')}
                    disabled={useDice ? !selectedAttrs : remainingPoints < 0}
                    className="btn-primary"
                  >
                    下一步
                  </button>
                </div>
              </div>
            )}

            {/* Step 3: 职业选择 */}
            {step === '职业选择' && (
              <div>
                <input
                  value={occSearch}
                  onChange={(e) => setOccSearch(e.target.value)}
                  placeholder="搜索职业名称或技能关键词..."
                  className="input w-full mb-3"
                />
                <div className="grid grid-cols-2 gap-2 mb-3 max-h-64 overflow-auto">
                  {filteredOccs.map((occ) => (
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
                  {filteredOccs.length === 0 && (
                    <p className="col-span-2 text-center text-sm py-4" style={{ color: 'var(--color-text-secondary)' }}>
                      未找到匹配的职业
                    </p>
                  )}
                </div>

                {selectedOcc && (
                  <div className="card mb-3 !bg-[var(--color-bg-tertiary)]">
                    <div className="font-semibold mb-1" style={{ color: 'var(--color-text-accent)' }}>{selectedOcc.name}</div>
                    <div className="text-xs" style={{ color: 'var(--color-text-secondary)' }}>
                      <p>职业技能点：<strong className="font-mono">{occPoints}</strong>（{selectedOcc.skill_formula}）</p>
                      <p>兴趣技能点：<strong className="font-mono">{intPoints}</strong>（INT×2）</p>
                      <p className="mt-1">本职技能：{selectedOcc.skills.join('、')}</p>
                      {selectedOcc.choices > 0 && <p>可自选 {selectedOcc.choices} 项技能</p>}
                    </div>
                  </div>
                )}

                <div className="flex gap-2">
                  <button onClick={() => setStep('属性设定')} className="btn-secondary">上一步</button>
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
                  onChange={(e) => { setBackstory(e.target.value); setEvalResult(null) }}
                  placeholder="描述你角色的背景故事、性格特点、重要经历等（可选）..."
                  rows={6}
                  className="input w-full mb-3"
                  style={{ resize: 'vertical' }}
                />

                {evalResult && (
                  <div
                    className="card mb-3 text-sm"
                    style={{
                      borderColor: evalResult.compatible ? 'var(--color-success)' : 'var(--color-danger)',
                      background: evalResult.compatible ? 'rgba(45, 125, 70, 0.06)' : 'rgba(153, 27, 27, 0.06)',
                    }}
                  >
                    <div className="font-semibold mb-1" style={{ color: evalResult.compatible ? 'var(--color-success)' : 'var(--color-danger)' }}>
                      {evalResult.compatible ? 'AI 评估通过（有建议）' : 'AI 评估发现问题'}
                    </div>
                    {evalResult.warnings.length > 0 && (
                      <ul className="list-disc list-inside mb-1" style={{ color: 'var(--color-danger)' }}>
                        {evalResult.warnings.map((w, i) => <li key={i}>{w}</li>)}
                      </ul>
                    )}
                    {evalResult.suggestions.length > 0 && (
                      <ul className="list-disc list-inside" style={{ color: 'var(--color-text-secondary)' }}>
                        {evalResult.suggestions.map((s, i) => <li key={i}>{s}</li>)}
                      </ul>
                    )}
                    <div className="flex gap-2 mt-2">
                      <button onClick={doCreate} disabled={creating} className="btn-primary text-sm">
                        {creating ? '创建中...' : '仍然创建'}
                      </button>
                      <button onClick={() => setEvalResult(null)} className="btn-secondary text-sm">
                        返回修改
                      </button>
                    </div>
                  </div>
                )}

                {!evalResult && (
                  <div className="flex gap-2">
                    <button onClick={() => setStep('技能加点')} className="btn-secondary">上一步</button>
                    <button onClick={evaluateAndCreate} disabled={evaluating || creating} className="btn-primary">
                      {evaluating ? 'AI 评估中...' : creating ? '创建中...' : '完成创建'}
                    </button>
                  </div>
                )}
              </div>
            )}
          </div>

          {/* 已有角色列表 */}
          <div className="space-y-3">
            {characters.map((c) => {
              const hp = (c.system_data?.hitPoints as { current: number; max: number }) || {}
              const san = (c.system_data?.sanity as { current: number; max: number }) || {}
              const occ = (c.system_data?.occupation as string) || ''
              const isActive = selectedChar?.id === c.id
              return (
                <div
                  key={c.id}
                  className="card cursor-pointer transition-colors"
                  style={{ borderColor: isActive ? 'var(--color-accent)' : undefined }}
                  onClick={() => setSelectedChar(isActive ? null : c)}
                >
                  <div className="flex items-center justify-between mb-2">
                    <h3 className="card-title !mb-0 flex items-center gap-2">
                      <GiCharacter className="opacity-60" /> {c.name}
                    </h3>
                    <div className="flex items-center gap-2">
                      {occ && <span className="badge">{occ}</span>}
                      <span className="badge">{c.rule_system.toUpperCase()}</span>
                      <ConfirmDialog
                        title="删除角色"
                        description={`确定要删除「${c.name}」吗？此操作不可恢复。`}
                        confirmLabel="删除"
                        onConfirm={() => deleteCharacter(c.id)}
                      >
                        {(open) => (
                          <button
                            onClick={(e) => { e.stopPropagation(); open() }}
                            className="text-xs px-1.5 py-0.5 rounded hover:bg-[var(--color-danger)] hover:text-white transition-colors"
                            style={{ color: 'var(--color-danger)', border: '1px solid var(--color-danger)' }}
                          >
                            删除
                          </button>
                        )}
                      </ConfirmDialog>
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
      </div>

      {/* 右侧角色详情面板 */}
      {selectedChar && (
        <aside
          className="w-72 flex-shrink-0 border-l overflow-hidden"
          style={{ borderColor: 'var(--color-border)', background: 'var(--color-bg-card)' }}
        >
          <CharacterPanel character={selectedChar} />
        </aside>
      )}
    </div>
  )
}
