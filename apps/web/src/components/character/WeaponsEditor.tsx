import { useState } from 'react'
import { WeaponPickerDialog } from './WeaponPickerDialog'
import { type CharWeapon, type WeaponDef } from './useCocData'

const inputCls = 'w-full px-2 py-1 rounded text-sm'
const inputStyle = { background: 'var(--color-bg-tertiary)', border: '1px solid var(--color-border)' }

// 行内文本字段（成功率、贯穿单独处理）
const FIELDS: { key: keyof CharWeapon; label: string }[] = [
  { key: 'skill', label: '使用技能' },
  { key: 'dam', label: '伤害' },
  { key: 'range', label: '射程' },
  { key: 'round', label: '次数(a/b)' },
  { key: 'num', label: '装弹量' },
  { key: 'err', label: '故障' },
]

/**
 * 武器编辑器：规范九字段（名称/使用技能/成功率/伤害/射程/贯穿/次数a-b/装弹量/故障），
 * 支持从武器表挑选或手动添加。成功率默认取使用技能当前值，可手改。
 */
export function WeaponsEditor({
  weapons, onChange, skillValueOf,
}: {
  weapons: CharWeapon[]
  onChange: (next: CharWeapon[]) => void
  /** 按使用技能取当前成功率 */
  skillValueOf: (skill: string) => number
}) {
  const [pickerOpen, setPickerOpen] = useState(false)

  const pick = (w: WeaponDef) => {
    onChange([...weapons, {
      name: w.name, skill: w.skill, success: skillValueOf(w.skill),
      dam: w.dam, range: w.range, tho: !!w.tho, round: w.round, num: w.num, err: w.err,
    }])
  }

  return (
    <div>
      <div className="flex items-center justify-between mb-1.5">
        <h4 className="text-sm font-semibold" style={{ color: 'var(--color-text-accent)' }}>武器</h4>
        <div className="flex gap-2">
          <button onClick={() => setPickerOpen(true)} className="btn-secondary text-xs">从武器表添加</button>
          <button onClick={() => onChange([...weapons, { name: '' }])} className="btn-secondary text-xs">手动添加</button>
        </div>
      </div>
      <div className="space-y-2">
        {weapons.map((w, i) => {
          const upd = (patch: Partial<CharWeapon>) => {
            const next = [...weapons]; next[i] = { ...w, ...patch }; onChange(next)
          }
          return (
            <div key={i} className="p-2 rounded space-y-2" style={{ background: 'var(--color-bg-tertiary)' }}>
              <div className="flex items-center gap-2">
                <input
                  value={w.name}
                  onChange={(e) => upd({ name: e.target.value })}
                  placeholder="武器名称"
                  className={inputCls + ' flex-1'}
                  style={inputStyle}
                />
                <label className="flex items-center gap-1 text-xs whitespace-nowrap" style={{ color: 'var(--color-text-secondary)' }}>
                  <input type="checkbox" checked={!!w.tho} onChange={(e) => upd({ tho: e.target.checked })} /> 贯穿
                </label>
                <button
                  onClick={() => onChange(weapons.filter((_, j) => j !== i))}
                  className="text-xs px-2 py-1 rounded hover:bg-[var(--color-danger)] hover:text-white transition-colors"
                  style={{ color: 'var(--color-danger)', border: '1px solid var(--color-danger)' }}
                >删除</button>
              </div>
              <div className="grid grid-cols-3 gap-2">
                <label className="text-xs">
                  <span className="block mb-0.5" style={{ color: 'var(--color-text-secondary)' }}>
                    成功率
                    <button
                      onClick={() => upd({ success: skillValueOf(w.skill || '') })}
                      className="ml-1 underline"
                      style={{ color: 'var(--color-text-accent)' }}
                      title="按使用技能的当前值回填"
                    >取技能</button>
                  </span>
                  <input
                    type="number"
                    value={w.success ?? 0}
                    onChange={(e) => upd({ success: Number(e.target.value) || 0 })}
                    className={inputCls + ' font-mono'}
                    style={inputStyle}
                  />
                </label>
                {FIELDS.map((f) => (
                  <label key={f.key} className="text-xs">
                    <span className="block mb-0.5" style={{ color: 'var(--color-text-secondary)' }}>{f.label}</span>
                    <input
                      value={String(w[f.key] ?? '')}
                      onChange={(e) => upd({ [f.key]: e.target.value } as Partial<CharWeapon>)}
                      className={inputCls}
                      style={inputStyle}
                    />
                  </label>
                ))}
              </div>
            </div>
          )
        })}
        {weapons.length === 0 && (
          <p className="text-xs" style={{ color: 'var(--color-text-secondary)', opacity: 0.7 }}>暂无武器</p>
        )}
      </div>
      <WeaponPickerDialog open={pickerOpen} onOpenChange={setPickerOpen} onPick={pick} />
    </div>
  )
}
