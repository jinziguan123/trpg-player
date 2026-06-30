import { useState } from 'react'
import { toast } from 'sonner'
import { api } from '../../api/client'
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogFooter } from '@/components/ui/dialog'
import { Tabs, TabsList, TabsTrigger, TabsContent } from '@/components/ui/tabs'
import { GiCancel, GiCheckMark } from 'react-icons/gi'
import { SpecializationDialog } from './SpecializationDialog'
import { WeaponsEditor } from './WeaponsEditor'
import { useSpecializations, normalizeWeapon, type CharWeapon } from './useCocData'
import {
  AssetsPanel, MythosEditor, RelationsEditor, ModuleHistoryEditor,
  readAssets, readMythos, readRelations, readModuleHistory,
  type AssetsInfo, type Mythos, type Relation, type ModuleExperience,
} from './CharacterExtraEditors'

interface CharacterData {
  id: string
  name: string
  base_attributes: Record<string, number>
  skills: Record<string, number>
  system_data: Record<string, unknown>
  backstory: string
  status: string
}

const ATTR_KEYS = ['STR', 'CON', 'SIZ', 'DEX', 'APP', 'INT', 'POW', 'EDU'] as const
const ATTR_LABELS: Record<string, string> = {
  STR: '力量', CON: '体质', SIZ: '体型', DEX: '敏捷',
  APP: '外貌', INT: '智力', POW: '意志', EDU: '教育',
}

// 专精基名（与后端 SPECIALIZATIONS 对齐）
const SPEC_BASES = ['母语', '外语', '格斗', '射击', '科学', '生存', '技艺', '驾驶']

// 与后端 app/rules/coc/status.py 对齐
const STATUS_OPTIONS: { value: string; label: string }[] = [
  { value: 'active', label: '正常' },
  { value: 'major_wound', label: '重伤' },
  { value: 'unconscious', label: '昏迷' },
  { value: 'dead', label: '死亡' },
  { value: 'temporary_insanity', label: '临时疯狂' },
  { value: 'indefinite_insanity', label: '不定期疯狂' },
  { value: 'permanent_insanity', label: '永久疯狂' },
]
// 兼容历史 incapacitated（旧义≈重伤）
const STATUS_FALLBACK: Record<string, string> = { incapacitated: 'major_wound' }

// 派生数值（system_data 顶层标量字段）
const SCALAR_FIELDS: { key: string; label: string; type: 'number' | 'text' }[] = [
  { key: 'luck', label: '幸运', type: 'number' },
  { key: 'move', label: '移动力', type: 'number' },
  { key: 'damageBonus', label: '伤害加值', type: 'text' },
  { key: 'build', label: '体格', type: 'number' },
  { key: 'creditRating', label: '信用评级', type: 'number' },
  { key: 'occupation', label: '职业', type: 'text' },
  { key: 'age', label: '年龄', type: 'number' },
  { key: 'gender', label: '性别', type: 'text' },
  { key: 'residence', label: '居住地', type: 'text' },
  { key: 'birthplace', label: '出生地', type: 'text' },
]

const VITAL_FIELDS: { key: string; label: string }[] = [
  { key: 'hitPoints', label: 'HP' },
  { key: 'sanity', label: 'SAN' },
  { key: 'magicPoints', label: 'MP' },
]

// 装备历史上可能是字符串数组或对象数组，统一取名字
function equipmentName(item: unknown): string {
  if (typeof item === 'string') return item
  if (item && typeof item === 'object' && 'name' in item) return String((item as { name: unknown }).name ?? '')
  return ''
}

const BACKSTORY_SECTIONS: { key: string; label: string }[] = [
  { key: 'personalDescription', label: '个人描述' },
  { key: 'ideologyBeliefs', label: '思想/信念' },
  { key: 'significantPeople', label: '重要之人' },
  { key: 'meaningfulLocations', label: '意义非凡之地' },
  { key: 'treasuredPossessions', label: '宝贵之物' },
  { key: 'traits', label: '特点' },
]

const inputCls = 'w-full px-2 py-1 rounded text-sm'
const inputStyle = { background: 'var(--color-bg-tertiary)', border: '1px solid var(--color-border)' }

function NumInput({ value, onChange }: { value: number; onChange: (v: number) => void }) {
  return (
    <input
      type="number"
      value={Number.isFinite(value) ? value : 0}
      onChange={(e) => onChange(Number(e.target.value) || 0)}
      className={inputCls + ' font-mono'}
      style={inputStyle}
    />
  )
}

export function CharacterEditModal({
  character, open, onOpenChange, onSaved,
}: {
  character: CharacterData
  open: boolean
  onOpenChange: (v: boolean) => void
  onSaved: (c: CharacterData) => void
}) {
  const sd = character.system_data || {}
  const spec = useSpecializations()
  const [name, setName] = useState(character.name)
  const [status, setStatus] = useState(STATUS_FALLBACK[character.status] || character.status || 'active')
  const [attrs, setAttrs] = useState<Record<string, number>>({ ...character.base_attributes })
  const [skills, setSkills] = useState<[string, number][]>(
    Object.entries(character.skills || {}).sort((a, b) => b[1] - a[1]),
  )
  const [newSkill, setNewSkill] = useState('')
  const [specBase, setSpecBase] = useState<string>('')   // 当前打开专精弹窗的基名
  // 派生标量
  const [scalars, setScalars] = useState<Record<string, string | number>>(() => {
    const o: Record<string, string | number> = {}
    for (const f of SCALAR_FIELDS) o[f.key] = (sd[f.key] as string | number) ?? (f.type === 'number' ? 0 : '')
    return o
  })
  // 三维生命值 current/max
  const [vitals, setVitals] = useState<Record<string, { current: number; max: number }>>(() => {
    const o: Record<string, { current: number; max: number }> = {}
    for (const v of VITAL_FIELDS) {
      const raw = sd[v.key] as { current?: number; max?: number } | undefined
      o[v.key] = { current: raw?.current ?? 0, max: raw?.max ?? 0 }
    }
    return o
  })
  // 随身物品：自由文本，以、（顿号）分隔
  const [equipText, setEquipText] = useState(
    (Array.isArray(sd.equipment) ? sd.equipment : []).map(equipmentName).filter(Boolean).join('、'),
  )
  const [weapons, setWeapons] = useState<CharWeapon[]>(
    (Array.isArray(sd.weapons) ? sd.weapons : []).map((w) => normalizeWeapon(w as Record<string, unknown>)),
  )
  const [backstory, setBackstory] = useState(character.backstory || '')
  const [sections, setSections] = useState<Record<string, string>>(() => {
    const o: Record<string, string> = {}
    for (const s of BACKSTORY_SECTIONS) o[s.key] = (sd[s.key] as string) || ''
    return o
  })
  const [assetsInfo, setAssetsInfo] = useState<AssetsInfo>(() => readAssets(sd))
  const [mythos, setMythos] = useState<Mythos>(() => readMythos(sd))
  const [relations, setRelations] = useState<Relation[]>(() => readRelations(sd))
  const [moduleHistory, setModuleHistory] = useState<ModuleExperience[]>(() => readModuleHistory(sd))
  const [saving, setSaving] = useState(false)

  // 按使用技能取当前成功率（用于武器自动填成功率）
  const skillValueOf = (skillKey: string): number => {
    const hit = skills.find(([k]) => k === skillKey)
    return hit ? hit[1] : 0
  }
  // 信用评级取自「信用评级」技能（回退到旧的 system_data.creditRating）
  const creditRating = skillValueOf('信用评级') || Number(scalars.creditRating) || 0

  // 选中专精 → 落为「基名(专精)」技能；母语值=EDU，其余用专精 init
  const addSpecialization = (base: string, specName: string, init: number) => {
    const key = `${base}(${specName})`
    if (skills.some(([k]) => k === key)) { toast.error(`已存在「${key}」`); return }
    const value = base === '母语' ? (attrs.EDU || init) : init
    setSkills([...skills, [key, value]])
    toast.success(`已添加 ${key}`)
  }

  const save = async () => {
    if (!name.trim()) { toast.error('角色名不能为空'); return }
    setSaving(true)
    // 保留未编辑的 system_data 字段，仅覆盖本表单管理的键
    const newSd: Record<string, unknown> = { ...sd }
    for (const f of SCALAR_FIELDS) newSd[f.key] = scalars[f.key]
    for (const v of VITAL_FIELDS) newSd[v.key] = vitals[v.key]
    for (const s of BACKSTORY_SECTIONS) {
      if (sections[s.key].trim()) newSd[s.key] = sections[s.key]
      else delete newSd[s.key]
    }
    // 物品：按、（顿号，兼容中英文逗号）切分
    newSd.equipment = equipText.split(/[、,，]/).map((e) => e.trim()).filter(Boolean)
    newSd.weapons = weapons
      .filter((w) => w.name.trim())
      .map((w) => ({ ...w, name: w.name.trim() }))
    // 资产 / 克苏鲁神话 / 人际关系 / 模组经历
    newSd.cash = assetsInfo.cash
    newSd.spendingLevel = assetsInfo.spendingLevel
    newSd.assets = assetsInfo.assets
    newSd.creditRating = creditRating
    newSd.mythos = {
      spells: mythos.spells.map((s) => s.trim()).filter(Boolean),
      tomes: mythos.tomes.map((s) => s.trim()).filter(Boolean),
      encounters: mythos.encounters.map((s) => s.trim()).filter(Boolean),
    }
    newSd.relations = relations.filter((r) => r.name.trim() || r.relation.trim())
    newSd.moduleHistory = moduleHistory.filter((m) => m.module.trim() || m.experience.trim())
    const payload = {
      name: name.trim(),
      status,
      base_attributes: attrs,
      skills: Object.fromEntries(skills.filter(([k]) => k.trim())),
      system_data: newSd,
      backstory,
    }
    try {
      const updated = await api.put<CharacterData>(`/characters/${character.id}`, payload)
      toast.success('角色卡已保存')
      onSaved(updated)
      onOpenChange(false)
    } catch {
      toast.error('保存失败')
    } finally {
      setSaving(false)
    }
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="!max-w-2xl !max-h-[85vh] flex flex-col overflow-hidden">
        <DialogHeader>
          <DialogTitle>编辑角色卡</DialogTitle>
        </DialogHeader>

        <Tabs defaultValue="基本" className="flex-1 min-h-0 flex flex-col">
          <TabsList>
            {['基本', '属性', '技能', '资产', '道具', '神话', '关系', '经历', '背景'].map((t) => (
              <TabsTrigger key={t} value={t}>{t}</TabsTrigger>
            ))}
          </TabsList>

          <div className="flex-1 overflow-y-auto pr-1 mt-2" style={{ maxHeight: '55vh' }}>
            {/* 基本：名称、状态、派生数值、三维 */}
            <TabsContent value="基本">
              <div className="grid grid-cols-2 gap-3">
                <label className="text-sm col-span-2">
                  <span className="block mb-1" style={{ color: 'var(--color-text-secondary)' }}>角色名</span>
                  <input value={name} onChange={(e) => setName(e.target.value)} className={inputCls} style={inputStyle} />
                </label>
                <label className="text-sm">
                  <span className="block mb-1" style={{ color: 'var(--color-text-secondary)' }}>状态</span>
                  <select value={status} onChange={(e) => setStatus(e.target.value)} className={inputCls} style={inputStyle}>
                    {STATUS_OPTIONS.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
                  </select>
                </label>
                <div />
                {VITAL_FIELDS.map((v) => (
                  <div key={v.key} className="text-sm">
                    <span className="block mb-1" style={{ color: 'var(--color-text-secondary)' }}>{v.label}（当前 / 上限）</span>
                    <div className="flex items-center gap-1">
                      <NumInput value={vitals[v.key].current} onChange={(n) => setVitals({ ...vitals, [v.key]: { ...vitals[v.key], current: n } })} />
                      <span style={{ color: 'var(--color-text-secondary)' }}>/</span>
                      <NumInput value={vitals[v.key].max} onChange={(n) => setVitals({ ...vitals, [v.key]: { ...vitals[v.key], max: n } })} />
                    </div>
                  </div>
                ))}
                {SCALAR_FIELDS.map((f) => (
                  <label key={f.key} className="text-sm">
                    <span className="block mb-1" style={{ color: 'var(--color-text-secondary)' }}>{f.label}</span>
                    {f.type === 'number' ? (
                      <NumInput value={Number(scalars[f.key]) || 0} onChange={(n) => setScalars({ ...scalars, [f.key]: n })} />
                    ) : (
                      <input value={String(scalars[f.key] ?? '')} onChange={(e) => setScalars({ ...scalars, [f.key]: e.target.value })} className={inputCls} style={inputStyle} />
                    )}
                  </label>
                ))}
              </div>
            </TabsContent>

            {/* 属性：八维 */}
            <TabsContent value="属性">
              <div className="grid grid-cols-4 gap-3">
                {ATTR_KEYS.map((k) => (
                  <label key={k} className="text-sm">
                    <span className="block mb-1" style={{ color: 'var(--color-text-secondary)' }}>{ATTR_LABELS[k]}（{k}）</span>
                    <NumInput value={attrs[k] || 0} onChange={(n) => setAttrs({ ...attrs, [k]: n })} />
                  </label>
                ))}
              </div>
            </TabsContent>

            {/* 技能：增删改值 + 专精添加 */}
            <TabsContent value="技能">
              <div className="space-y-1.5">
                <div className="flex flex-wrap items-center gap-1.5 pb-1">
                  <span className="text-xs" style={{ color: 'var(--color-text-secondary)' }}>添加专精：</span>
                  {SPEC_BASES.map((b) => (
                    <button
                      key={b}
                      onClick={() => setSpecBase(b)}
                      disabled={!spec}
                      className="text-xs px-2 py-0.5 rounded border transition-colors hover:bg-[var(--color-accent)] hover:text-white"
                      style={{ borderColor: 'var(--color-border)', color: 'var(--color-text-accent)' }}
                    >{b}</button>
                  ))}
                </div>
                {skills.map(([sk, val], i) => (
                  <div key={sk + i} className="flex items-center gap-2">
                    <input
                      value={sk}
                      onChange={(e) => { const next = [...skills]; next[i] = [e.target.value, val]; setSkills(next) }}
                      className={inputCls + ' flex-1'}
                      style={inputStyle}
                    />
                    <div className="w-20">
                      <NumInput value={val} onChange={(n) => { const next = [...skills]; next[i] = [sk, n]; setSkills(next) }} />
                    </div>
                    <button
                      onClick={() => setSkills(skills.filter((_, j) => j !== i))}
                      className="text-xs px-2 py-1 rounded hover:bg-[var(--color-danger)] hover:text-white transition-colors"
                      style={{ color: 'var(--color-danger)', border: '1px solid var(--color-danger)' }}
                    >删除</button>
                  </div>
                ))}
                <div className="flex items-center gap-2 pt-2">
                  <input
                    value={newSkill}
                    onChange={(e) => setNewSkill(e.target.value)}
                    placeholder="新增技能名…"
                    className={inputCls + ' flex-1'}
                    style={inputStyle}
                  />
                  <button
                    onClick={() => { if (newSkill.trim()) { setSkills([...skills, [newSkill.trim(), 0]]); setNewSkill('') } }}
                    className="btn-secondary text-sm"
                  >添加</button>
                </div>
              </div>
            </TabsContent>

            {/* 资产：信用评级 + 现金/消费水平/资产情况 */}
            <TabsContent value="资产">
              <AssetsPanel creditRating={creditRating} value={assetsInfo} onChange={setAssetsInfo} />
            </TabsContent>

            {/* 道具：武器（规范字段）+ 随身物品（自由文本） */}
            <TabsContent value="道具">
              <div className="space-y-4">
                <WeaponsEditor weapons={weapons} onChange={setWeapons} skillValueOf={skillValueOf} />

                <div>
                  <h4 className="text-sm font-semibold mb-1" style={{ color: 'var(--color-text-accent)' }}>随身物品与装备</h4>
                  <p className="text-xs mb-1.5" style={{ color: 'var(--color-text-secondary)', opacity: 0.8 }}>
                    自由填写，多个物品以、（顿号）分隔
                  </p>
                  <textarea
                    value={equipText}
                    onChange={(e) => setEquipText(e.target.value)}
                    rows={3}
                    placeholder="如：怀表、笔记本与钢笔、手电筒、绳索"
                    className={inputCls + ' resize-y'}
                    style={inputStyle}
                  />
                </div>
              </div>
            </TabsContent>

            {/* 克苏鲁神话：法术 / 魔法物品与典籍 / 第三类接触 */}
            <TabsContent value="神话">
              <MythosEditor value={mythos} onChange={setMythos} />
            </TabsContent>

            {/* 人际关系 */}
            <TabsContent value="关系">
              <RelationsEditor value={relations} onChange={setRelations} />
            </TabsContent>

            {/* 模组经历 */}
            <TabsContent value="经历">
              <ModuleHistoryEditor value={moduleHistory} onChange={setModuleHistory} />
            </TabsContent>

            {/* 背景：分项 + 纯文本 */}
            <TabsContent value="背景">
              <div className="space-y-3">
                {BACKSTORY_SECTIONS.map((s) => (
                  <label key={s.key} className="text-sm block">
                    <span className="block mb-1" style={{ color: 'var(--color-text-secondary)' }}>{s.label}</span>
                    <textarea
                      value={sections[s.key]}
                      onChange={(e) => setSections({ ...sections, [s.key]: e.target.value })}
                      rows={2}
                      className={inputCls + ' resize-y'}
                      style={inputStyle}
                    />
                  </label>
                ))}
                <label className="text-sm block">
                  <span className="block mb-1" style={{ color: 'var(--color-text-secondary)' }}>背景故事（纯文本）</span>
                  <textarea
                    value={backstory}
                    onChange={(e) => setBackstory(e.target.value)}
                    rows={4}
                    className={inputCls + ' resize-y'}
                    style={inputStyle}
                  />
                </label>
              </div>
            </TabsContent>
          </div>
        </Tabs>

        <DialogFooter>
          <button onClick={() => onOpenChange(false)} className="btn-secondary flex items-center gap-1 text-sm">
            <GiCancel /> 取消
          </button>
          <button onClick={save} disabled={saving} className="btn-primary flex items-center gap-1 text-sm">
            <GiCheckMark /> {saving ? '保存中…' : '保存'}
          </button>
        </DialogFooter>
      </DialogContent>

      {/* 专精选择弹窗 */}
      {specBase && (
        <SpecializationDialog
          base={specBase}
          open={!!specBase}
          onOpenChange={(v) => { if (!v) setSpecBase('') }}
          disabledItems={skills
            .map(([k]) => k)
            .filter((k) => k.startsWith(`${specBase}(`))
            .map((k) => k.slice(specBase.length + 1, -1))}
          onConfirm={(specName, init) => addSpecialization(specBase, specName, init)}
        />
      )}
    </Dialog>
  )
}
