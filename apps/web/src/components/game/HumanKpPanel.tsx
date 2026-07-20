import { useState } from 'react'
import { toast } from 'sonner'
import { api } from '@/api/client'
import { GiRollingDices, GiScrollUnfurled } from 'react-icons/gi'
import { Swords, WandSparkles } from 'lucide-react'

type KpAction =
  | 'narration'
  | 'dialogue'
  | 'dice_check'
  | 'opposed_check'
  | 'san_check'
  | 'scene_change'
  | 'set_flag'
  | 'clear_flag'
  | 'handout'
  | 'hp_change'
  | 'start_combat'

interface Props {
  sessionId: string
}

const ACTION_LABELS: Record<KpAction, string> = {
  narration: '发布叙事',
  dialogue: 'NPC 台词',
  dice_check: '发起检定',
  opposed_check: '对抗检定',
  san_check: '理智检定',
  scene_change: '切换场景',
  set_flag: '推进标志',
  clear_flag: '解除标志',
  handout: '发放手书',
  hp_change: '结算 HP',
  start_combat: '开始战斗',
}

export function HumanKpPanel({ sessionId }: Props) {
  const [action, setAction] = useState<KpAction>('narration')
  const [busy, setBusy] = useState(false)
  const [fields, setFields] = useState<Record<string, string>>({})

  const setField = (key: string, value: string) => {
    setFields((current) => ({ ...current, [key]: value }))
  }

  const submit = async () => {
    const payload = Object.fromEntries(
      Object.entries(fields).filter(([, value]) => value.trim()),
    )
    setBusy(true)
    try {
      await api.post(`/sessions/${sessionId}/kp/action`, { action, payload })
      setFields({})
      toast.success(`${ACTION_LABELS[action]}已发布`)
    } catch (error) {
      toast.error(error instanceof Error ? error.message : 'KP 动作执行失败')
    } finally {
      setBusy(false)
    }
  }

  const Input = ({ name, placeholder }: { name: string; placeholder: string }) => (
    <input
      value={fields[name] || ''}
      onChange={(event) => setField(name, event.target.value)}
      placeholder={placeholder}
      className="input min-w-0 flex-1 text-xs"
    />
  )

  return (
    <section
      className="mx-3 mb-2 rounded-md px-3 py-2"
      style={{ background: 'var(--color-bg-tertiary)', border: '1px solid var(--color-border-strong)' }}
    >
      <div className="mb-2 flex items-center gap-2">
        <WandSparkles size={14} style={{ color: 'var(--color-text-accent)' }} />
        <span className="text-xs font-semibold" style={{ color: 'var(--color-text-accent)' }}>真人 KP 工具桌</span>
        <select
          value={action}
          onChange={(event) => { setAction(event.target.value as KpAction); setFields({}) }}
          className="input ml-auto !w-auto text-xs"
          aria-label="KP 动作"
        >
          {Object.entries(ACTION_LABELS).map(([value, label]) => (
            <option key={value} value={value}>{label}</option>
          ))}
        </select>
      </div>

      <div className="flex flex-wrap gap-2">
        {action === 'narration' && (
          <textarea
            value={fields.content || ''}
            onChange={(event) => setField('content', event.target.value)}
            placeholder="输入要发布给全桌的旁白"
            className="input min-h-16 w-full resize-y text-xs"
          />
        )}
        {action === 'dialogue' && <><Input name="npc_id" placeholder="NPC 名称或 ID" /><Input name="content" placeholder="台词内容" /></>}
        {action === 'dice_check' && <>
          <Input name="skill" placeholder="技能，如 幸运、侦查" />
          <Input name="char" placeholder="角色，空=主角；在场=群检" />
          <select value={fields.difficulty || 'normal'} onChange={(event) => setField('difficulty', event.target.value)} className="input w-auto text-xs">
            <option value="normal">普通</option><option value="hard">困难</option><option value="extreme">极难</option>
          </select>
          <Input name="source" placeholder="目标/来源（可选）" />
        </>}
        {action === 'opposed_check' && <>
          <Input name="a" placeholder="甲方角色" /><Input name="a_skill" placeholder="甲方技能" />
          <Input name="b" placeholder="乙方角色/NPC" /><Input name="b_skill" placeholder="乙方技能" />
        </>}
        {action === 'san_check' && <>
          <Input name="chars" placeholder="目睹者，空=全队" /><Input name="source" placeholder="恐怖源" />
          <Input name="success_loss" placeholder="成功损失，如 0" /><Input name="failure_loss" placeholder="失败损失，如 1d6" />
        </>}
        {action === 'scene_change' && <Input name="scene_id" placeholder="场景 ID 或名称" />}
        {(action === 'set_flag' || action === 'clear_flag') && <Input name="flag" placeholder="剧情标志" />}
        {action === 'handout' && <><GiScrollUnfurled size={16} /><Input name="id" placeholder="手书 ID" /></>}
        {action === 'hp_change' && <><Input name="target" placeholder="角色名" /><Input name="delta" placeholder="变化值，如 -3 或 2" /><Input name="reason" placeholder="原因（可选）" /></>}
        {action === 'start_combat' && <><Swords size={16} /><Input name="enemies" placeholder="敌人名称，多个用逗号分隔" /><Input name="trigger" placeholder="开战原因（可选）" /></>}
        <button onClick={() => void submit()} disabled={busy} className="btn-primary inline-flex items-center gap-1 text-xs">
          {action === 'dice_check' ? <GiRollingDices size={13} /> : <WandSparkles size={13} />}
          {busy ? '处理中…' : ACTION_LABELS[action]}
        </button>
      </div>
    </section>
  )
}
