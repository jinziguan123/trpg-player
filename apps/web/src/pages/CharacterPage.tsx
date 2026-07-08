import { useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { toast } from 'sonner'
import { api, uploadFile } from '../api/client'
import { useModuleStore } from '../stores/moduleStore'
import { CharacterPanel } from '../components/character/CharacterPanel'
import { CharacterEditModal } from '../components/character/CharacterEditModal'
import { SpecializationDialog } from '../components/character/SpecializationDialog'
import { WeaponsEditor } from '../components/character/WeaponsEditor'
import { useSpecializations, normalizeWeapon, type CharWeapon } from '../components/character/useCocData'
import {
  AssetsPanel, MythosEditor, RelationsEditor, ModuleHistoryEditor,
  type AssetsInfo, type Mythos, type Relation, type ModuleExperience,
} from '../components/character/CharacterExtraEditors'
import { ConfirmDialog } from '../components/ui/confirm-dialog'
import { Select, SelectTrigger, SelectValue, SelectContent, SelectItem } from '@/components/ui/select'
import { GiDiceSixFacesSix, GiCharacter, GiReturnArrow, GiUpCard, GiPadlock } from 'react-icons/gi'
import { ChevronRight } from 'lucide-react'

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
  category?: string
}

const ATTR_LABELS: Record<string, string> = {
  STR: '力量', CON: '体质', SIZ: '体型', DEX: '敏捷',
  APP: '外貌', INT: '智力', POW: '意志', EDU: '教育',
}

const ATTR_KEYS = ['STR', 'CON', 'SIZ', 'DEX', 'APP', 'INT', 'POW', 'EDU'] as const

// 专精基名（与后端 SPECIALIZATIONS 对齐）
const SPEC_BASES = ['母语', '外语', '格斗', '射击', '科学', '生存', '技艺', '驾驶']

// 技能键基名："格斗(斗殴)" → "格斗"
const skillBase = (s: string) => s.split('(')[0]

const ATTR_RANGES: Record<string, { min: number; max: number }> = {
  STR: { min: 15, max: 90 }, CON: { min: 15, max: 90 },
  SIZ: { min: 40, max: 90 }, DEX: { min: 15, max: 90 },
  APP: { min: 15, max: 90 }, INT: { min: 40, max: 90 },
  POW: { min: 15, max: 90 }, EDU: { min: 40, max: 90 },
}

const POINT_POOL = 460

const STEPS = ['基本信息', '属性设定', '职业选择', '技能加点', '背景故事', '随身物品'] as const
type Step = (typeof STEPS)[number]

interface WeaponItem {
  name: string
  skill: string
  damage: string
  range: string
  attacks: number
  ammo: string
}

// Excel 导入与 AI 建卡返回的统一数据结构
interface ImportedCharacterData {
  name?: string
  age?: number
  base_attributes?: Record<string, number>
  skills?: Record<string, number>
  backstory?: string
  equipment?: string[]
  weapons?: WeaponItem[]
  system_data?: {
    gender?: string
    residence?: string
    birthplace?: string
    creditRating?: number
    luck?: number
    occupation?: string
    personalDescription?: string
    ideologyBeliefs?: string
    significantPeople?: string
    meaningfulLocations?: string
    treasuredPossessions?: string
    traits?: string
    scarsAndWounds?: string
    phobiasAndManias?: string
    investigatorHistory?: string
  }
}

const NON_ALLOCATABLE_SKILLS = ['克苏鲁神话']

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
  const [editingChar, setEditingChar] = useState<Character | null>(null)
  // 默认只展示列表；点「创建角色」进入创建流程
  const [inCreateFlow, setInCreateFlow] = useState(false)
  const [charQuery, setCharQuery] = useState('')
  const [charPage, setCharPage] = useState(1)
  const CHAR_PAGE_SIZE = 8

  // Step 1: 基本信息
  const [name, setName] = useState('')
  const [moduleId, setModuleId] = useState('')
  const [age, setAge] = useState(25)
  const [gender, setGender] = useState('')
  const [residence, setResidence] = useState('')
  const [birthplace, setBirthplace] = useState('')

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
  const [occCat, setOccCat] = useState('')               // 选中的职业大类（空=全部）
  const [occCategories, setOccCategories] = useState<string[]>([])

  // Step 2: 幸运值
  const [luck, setLuck] = useState(0)

  // Step 3: 信用评级
  const [creditRating, setCreditRating] = useState(0)

  // Step 4: 技能
  const [skillAlloc, setSkillAlloc] = useState<Record<string, number>>({})
  const [defaultSkills, setDefaultSkills] = useState<Record<string, number>>({})
  const [extraSkills, setExtraSkills] = useState<string[]>([])              // 经专精弹窗添加的「基名(专精)」技能键
  const [specBaseVals, setSpecBaseVals] = useState<Record<string, number>>({})  // 这些专精技能的基础起始值
  const [specBase, setSpecBase] = useState<string>('')                     // 当前打开专精弹窗的基名
  const spec = useSpecializations()

  // Step 5: 物品
  const [equipText, setEquipText] = useState('')                           // 随身物品自由文本，以、分隔
  const [weapons, setWeapons] = useState<CharWeapon[]>([])
  // 资产 / 克苏鲁神话 / 人际关系 / 模组经历
  const [assetsInfo, setAssetsInfo] = useState<AssetsInfo>({ cash: 0, spendingLevel: 0, assets: '' })
  const [mythos, setMythos] = useState<Mythos>({ spells: [], tomes: [], encounters: [] })
  const [relations, setRelations] = useState<Relation[]>([])
  const [moduleHistory, setModuleHistory] = useState<ModuleExperience[]>([])

  // Step 5: 结构化背景故事
  const [personalDesc, setPersonalDesc] = useState('')
  const [ideologyBeliefs, setIdeologyBeliefs] = useState('')
  const [significantPeople, setSignificantPeople] = useState('')
  const [meaningfulLocations, setMeaningfulLocations] = useState('')
  const [treasuredPossessions, setTreasuredPossessions] = useState('')
  const [traits, setTraits] = useState('')
  const [scarsAndWounds, setScarsAndWounds] = useState('')
  const [phobiasAndManias, setPhobiasAndManias] = useState('')
  const [investigatorHistory, setInvestigatorHistory] = useState('')

  useEffect(() => {
    loadCharacters()
    fetchModules()
    api.get<OccupationDef[]>('/rules/coc/occupations').then(setOccupations)
    api.get<string[]>('/rules/coc/occupation-categories').then(setOccCategories).catch(() => {})
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

  // ---- 派生属性计算 ----
  const derivedStats = (() => {
    if (!effectiveAttrs) return null
    const str = effectiveAttrs.STR ?? 50
    const con = effectiveAttrs.CON ?? 50
    const siz = effectiveAttrs.SIZ ?? 50
    const dex = effectiveAttrs.DEX ?? 50
    const pow = effectiveAttrs.POW ?? 50
    const hp = Math.floor((con + siz) / 10)
    const mp = Math.floor(pow / 5)
    const san = pow
    let mov = (dex < siz && str < siz) ? 7 : (dex >= siz || str >= siz) ? 8 : 9
    if (age >= 80) mov -= 5
    else if (age >= 70) mov -= 4
    else if (age >= 60) mov -= 3
    else if (age >= 50) mov -= 2
    else if (age >= 40) mov -= 1
    const combined = str + siz
    let db: string, build: number
    if (combined <= 64) { db = '-2'; build = -2 }
    else if (combined <= 84) { db = '-1'; build = -1 }
    else if (combined <= 124) { db = '0'; build = 0 }
    else if (combined <= 164) { db = '1D4'; build = 1 }
    else { db = '1D6'; build = 2 }
    const dodge = Math.floor(dex / 2)
    return { hp, mp, san, mov, db, build, dodge }
  })()

  // ---- 掷骰 ----
  const rollLuck = () => {
    const d1 = Math.floor(Math.random() * 6) + 1
    const d2 = Math.floor(Math.random() * 6) + 1
    const d3 = Math.floor(Math.random() * 6) + 1
    setLuck((d1 + d2 + d3) * 5)
  }

  const rollAttributes = async () => {
    const data = await api.post<AttrSet>('/characters/roll-attributes?rule_system=coc')
    setAttrSets(data.sets)
    setSelectedAttrs(null)
  }

  // ---- 职业 ----
  const selectOccupation = async (occ: OccupationDef) => {
    setSelectedOcc(occ)
    setCreditRating(occ.credit_min)
    if (effectiveAttrs) {
      const data = await api.post<{ occupation_points: number; interest_points: number }>(
        '/rules/coc/calc-skill-points',
        { occupation: occ.name, base_attributes: effectiveAttrs },
      )
      setOccPoints(data.occupation_points)
      setIntPoints(data.interest_points)
    }
  }

  const filteredOccs = occupations.filter((o) => {
    if (occCat && (o.category || '其他') !== occCat) return false
    const q = occSearch.trim().toLowerCase()
    if (!q) return true
    return o.name.toLowerCase().includes(q) || o.skills.some((s) => s.toLowerCase().includes(q))
  })

  // ---- 技能 ----
  const goToSkills = async () => {
    const schema = await api.get<{ default_skills: Record<string, number> }>('/rules/coc/character-schema')
    setDefaultSkills(schema.default_skills || {})
    if (!isImported) setSkillAlloc({})
    setStep('技能加点')
  }

  // 职业技能按基名匹配：职业写「格斗」可命中「格斗(斗殴)」等专精
  const occHas = (skillName: string): boolean => {
    const occSkills = selectedOcc?.skills ?? []
    return occSkills.some((os) => os === skillName || skillBase(os) === skillBase(skillName))
  }

  const allocatedOccTotal = Object.entries(skillAlloc)
    .filter(([k]) => occHas(k))
    .reduce((s, [, v]) => s + v, 0)

  const allocatedIntTotal = Object.entries(skillAlloc)
    .filter(([k]) => !occHas(k))
    .reduce((s, [, v]) => s + v, 0)

  const remainingOcc = occPoints - allocatedOccTotal
  const remainingInt = intPoints - allocatedIntTotal

  // 技能当前值：基础(默认或专精起始) + 已加点；导入模式直接用加点值
  const skillValueOf = (skillName: string): number => {
    const alloc = skillAlloc[skillName] || 0
    if (isImported) return alloc
    return (defaultSkills[skillName] ?? specBaseVals[skillName] ?? 0) + alloc
  }

  // 选中专精 → 落为「基名(专精)」技能；母语值=EDU，其余用专精 init
  const addSpecialization = (base: string, specName: string, init: number) => {
    const key = `${base}(${specName})`
    if (defaultSkills[key] != null || extraSkills.includes(key)) { toast.error(`已存在「${key}」`); return }
    const value = base === '母语' ? ((effectiveAttrs?.EDU as number) || init) : init
    setExtraSkills([...extraSkills, key])
    setSpecBaseVals({ ...specBaseVals, [key]: value })
    toast.success(`已添加 ${key}`)
  }

  const updateSkill = (skillName: string, delta: number) => {
    if (!isImported) {
      const isOccSkill = occHas(skillName)
      const remaining = isOccSkill ? remainingOcc : remainingInt
      if (delta > 0 && remaining <= 0) return
      if (delta > 0 && delta > remaining) return
    }
    const current = skillAlloc[skillName] || 0
    const newVal = Math.max(0, current + delta)
    setSkillAlloc({ ...skillAlloc, [skillName]: newVal })
  }

  // ---- 构建背景文本 ----
  const buildBackstoryText = () => {
    const parts: string[] = []
    if (personalDesc) parts.push(`【个人描述】${personalDesc}`)
    if (ideologyBeliefs) parts.push(`【思想/信念】${ideologyBeliefs}`)
    if (significantPeople) parts.push(`【重要之人】${significantPeople}`)
    if (meaningfulLocations) parts.push(`【意义非凡之地】${meaningfulLocations}`)
    if (treasuredPossessions) parts.push(`【宝贵之物】${treasuredPossessions}`)
    if (traits) parts.push(`【特点】${traits}`)
    if (scarsAndWounds) parts.push(`【伤口/疤痕】${scarsAndWounds}`)
    if (phobiasAndManias) parts.push(`【恐惧症/狂躁症】${phobiasAndManias}`)
    if (investigatorHistory) parts.push(`【调查员经历】${investigatorHistory}`)
    return parts.join('\n')
  }

  const buildSystemData = () => {
    const sd: Record<string, unknown> = {}
    if (selectedOcc) sd.occupation = selectedOcc.name
    if (gender) sd.gender = gender
    if (residence) sd.residence = residence
    if (birthplace) sd.birthplace = birthplace
    if (creditRating > 0) sd.creditRating = creditRating
    if (luck > 0) sd.luck = luck
    if (personalDesc) sd.personalDescription = personalDesc
    if (ideologyBeliefs) sd.ideologyBeliefs = ideologyBeliefs
    if (significantPeople) sd.significantPeople = significantPeople
    if (meaningfulLocations) sd.meaningfulLocations = meaningfulLocations
    if (treasuredPossessions) sd.treasuredPossessions = treasuredPossessions
    if (traits) sd.traits = traits
    if (scarsAndWounds) sd.scarsAndWounds = scarsAndWounds
    if (phobiasAndManias) sd.phobiasAndManias = phobiasAndManias
    if (investigatorHistory) sd.investigatorHistory = investigatorHistory
    const equip = equipText.split(/[、,，]/).map((e) => e.trim()).filter(Boolean)
    if (equip.length > 0) sd.equipment = equip
    if (weapons.length > 0) sd.weapons = weapons
    // 资产（现金/消费水平/资产情况）
    if (assetsInfo.cash) sd.cash = assetsInfo.cash
    if (assetsInfo.spendingLevel) sd.spendingLevel = assetsInfo.spendingLevel
    if (assetsInfo.assets.trim()) sd.assets = assetsInfo.assets
    // 克苏鲁神话
    const mythosClean = {
      spells: mythos.spells.map((s) => s.trim()).filter(Boolean),
      tomes: mythos.tomes.map((s) => s.trim()).filter(Boolean),
      encounters: mythos.encounters.map((s) => s.trim()).filter(Boolean),
    }
    if (mythosClean.spells.length || mythosClean.tomes.length || mythosClean.encounters.length) sd.mythos = mythosClean
    const rels = relations.filter((r) => r.name.trim() || r.relation.trim())
    if (rels.length) sd.relations = rels
    const hist = moduleHistory.filter((m) => m.module.trim() || m.experience.trim())
    if (hist.length) sd.moduleHistory = hist
    return sd
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
        backstory: buildBackstoryText(),
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
      let finalSkills: Record<string, number>
      if (isImported) {
        finalSkills = { ...defaultSkills, ...specBaseVals, ...skillAlloc }
      } else {
        finalSkills = { ...defaultSkills, ...specBaseVals }
        for (const [k, v] of Object.entries(skillAlloc)) {
          finalSkills[k] = (finalSkills[k] || 0) + v
        }
      }
      finalSkills['信用评级'] = creditRating
      await api.post('/characters', {
        name,
        module_id: moduleId,
        rule_system: 'coc',
        age,
        base_attributes: effectiveAttrs,
        skills: finalSkills,
        backstory: buildBackstoryText(),
        system_data: buildSystemData(),
      })
      await loadCharacters()
      toast.success(`角色「${name}」创建成功`)
      resetForm()
      setInCreateFlow(false)   // 创建完成回到列表
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
    setAge(25)
    setGender('')
    setResidence('')
    setBirthplace('')
    setUseDice(false)
    setCustomAttrs(initAttrs())
    setAttrSets(null)
    setSelectedAttrs(null)
    setLuck(0)
    setSelectedOcc(null)
    setCreditRating(0)
    setSkillAlloc({})
    setExtraSkills([])
    setSpecBaseVals({})
    setEquipText('')
    setWeapons([])
    setAssetsInfo({ cash: 0, spendingLevel: 0, assets: '' })
    setMythos({ spells: [], tomes: [], encounters: [] })
    setRelations([])
    setModuleHistory([])
    setPersonalDesc('')
    setIdeologyBeliefs('')
    setSignificantPeople('')
    setMeaningfulLocations('')
    setTreasuredPossessions('')
    setTraits('')
    setScarsAndWounds('')
    setPhobiasAndManias('')
    setInvestigatorHistory('')
    setOccSearch('')
    setEvalResult(null)
    setIsImported(false)
    setAiHint('')
  }

  const [importing, setImporting] = useState(false)
  const [isImported, setIsImported] = useState(false)
  const [aiHint, setAiHint] = useState('')
  const [aiGenerating, setAiGenerating] = useState(false)

  // 把导入/AI 生成的数据填入创建表单（Excel 导入与 AI 建卡共用）
  const applyImportedData = (data: ImportedCharacterData) => {
    if (data.name) setName(data.name)
    if (data.age) setAge(data.age)
    const sd = data.system_data || {}
    if (sd.gender) setGender(sd.gender)
    if (sd.residence) setResidence(sd.residence)
    if (sd.birthplace) setBirthplace(sd.birthplace)
    if (sd.creditRating) setCreditRating(sd.creditRating)
    if (sd.luck) setLuck(sd.luck)
    if (sd.personalDescription) setPersonalDesc(sd.personalDescription)
    if (sd.ideologyBeliefs) setIdeologyBeliefs(sd.ideologyBeliefs)
    if (sd.significantPeople) setSignificantPeople(sd.significantPeople)
    if (sd.meaningfulLocations) setMeaningfulLocations(sd.meaningfulLocations)
    if (sd.treasuredPossessions) setTreasuredPossessions(sd.treasuredPossessions)
    if (sd.traits) setTraits(sd.traits)
    if (sd.scarsAndWounds) setScarsAndWounds(sd.scarsAndWounds)
    if (sd.phobiasAndManias) setPhobiasAndManias(sd.phobiasAndManias)
    if (sd.investigatorHistory) setInvestigatorHistory(sd.investigatorHistory)

    // 若结构化字段为空但有自由文本背景故事，填入个人描述
    const hasStructured = sd.personalDescription || sd.ideologyBeliefs || sd.significantPeople
      || sd.meaningfulLocations || sd.treasuredPossessions || sd.traits
    if (!hasStructured && data.backstory) {
      setPersonalDesc(data.backstory)
    }

    if (data.base_attributes && Object.keys(data.base_attributes).length > 0) {
      setUseDice(false)
      setCustomAttrs(data.base_attributes)
    }

    if (data.skills && Object.keys(data.skills).length > 0) {
      setSkillAlloc(data.skills)
    }

    if (data.equipment && data.equipment.length > 0) {
      setEquipText(data.equipment.join('、'))
    }

    if (data.weapons && data.weapons.length > 0) {
      setWeapons(data.weapons.map((w) => normalizeWeapon(w as Record<string, unknown>)))
    }

    // 查找匹配职业；找不到则建为自定义职业
    if (sd.occupation) {
      const match = occupations.find((o) => o.name === sd.occupation)
      if (match) {
        selectOccupation(match)
      } else {
        const importedSkillNames = Object.keys(data.skills || {})
        setSelectedOcc({
          name: String(sd.occupation),
          credit_min: 0,
          credit_max: 99,
          skill_formula: '自定义',
          skills: importedSkillNames,
          choices: 0,
        })
        const totalPoints = Object.values(data.skills || {})
          .reduce((s, v) => s + (typeof v === 'number' ? v : 0), 0)
        setOccPoints(totalPoints)
        setIntPoints(0)
      }
    }

    setIsImported(true)
  }

  const handleExcelImport = async (file: File) => {
    if (!moduleId) {
      toast.error('请先选择模组')
      return
    }
    setImporting(true)
    try {
      const formData = new FormData()
      formData.append('file', file)
      const data = await uploadFile<ImportedCharacterData>(
        `/characters/import-excel?module_id=${encodeURIComponent(moduleId)}`,
        formData,
      )
      applyImportedData(data)
      toast.success(`已导入角色「${data.name}」的数据，请检查后继续`)
    } catch (e) {
      toast.error(e instanceof Error ? e.message : '导入失败')
    } finally {
      setImporting(false)
    }
  }

  const handleAIGenerate = async () => {
    if (!moduleId) {
      toast.error('请先选择模组')
      return
    }
    setAiGenerating(true)
    try {
      const data = await api.post<ImportedCharacterData & { _fallback?: boolean }>(
        '/characters/ai-generate',
        { module_id: moduleId, hint: aiHint.trim() },
      )
      applyImportedData(data)
      setStep('属性设定')
      if (data._fallback) {
        toast.warning('AI 暂时不可用，已用规则生成一张草稿，请检查后调整')
      } else {
        toast.success(`AI 已生成角色「${data.name}」，请逐步检查后创建`)
      }
    } catch (e) {
      toast.error(e instanceof Error ? e.message : 'AI 生成失败')
    } finally {
      setAiGenerating(false)
    }
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

  // 角色列表：条件查询（名/职业/规则）+ 分页
  const charFiltered = characters.filter((c) => {
    const q = charQuery.trim().toLowerCase()
    if (!q) return true
    const occ = String((c.system_data as Record<string, unknown>)?.occupation ?? '').toLowerCase()
    return c.name.toLowerCase().includes(q) || occ.includes(q) || c.rule_system.toLowerCase().includes(q)
  })
  const charTotalPages = Math.max(1, Math.ceil(charFiltered.length / CHAR_PAGE_SIZE))
  const pageClamped = Math.min(charPage, charTotalPages)
  const charPageItems = charFiltered.slice((pageClamped - 1) * CHAR_PAGE_SIZE, pageClamped * CHAR_PAGE_SIZE)

  const allSkillNames = [...new Set([
    ...Object.keys(defaultSkills),
    ...extraSkills,
    ...(isImported ? Object.keys(skillAlloc) : []),
  ])].sort((a, b) => {
    const aIsOcc = occHas(a)
    const bIsOcc = occHas(b)
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
            {inCreateFlow ? (
              <button onClick={() => { setInCreateFlow(false); resetForm() }} className="ml-auto btn-secondary flex items-center gap-1 text-sm">
                <GiReturnArrow /> 返回列表
              </button>
            ) : (
              <button onClick={() => { resetForm(); setInCreateFlow(true) }} className="ml-auto btn-primary flex items-center gap-1 text-sm">
                <GiUpCard /> 创建角色
              </button>
            )}
          </div>

          {inCreateFlow && (
          <div className="card mb-8">
            <h3 className="card-title flex items-center gap-2">
              <GiCharacter /> 创建角色（CoC 七版）
            </h3>

            {/* 步骤指示器 */}
            <div className="flex items-center gap-1 mb-4 text-xs">
              {STEPS.map((s, i) => (
                <div key={s} className="flex items-center gap-1">
                  {i > 0 && <ChevronRight size={12} style={{ color: 'var(--color-border-strong)' }} />}
                  <span
                    className="px-2 py-0.5 rounded"
                    style={{
                      background: i === stepIndex ? 'var(--color-accent)' : i < stepIndex ? 'var(--color-bg-tertiary)' : 'transparent',
                      color: i === stepIndex ? 'var(--color-on-accent)' : i < stepIndex ? 'var(--color-text-primary)' : 'var(--color-text-secondary)',
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
                <div className="flex items-center gap-2 mb-3 p-2 rounded" style={{ background: 'var(--color-bg-tertiary)' }}>
                  <GiUpCard className="text-lg flex-shrink-0" style={{ color: 'var(--color-text-accent)' }} />
                  <span className="text-xs" style={{ color: 'var(--color-text-secondary)' }}>有现成角色卡？</span>
                  <label className="btn-secondary !px-2 !py-0.5 text-xs cursor-pointer">
                    {importing ? '导入中...' : '导入 Excel 角色卡'}
                    <input
                      type="file"
                      accept=".xlsx"
                      className="hidden"
                      disabled={importing}
                      onChange={(e) => {
                        const f = e.target.files?.[0]
                        if (f) handleExcelImport(f)
                        e.target.value = ''
                      }}
                    />
                  </label>
                </div>
                <div className="flex items-center gap-2 mb-3 p-2 rounded" style={{ background: 'var(--color-bg-tertiary)' }}>
                  <GiDiceSixFacesSix className="text-lg flex-shrink-0" style={{ color: 'var(--color-text-accent)' }} />
                  <span className="text-xs flex-shrink-0" style={{ color: 'var(--color-text-secondary)' }}>让 AI 建卡？</span>
                  <input
                    value={aiHint}
                    onChange={(e) => setAiHint(e.target.value)}
                    placeholder="角色概念（可选，如：胆小的图书管理员）"
                    disabled={aiGenerating}
                    className="input flex-1 !py-0.5 text-xs"
                  />
                  <button
                    onClick={handleAIGenerate}
                    disabled={aiGenerating || !moduleId}
                    className="btn-secondary !px-2 !py-0.5 text-xs flex-shrink-0 whitespace-nowrap"
                  >
                    {aiGenerating ? '生成中...' : 'AI 生成角色卡'}
                  </button>
                </div>
                <div className="grid grid-cols-2 gap-3 mb-3">
                  <div>
                    <label className="block text-sm mb-1" style={{ color: 'var(--color-text-secondary)' }}>姓名</label>
                    <input value={name} onChange={(e) => setName(e.target.value)} placeholder="调查员姓名" className="input w-full" />
                  </div>
                  <div>
                    <label className="block text-sm mb-1" style={{ color: 'var(--color-text-secondary)' }}>性别</label>
                    <input value={gender} onChange={(e) => setGender(e.target.value)} placeholder="如：男/女" className="input w-full" />
                  </div>
                </div>
                <div className="grid grid-cols-3 gap-3 mb-3">
                  <div>
                    <label className="block text-sm mb-1" style={{ color: 'var(--color-text-secondary)' }}>年龄</label>
                    <input
                      type="number" value={age} min={15} max={90}
                      onChange={(e) => setAge(Math.max(15, Math.min(90, parseInt(e.target.value) || 25)))}
                      className="input w-full font-mono"
                    />
                  </div>
                  <div>
                    <label className="block text-sm mb-1" style={{ color: 'var(--color-text-secondary)' }}>住地</label>
                    <input value={residence} onChange={(e) => setResidence(e.target.value)} placeholder="现居住地" className="input w-full" />
                  </div>
                  <div>
                    <label className="block text-sm mb-1" style={{ color: 'var(--color-text-secondary)' }}>故乡</label>
                    <input value={birthplace} onChange={(e) => setBirthplace(e.target.value)} placeholder="出生地" className="input w-full" />
                  </div>
                </div>
                {age >= 40 && (
                  <div className="text-xs mb-3 px-2 py-1 rounded" style={{ background: 'rgba(212, 162, 78, 0.12)', color: 'var(--color-text-accent)' }}>
                    注意：年龄 {age} 岁，移动力将减少 {age >= 80 ? 5 : age >= 70 ? 4 : age >= 60 ? 3 : age >= 50 ? 2 : 1} 点
                  </div>
                )}
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
                    {isImported ? (
                      <div className="mb-3 text-xs px-2 py-1.5 rounded" style={{ background: 'var(--color-bg-tertiary)', color: 'var(--color-text-accent)' }}>
                        属性已自动填入，如需可手动微调（导入/AI 角色不受创建点数池限制）
                      </div>
                    ) : (
                      <div className="flex justify-between items-center mb-3 text-sm">
                        <span>总点数池：<strong className="font-mono">{POINT_POOL}</strong></span>
                        <span>
                          剩余：
                          <strong className="font-mono" style={{ color: remainingPoints > 0 ? 'var(--color-success)' : remainingPoints < 0 ? 'var(--color-danger)' : 'var(--color-text-secondary)' }}>
                            {remainingPoints}
                          </strong>
                        </span>
                      </div>
                    )}
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

                {/* 幸运值 */}
                <div className="flex items-center gap-3 mb-3 py-2 px-3 rounded" style={{ background: 'var(--color-bg-tertiary)' }}>
                  <span className="text-sm" style={{ color: 'var(--color-text-secondary)' }}>幸运 LUCK</span>
                  <span className="font-mono font-bold text-sm w-10 text-center">{luck || '—'}</span>
                  <button onClick={rollLuck} className="btn-secondary !px-2 !py-0.5 text-xs flex items-center gap-1">
                    <GiDiceSixFacesSix /> 掷骰 (3D6×5)
                  </button>
                  {luck > 0 && (
                    <span className="text-xs" style={{ color: 'var(--color-text-secondary)' }}>
                      半值 {Math.floor(luck / 2)} / 五分之一 {Math.floor(luck / 5)}
                    </span>
                  )}
                </div>

                {/* 派生属性预览 */}
                {derivedStats && (
                  <div className="mb-3 py-2 px-3 rounded" style={{ background: 'var(--color-bg-tertiary)' }}>
                    <div className="text-xs mb-1.5 font-semibold" style={{ color: 'var(--color-text-accent)' }}>派生属性</div>
                    <div className="grid grid-cols-4 gap-2 text-sm">
                      <div><span style={{ color: 'var(--color-text-secondary)' }}>HP </span><span className="font-mono font-bold">{derivedStats.hp}</span></div>
                      <div><span style={{ color: 'var(--color-text-secondary)' }}>SAN </span><span className="font-mono font-bold">{derivedStats.san}</span></div>
                      <div><span style={{ color: 'var(--color-text-secondary)' }}>MP </span><span className="font-mono font-bold">{derivedStats.mp}</span></div>
                      <div><span style={{ color: 'var(--color-text-secondary)' }}>MOV </span><span className="font-mono font-bold">{derivedStats.mov}</span></div>
                      <div><span style={{ color: 'var(--color-text-secondary)' }}>伤害加值 </span><span className="font-mono font-bold">{derivedStats.db}</span></div>
                      <div><span style={{ color: 'var(--color-text-secondary)' }}>体格 </span><span className="font-mono font-bold">{derivedStats.build}</span></div>
                      <div><span style={{ color: 'var(--color-text-secondary)' }}>闪避 </span><span className="font-mono font-bold">{derivedStats.dodge}</span></div>
                    </div>
                  </div>
                )}

                <div className="flex gap-2">
                  <button onClick={() => setStep('基本信息')} className="btn-secondary">上一步</button>
                  <button
                    onClick={() => setStep('职业选择')}
                    disabled={isImported ? false : ((useDice ? !selectedAttrs : remainingPoints < 0) || luck <= 0)}
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
                {isImported && selectedOcc && !occupations.some((o) => o.name === selectedOcc.name) && (
                  <div className="card mb-3 !bg-[var(--color-bg-tertiary)]" style={{ borderColor: 'var(--color-accent)' }}>
                    <div className="font-semibold" style={{ color: 'var(--color-text-accent)' }}>
                      {selectedOcc.name}
                      <span className="text-xs font-normal ml-2" style={{ color: 'var(--color-text-secondary)' }}>（自定义职业，从角色卡导入）</span>
                    </div>
                    <div className="text-xs mt-1" style={{ color: 'var(--color-text-secondary)' }}>
                      也可从下方列表选择系统内置职业替换
                    </div>
                  </div>
                )}
                {/* 职业大类：先选大类，再在类下挑选 */}
                {occCategories.length > 0 && (
                  <div className="flex flex-wrap gap-1.5 mb-2">
                    {['', ...occCategories].map((c) => {
                      const active = occCat === c
                      return (
                        <button
                          key={c || 'all'}
                          onClick={() => setOccCat(c)}
                          className="text-xs px-2 py-1 rounded border transition-colors"
                          style={{
                            borderColor: active ? 'var(--color-accent)' : 'var(--color-border)',
                            background: active ? 'var(--color-accent)' : 'var(--color-bg-tertiary)',
                            color: active ? 'var(--color-on-accent)' : 'var(--color-text-primary)',
                          }}
                        >{c || '全部'}</button>
                      )
                    })}
                  </div>
                )}
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
                    <div className="mt-2 pt-2 border-t" style={{ borderColor: 'var(--color-border)' }}>
                      <label className="block text-xs mb-1" style={{ color: 'var(--color-text-secondary)' }}>
                        信用评级（{selectedOcc.credit_min}-{selectedOcc.credit_max}）
                      </label>
                      <div className="flex items-center gap-2">
                        <input
                          type="range"
                          min={selectedOcc.credit_min}
                          max={selectedOcc.credit_max}
                          value={creditRating}
                          onChange={(e) => setCreditRating(parseInt(e.target.value))}
                          className="flex-1"
                          style={{ accentColor: 'var(--color-accent)' }}
                        />
                        <span className="font-mono font-bold text-sm w-10 text-right">{creditRating}%</span>
                      </div>
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
                {isImported ? (
                  <div className="text-xs mb-3 px-2 py-1.5 rounded" style={{ background: 'var(--color-bg-tertiary)', color: 'var(--color-text-accent)' }}>
                    导入模式：技能值已从角色卡导入，可自由调整
                  </div>
                ) : (
                  <div className="flex gap-4 mb-3 text-sm">
                    <span>
                      职业点剩余：<strong className="font-mono" style={{ color: remainingOcc > 0 ? 'var(--color-success)' : 'var(--color-text-secondary)' }}>{remainingOcc}</strong>
                    </span>
                    <span>
                      兴趣点剩余：<strong className="font-mono" style={{ color: remainingInt > 0 ? 'var(--color-success)' : 'var(--color-text-secondary)' }}>{remainingInt}</strong>
                    </span>
                  </div>
                )}

                <div className="flex flex-wrap items-center gap-1.5 mb-2">
                  <span className="text-xs" style={{ color: 'var(--color-text-secondary)' }}>添加专精：</span>
                  {SPEC_BASES.map((b) => (
                    <button
                      key={b}
                      onClick={() => setSpecBase(b)}
                      disabled={!spec}
                      className="text-xs px-2 py-0.5 rounded border transition-colors hover:bg-[var(--color-accent)] hover:text-[var(--color-on-accent)]"
                      style={{ borderColor: 'var(--color-border)', color: 'var(--color-text-accent)' }}
                    >{b}</button>
                  ))}
                </div>

                <div className="max-h-72 overflow-auto space-y-0.5 mb-3">
                  {allSkillNames.map((skillName) => {
                    const base = defaultSkills[skillName] ?? specBaseVals[skillName] ?? 0
                    const alloc = skillAlloc[skillName] || 0
                    const isOcc = occHas(skillName)
                    const displayVal = isImported ? alloc : base + alloc
                    const isLocked = NON_ALLOCATABLE_SKILLS.includes(skillName)
                    return (
                      <div
                        key={skillName}
                        className="flex items-center justify-between py-1 px-2 rounded text-sm"
                        style={{ background: isOcc ? 'rgba(212, 162, 78, 0.08)' : undefined }}
                      >
                        <div className="flex items-center gap-2">
                          {!isImported && isOcc && <span className="text-xs" style={{ color: 'var(--color-accent)' }}>职</span>}
                          <span>{skillName}</span>
                          {isLocked && <GiPadlock className="text-xs" style={{ color: 'var(--color-text-secondary)' }} />}
                          {!isImported && !isLocked && (
                            <span className="text-xs font-mono" style={{ color: 'var(--color-text-secondary)' }}>
                              ({base}{alloc > 0 ? `+${alloc}` : ''})
                            </span>
                          )}
                        </div>
                        <div className="flex items-center gap-1">
                          <span className="font-mono font-bold w-8 text-right">{displayVal}</span>
                          {!isLocked && (
                            <>
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
                                disabled={!isImported && (isOcc ? remainingOcc : remainingInt) < 5}
                                className="w-6 h-6 rounded text-xs border flex items-center justify-center"
                                style={{ borderColor: 'var(--color-border)', opacity: (!isImported && (isOcc ? remainingOcc : remainingInt) < 5) ? 0.3 : 1 }}
                              >
                                +
                              </button>
                            </>
                          )}
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
                <div className="space-y-3 mb-3">
                  <div>
                    <label className="block text-sm mb-1" style={{ color: 'var(--color-text-secondary)' }}>个人描述</label>
                    <textarea
                      value={personalDesc}
                      onChange={(e) => { setPersonalDesc(e.target.value); setEvalResult(null) }}
                      placeholder="外貌、穿着、气质等外在特征..."
                      rows={2} className="input w-full" style={{ resize: 'vertical' }}
                    />
                  </div>
                  <div>
                    <label className="block text-sm mb-1" style={{ color: 'var(--color-text-secondary)' }}>思想 / 信念</label>
                    <textarea
                      value={ideologyBeliefs}
                      onChange={(e) => { setIdeologyBeliefs(e.target.value); setEvalResult(null) }}
                      placeholder="角色信奉的理念、宗教信仰或价值观..."
                      rows={2} className="input w-full" style={{ resize: 'vertical' }}
                    />
                  </div>
                  <div>
                    <label className="block text-sm mb-1" style={{ color: 'var(--color-text-secondary)' }}>重要之人</label>
                    <textarea
                      value={significantPeople}
                      onChange={(e) => { setSignificantPeople(e.target.value); setEvalResult(null) }}
                      placeholder="对角色最重要的人，以及为什么重要..."
                      rows={2} className="input w-full" style={{ resize: 'vertical' }}
                    />
                  </div>
                  <div>
                    <label className="block text-sm mb-1" style={{ color: 'var(--color-text-secondary)' }}>意义非凡之地</label>
                    <textarea
                      value={meaningfulLocations}
                      onChange={(e) => { setMeaningfulLocations(e.target.value); setEvalResult(null) }}
                      placeholder="对角色有特殊意义的地点..."
                      rows={2} className="input w-full" style={{ resize: 'vertical' }}
                    />
                  </div>
                  <div>
                    <label className="block text-sm mb-1" style={{ color: 'var(--color-text-secondary)' }}>宝贵之物</label>
                    <textarea
                      value={treasuredPossessions}
                      onChange={(e) => { setTreasuredPossessions(e.target.value); setEvalResult(null) }}
                      placeholder="角色珍视的物品..."
                      rows={2} className="input w-full" style={{ resize: 'vertical' }}
                    />
                  </div>
                  <div>
                    <label className="block text-sm mb-1" style={{ color: 'var(--color-text-secondary)' }}>特点</label>
                    <textarea
                      value={traits}
                      onChange={(e) => { setTraits(e.target.value); setEvalResult(null) }}
                      placeholder="性格特点、习惯、口头禅等..."
                      rows={2} className="input w-full" style={{ resize: 'vertical' }}
                    />
                  </div>
                  <div>
                    <label className="block text-sm mb-1" style={{ color: 'var(--color-text-secondary)' }}>伤口 / 疤痕</label>
                    <textarea
                      value={scarsAndWounds}
                      onChange={(e) => { setScarsAndWounds(e.target.value); setEvalResult(null) }}
                      placeholder="角色身上的伤疤或旧伤..."
                      rows={1} className="input w-full" style={{ resize: 'vertical' }}
                    />
                  </div>
                  <div>
                    <label className="block text-sm mb-1" style={{ color: 'var(--color-text-secondary)' }}>恐惧症 / 狂躁症</label>
                    <textarea
                      value={phobiasAndManias}
                      onChange={(e) => { setPhobiasAndManias(e.target.value); setEvalResult(null) }}
                      placeholder="角色的恐惧或狂躁倾向..."
                      rows={1} className="input w-full" style={{ resize: 'vertical' }}
                    />
                  </div>
                  <div>
                    <label className="block text-sm mb-1" style={{ color: 'var(--color-text-secondary)' }}>调查员经历</label>
                    <textarea
                      value={investigatorHistory}
                      onChange={(e) => { setInvestigatorHistory(e.target.value); setEvalResult(null) }}
                      placeholder="曾经历的模组、事件记录..."
                      rows={2} className="input w-full" style={{ resize: 'vertical' }}
                    />
                  </div>
                </div>

                {/* 资产信息（按信用评级换算，可手改） */}
                <div className="border-t pt-3 mb-3" style={{ borderColor: 'var(--color-border)' }}>
                  <h4 className="text-sm font-semibold mb-2" style={{ color: 'var(--color-text-accent)' }}>资产信息</h4>
                  <AssetsPanel creditRating={creditRating} value={assetsInfo} onChange={setAssetsInfo} />
                </div>

                {/* 克苏鲁神话 */}
                <div className="border-t pt-3 mb-3" style={{ borderColor: 'var(--color-border)' }}>
                  <h4 className="text-sm font-semibold mb-2" style={{ color: 'var(--color-text-accent)' }}>克苏鲁神话</h4>
                  <MythosEditor value={mythos} onChange={setMythos} />
                </div>

                {/* 人际关系 */}
                <div className="border-t pt-3 mb-3" style={{ borderColor: 'var(--color-border)' }}>
                  <h4 className="text-sm font-semibold mb-2" style={{ color: 'var(--color-text-accent)' }}>人际关系</h4>
                  <RelationsEditor value={relations} onChange={setRelations} />
                </div>

                {/* 模组经历 */}
                <div className="border-t pt-3 mb-3" style={{ borderColor: 'var(--color-border)' }}>
                  <h4 className="text-sm font-semibold mb-2" style={{ color: 'var(--color-text-accent)' }}>模组经历</h4>
                  <ModuleHistoryEditor value={moduleHistory} onChange={setModuleHistory} />
                </div>

                <div className="flex gap-2">
                  <button onClick={() => setStep('技能加点')} className="btn-secondary">上一步</button>
                  <button onClick={() => setStep('随身物品')} className="btn-primary">下一步</button>
                </div>
              </div>
            )}

            {/* Step 6: 随身物品 */}
            {step === '随身物品' && (
              <div>
                {/* 武器：从武器表挑选或手动添加，规范九字段 */}
                <div className="mb-4">
                  <WeaponsEditor weapons={weapons} onChange={setWeapons} skillValueOf={skillValueOf} />
                </div>

                {/* 随身物品与装备：自由填写，以、（顿号）分隔 */}
                <div className="mb-3">
                  <h4 className="text-sm font-semibold mb-1" style={{ color: 'var(--color-text-accent)' }}>随身物品与装备</h4>
                  <p className="text-xs mb-1.5" style={{ color: 'var(--color-text-secondary)', opacity: 0.8 }}>
                    自由填写，多个物品以、（顿号）分隔
                  </p>
                  <textarea
                    value={equipText}
                    onChange={(e) => setEquipText(e.target.value)}
                    rows={3}
                    placeholder="如：怀表、笔记本与钢笔、手电筒、绳索"
                    className="w-full px-2 py-1 rounded text-sm resize-y"
                    style={{ background: 'var(--color-bg-tertiary)', border: '1px solid var(--color-border)' }}
                  />
                </div>

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
                    <button onClick={() => setStep('背景故事')} className="btn-secondary">上一步</button>
                    <button onClick={evaluateAndCreate} disabled={evaluating || creating} className="btn-primary">
                      {evaluating ? 'AI 评估中...' : creating ? '创建中...' : '完成创建'}
                    </button>
                  </div>
                )}
              </div>
            )}
          </div>

          )}

          {/* 角色列表（默认视图）：条件查询 + 分页 */}
          {!inCreateFlow && (
          <div>
            <div className="flex items-center gap-2 mb-3">
              <input
                value={charQuery}
                onChange={(e) => { setCharQuery(e.target.value); setCharPage(1) }}
                placeholder="搜索角色名 / 职业 / 规则…"
                className="input flex-1"
              />
              <span className="text-xs whitespace-nowrap" style={{ color: 'var(--color-text-secondary)' }}>
                {charFiltered.length} 个角色
              </span>
            </div>
          <div className="space-y-3">
            {charPageItems.length === 0 && (
              <p className="text-sm py-6 text-center" style={{ color: 'var(--color-text-secondary)' }}>
                {characters.length === 0 ? '暂无角色，点右上角「创建角色」开始' : '没有匹配的角色'}
              </p>
            )}
            {charPageItems.map((c) => {
              const hp = (c.system_data?.hitPoints as { current: number; max: number }) || { current: 0, max: 0 }
              const san = (c.system_data?.sanity as { current: number; max: number }) || { current: 0, max: 0 }
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
                      <button
                        onClick={(e) => { e.stopPropagation(); setEditingChar(c) }}
                        className="text-xs px-1.5 py-0.5 rounded transition-colors hover:bg-[var(--color-accent)] hover:text-[var(--color-on-accent)]"
                        style={{ color: 'var(--color-text-accent)', border: '1px solid var(--color-border)' }}
                      >
                        编辑
                      </button>
                      <ConfirmDialog
                        title="删除角色"
                        description={`确定要删除「${c.name}」吗？此操作不可恢复。`}
                        confirmLabel="删除"
                        onConfirm={() => deleteCharacter(c.id)}
                      >
                        {(open) => (
                          <button
                            onClick={(e) => { e.stopPropagation(); open() }}
                            className="text-xs px-1.5 py-0.5 rounded hover:bg-[var(--color-danger-deep)] hover:text-white transition-colors"
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

          {charTotalPages > 1 && (
            <div className="flex items-center justify-center gap-2 mt-4 text-sm">
              <button
                onClick={() => setCharPage((p) => Math.max(1, p - 1))}
                disabled={pageClamped <= 1}
                className="btn-secondary !px-2 !py-1 disabled:opacity-40"
              >上一页</button>
              <span style={{ color: 'var(--color-text-secondary)' }}>{pageClamped} / {charTotalPages}</span>
              <button
                onClick={() => setCharPage((p) => Math.min(charTotalPages, p + 1))}
                disabled={pageClamped >= charTotalPages}
                className="btn-secondary !px-2 !py-1 disabled:opacity-40"
              >下一页</button>
            </div>
          )}
          </div>
          )}
        </div>
      </div>

      {/* 右侧角色详情面板 */}
      {selectedChar && (
        <aside
          className="w-72 flex-shrink-0 border-l overflow-y-auto"
          style={{ borderColor: 'var(--color-border)', background: 'var(--color-bg-card)' }}
        >
          <CharacterPanel character={selectedChar} />
        </aside>
      )}

      {editingChar && (
        <CharacterEditModal
          character={editingChar}
          open={!!editingChar}
          onOpenChange={(v) => { if (!v) setEditingChar(null) }}
          onSaved={(updated) => {
            setCharacters((prev) => prev.map((c) => (c.id === updated.id ? { ...c, ...updated } : c)))
            setSelectedChar((prev) => (prev && prev.id === updated.id ? { ...prev, ...updated } : prev))
            setEditingChar(null)
          }}
        />
      )}

      {/* 创建向导：专精选择弹窗 */}
      {specBase && (
        <SpecializationDialog
          base={specBase}
          open={!!specBase}
          onOpenChange={(v) => { if (!v) setSpecBase('') }}
          disabledItems={[...Object.keys(defaultSkills), ...extraSkills]
            .filter((k) => k.startsWith(`${specBase}(`))
            .map((k) => k.slice(specBase.length + 1, -1))}
          onConfirm={(specName, init) => addSpecialization(specBase, specName, init)}
        />
      )}
    </div>
  )
}
