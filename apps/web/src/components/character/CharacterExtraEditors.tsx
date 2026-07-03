import { deriveAssets } from './useCocData'

const inputCls = 'w-full px-2 py-1 rounded text-sm'
const inputStyle = { background: 'var(--color-bg-tertiary)', border: '1px solid var(--color-border)' }
const labelStyle = { color: 'var(--color-text-secondary)' }
const delBtn = 'text-xs px-2 py-1 rounded hover:bg-[var(--color-danger-deep)] hover:text-white transition-colors'
const delStyle = { color: 'var(--color-danger)', border: '1px solid var(--color-danger)' }

export interface Relation { name: string; relation: string }
export interface ModuleExperience { module: string; experience: string }
export interface Mythos { spells: string[]; tomes: string[]; encounters: string[] }
export interface AssetsInfo { cash: number; spendingLevel: number; assets: string }

// ---- 资产信息：信用评级（只读，来自技能）+ 现金/消费水平/资产情况，可按信用换算 ----
export function AssetsPanel({
  creditRating, value, onChange,
}: {
  creditRating: number
  value: AssetsInfo
  onChange: (v: AssetsInfo) => void
}) {
  const d = deriveAssets(creditRating)
  const fillFromCredit = () => onChange({
    cash: d.cash,
    spendingLevel: d.spendingLevel,
    assets: value.assets || `约 $${d.assets.toLocaleString()}`,
  })
  return (
    <div className="space-y-3">
      <div className="flex items-center justify-between p-2 rounded" style={{ background: 'var(--color-bg-tertiary)' }}>
        <div className="text-sm">
          <span style={labelStyle}>信用评级</span>{' '}
          <span className="font-mono font-bold">{creditRating}%</span>{' '}
          <span className="text-xs" style={labelStyle}>（{d.tier}，在「技能」页调整）</span>
        </div>
        <button onClick={fillFromCredit} className="btn-secondary text-xs">按信用评级换算</button>
      </div>
      <div className="grid grid-cols-2 gap-3">
        <label className="text-sm">
          <span className="block mb-1" style={labelStyle}>现金（$）</span>
          <input type="number" value={value.cash} onChange={(e) => onChange({ ...value, cash: Number(e.target.value) || 0 })} className={inputCls + ' font-mono'} style={inputStyle} />
        </label>
        <label className="text-sm">
          <span className="block mb-1" style={labelStyle}>消费水平（$）</span>
          <input type="number" value={value.spendingLevel} onChange={(e) => onChange({ ...value, spendingLevel: Number(e.target.value) || 0 })} className={inputCls + ' font-mono'} style={inputStyle} />
        </label>
      </div>
      <label className="text-sm block">
        <span className="block mb-1" style={labelStyle}>资产情况</span>
        <textarea value={value.assets} onChange={(e) => onChange({ ...value, assets: e.target.value })} rows={2} placeholder="如：一栋郊区住宅、一辆轿车、少量股票" className={inputCls + ' resize-y'} style={inputStyle} />
      </label>
    </div>
  )
}

// 通用字符串清单编辑器
function StringList({ label, placeholder, items, onChange }: {
  label: string; placeholder: string; items: string[]; onChange: (v: string[]) => void
}) {
  return (
    <div>
      <h4 className="text-sm font-semibold mb-1" style={{ color: 'var(--color-text-accent)' }}>{label}</h4>
      <div className="space-y-1.5">
        {items.map((it, i) => (
          <div key={i} className="flex items-center gap-2">
            <input value={it} onChange={(e) => { const n = [...items]; n[i] = e.target.value; onChange(n) }} className={inputCls + ' flex-1'} style={inputStyle} />
            <button onClick={() => onChange(items.filter((_, j) => j !== i))} className={delBtn} style={delStyle}>删除</button>
          </div>
        ))}
        <button onClick={() => onChange([...items, ''])} className="btn-secondary text-xs">+ 添加{label}</button>
      </div>
      {items.length === 0 && <p className="text-xs mt-1" style={{ ...labelStyle, opacity: 0.7 }}>{placeholder}</p>}
    </div>
  )
}

// ---- 克苏鲁神话：法术 / 魔法物品与典籍 / 第三类接触 ----
export function MythosEditor({ value, onChange }: { value: Mythos; onChange: (v: Mythos) => void }) {
  return (
    <div className="space-y-4">
      <StringList label="法术" placeholder="尚未习得任何法术" items={value.spells} onChange={(spells) => onChange({ ...value, spells })} />
      <StringList label="魔法物品与典籍" placeholder="暂无魔法物品或典籍" items={value.tomes} onChange={(tomes) => onChange({ ...value, tomes })} />
      <StringList label="第三类接触" placeholder="暂无与神话造物的接触记录" items={value.encounters} onChange={(encounters) => onChange({ ...value, encounters })} />
    </div>
  )
}

// ---- 人际关系：角色 + 关系 ----
export function RelationsEditor({ value, onChange }: { value: Relation[]; onChange: (v: Relation[]) => void }) {
  return (
    <div className="space-y-2">
      {value.map((r, i) => (
        <div key={i} className="flex items-center gap-2">
          <input value={r.name} onChange={(e) => { const n = [...value]; n[i] = { ...r, name: e.target.value }; onChange(n) }} placeholder="角色" className={inputCls + ' flex-1'} style={inputStyle} />
          <input value={r.relation} onChange={(e) => { const n = [...value]; n[i] = { ...r, relation: e.target.value }; onChange(n) }} placeholder="关系（如：挚友 / 宿敌 / 导师）" className={inputCls + ' flex-1'} style={inputStyle} />
          <button onClick={() => onChange(value.filter((_, j) => j !== i))} className={delBtn} style={delStyle}>删除</button>
        </div>
      ))}
      <button onClick={() => onChange([...value, { name: '', relation: '' }])} className="btn-secondary text-xs">+ 添加关系</button>
      {value.length === 0 && <p className="text-xs" style={{ ...labelStyle, opacity: 0.7 }}>暂无人际关系</p>}
    </div>
  )
}

// ---- 模组经历：模组 + 具体经历 ----
export function ModuleHistoryEditor({ value, onChange }: { value: ModuleExperience[]; onChange: (v: ModuleExperience[]) => void }) {
  return (
    <div className="space-y-2">
      {value.map((m, i) => (
        <div key={i} className="p-2 rounded space-y-1.5" style={{ background: 'var(--color-bg-tertiary)' }}>
          <div className="flex items-center gap-2">
            <input value={m.module} onChange={(e) => { const n = [...value]; n[i] = { ...m, module: e.target.value }; onChange(n) }} placeholder="模组名称" className={inputCls + ' flex-1'} style={inputStyle} />
            <button onClick={() => onChange(value.filter((_, j) => j !== i))} className={delBtn} style={delStyle}>删除</button>
          </div>
          <textarea value={m.experience} onChange={(e) => { const n = [...value]; n[i] = { ...m, experience: e.target.value }; onChange(n) }} rows={2} placeholder="具体经历（遭遇、结局、获得/失去……）" className={inputCls + ' resize-y'} style={inputStyle} />
        </div>
      ))}
      <button onClick={() => onChange([...value, { module: '', experience: '' }])} className="btn-secondary text-xs">+ 添加模组经历</button>
      {value.length === 0 && <p className="text-xs" style={{ ...labelStyle, opacity: 0.7 }}>暂无模组经历</p>}
    </div>
  )
}

// 从 system_data 读取（带默认值与历史兼容）
export function readMythos(sd: Record<string, unknown>): Mythos {
  const m = (sd.mythos || {}) as Partial<Mythos>
  return { spells: m.spells || [], tomes: m.tomes || [], encounters: m.encounters || [] }
}
export function readRelations(sd: Record<string, unknown>): Relation[] {
  return Array.isArray(sd.relations) ? (sd.relations as Relation[]) : []
}
export function readModuleHistory(sd: Record<string, unknown>): ModuleExperience[] {
  return Array.isArray(sd.moduleHistory) ? (sd.moduleHistory as ModuleExperience[]) : []
}
export function readAssets(sd: Record<string, unknown>): AssetsInfo {
  return {
    cash: (sd.cash as number) ?? 0,
    spendingLevel: (sd.spendingLevel as number) ?? 0,
    assets: (sd.assets as string) ?? '',
  }
}
