import { useEffect, useState, useCallback } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import { toast } from 'sonner'
import { api } from '../api/client'
import { Select, SelectTrigger, SelectValue, SelectContent, SelectItem } from '@/components/ui/select'
import { GiReturnArrow, GiScrollUnfurled, GiPadlock } from 'react-icons/gi'
import { Plus, Trash2, Pencil, Save, X, Eye, Network, FileText } from 'lucide-react'
import { ModuleGraph } from '../components/module/ModuleGraph'

interface Scene { id: string; name?: string; title?: string; description?: string; connections?: string[] }
interface NPC { id: string; name?: string; description?: string; personality?: string; secrets?: string[]; initial_location?: string; skills?: Record<string, number> }
interface Clue { id: string; name?: string; description?: string; location?: string; trigger_condition?: string }
interface ModuleData {
  id?: string
  title: string
  rule_system: string
  description: string
  world_setting: Record<string, unknown>
  scenes: Scene[]
  npcs: NPC[]
  clues: Clue[]
}

const BLANK: ModuleData = {
  title: '', rule_system: 'coc', description: '',
  world_setting: { era: '', location: '', tone: '', player_count: '', difficulty: '', tags: [], player_brief: '' },
  scenes: [], npcs: [], clues: [],
}

const WS_FIELDS: { key: string; label: string }[] = [
  { key: 'era', label: '年代' },
  { key: 'location', label: '地点' },
  { key: 'tone', label: '基调' },
  { key: 'player_count', label: '人数' },
  { key: 'difficulty', label: '难度' },
]

let _idc = 0
const genId = (p: string) => `${p}_${Date.now().toString(36)}_${_idc++}`
const sceneName = (s: Scene) => s.name || s.title || '(未命名场景)'
const wsStr = (ws: Record<string, unknown>, k: string) => (ws[k] == null ? '' : String(ws[k]))

export function ModuleDetailPage() {
  const { id } = useParams()
  const navigate = useNavigate()
  const isNew = !id
  const [data, setData] = useState<ModuleData>(BLANK)
  const [edit, setEdit] = useState(isNew)
  const [graph, setGraph] = useState(false)
  const [loading, setLoading] = useState(!isNew)
  const [saving, setSaving] = useState(false)

  useEffect(() => {
    if (isNew) return
    api.get<ModuleData>(`/modules/${id}`)
      .then((m) => setData({ ...BLANK, ...m, world_setting: { ...BLANK.world_setting, ...(m.world_setting || {}) } }))
      .catch(() => { toast.error('模组加载失败'); navigate('/modules') })
      .finally(() => setLoading(false))
  }, [id, isNew, navigate])

  const updateWS = (k: string, v: unknown) => setData((d) => ({ ...d, world_setting: { ...d.world_setting, [k]: v } }))
  const upd = useCallback(<K extends 'scenes' | 'npcs' | 'clues'>(key: K, i: number, patch: Partial<ModuleData[K][number]>) =>
    setData((d) => ({ ...d, [key]: (d[key] as Record<string, unknown>[]).map((it, j) => (j === i ? { ...it, ...patch } : it)) })), [])
  const removeAt = (key: 'scenes' | 'npcs' | 'clues', i: number) =>
    setData((d) => ({ ...d, [key]: (d[key] as unknown[]).filter((_, j) => j !== i) }))

  const save = async () => {
    if (!data.title.trim()) { toast.error('请填写模组标题'); return }
    setSaving(true)
    try {
      const tags = data.world_setting.tags
      const payload = {
        title: data.title.trim(),
        rule_system: data.rule_system,
        description: data.description,
        world_setting: {
          ...data.world_setting,
          tags: Array.isArray(tags) ? tags : String(tags || '').split(/[,，]/).map((s) => s.trim()).filter(Boolean),
        },
        scenes: data.scenes,
        npcs: data.npcs.map((n) => ({ ...n, secrets: (n.secrets || []).filter((s) => s.trim()) })),
        clues: data.clues,
      }
      const saved = isNew
        ? await api.post<ModuleData>('/modules', payload)
        : await api.put<ModuleData>(`/modules/${id}`, payload)
      toast.success(isNew ? '模组已创建' : '模组已保存')
      if (isNew) navigate(`/modules/${saved.id}`, { replace: true })
      else { setData({ ...BLANK, ...saved, world_setting: { ...BLANK.world_setting, ...(saved.world_setting || {}) } }); setEdit(false) }
    } catch (e) {
      toast.error(`保存失败：${e instanceof Error ? e.message : '未知错误'}`)
    } finally { setSaving(false) }
  }

  if (loading) return <p className="p-4" style={{ color: 'var(--color-text-secondary)' }}>加载中…</p>

  const tagsText = Array.isArray(data.world_setting.tags) ? (data.world_setting.tags as string[]).join('、') : wsStr(data.world_setting, 'tags')

  return (
    <div className="max-w-3xl">
      <div className="flex items-center gap-3 mb-4">
        <button onClick={() => navigate('/modules')} className="btn-secondary flex items-center gap-1 !px-2 !py-1 text-sm">
          <GiReturnArrow /> 返回
        </button>
        <h2 className="page-title !mb-0 flex items-center gap-2"><GiScrollUnfurled />{isNew ? '新建模组' : edit ? '编辑模组' : '查看模组'}</h2>
        <div className="ml-auto flex gap-2 items-center">
          {!edit && !isNew && (
            <div className="flex rounded overflow-hidden text-sm" style={{ border: '1px solid var(--color-border)' }}>
              <button onClick={() => setGraph(false)} className="flex items-center gap-1 px-2 py-1" style={!graph ? { background: 'var(--color-accent)', color: '#fff' } : { color: 'var(--color-text-secondary)' }}><FileText size={14} /> 详情</button>
              <button onClick={() => setGraph(true)} className="flex items-center gap-1 px-2 py-1" style={graph ? { background: 'var(--color-accent)', color: '#fff' } : { color: 'var(--color-text-secondary)' }}><Network size={14} /> 关系图</button>
            </div>
          )}
          {!isNew && !edit && !graph && (
            <button onClick={() => setEdit(true)} className="btn-secondary flex items-center gap-1 text-sm"><Pencil size={14} /> 编辑</button>
          )}
          {edit && (
            <>
              {!isNew && <button onClick={() => setEdit(false)} className="btn-secondary flex items-center gap-1 text-sm"><X size={14} /> 取消</button>}
              <button onClick={save} disabled={saving} className="btn-primary flex items-center gap-1 text-sm"><Save size={14} /> {saving ? '保存中…' : '保存'}</button>
            </>
          )}
        </div>
      </div>

      {!edit && (
        <div className="card mb-4 flex items-center gap-2 text-sm" style={{ borderColor: 'var(--color-danger)', color: 'var(--color-danger)' }}>
          <Eye size={15} /> 剧透警告：{graph ? '关系图含线索归属等剧情结构' : '以下含 NPC 秘密、线索与真相'}。若你打算亲自游玩本模组，请勿继续阅读。
        </div>
      )}

      {graph && !edit ? (
        <ModuleGraph scenes={data.scenes} npcs={data.npcs} clues={data.clues} />
      ) : (
      <>
      {/* 基本信息 */}
      <Section title="基本信息">
        <Row label="标题">{edit ? <TextInput value={data.title} onChange={(v) => setData((d) => ({ ...d, title: v }))} /> : <span className="font-semibold">{data.title}</span>}</Row>
        <Row label="规则">{edit ? (
          <Select value={data.rule_system} onValueChange={(v) => setData((d) => ({ ...d, rule_system: v }))}>
            <SelectTrigger className="w-28"><SelectValue /></SelectTrigger>
            <SelectContent><SelectItem value="coc">CoC</SelectItem><SelectItem value="dnd">DnD</SelectItem></SelectContent>
          </Select>
        ) : <span className="badge">{data.rule_system.toUpperCase()}</span>}</Row>
        <Row label="简介">{edit ? <TextInput value={data.description} onChange={(v) => setData((d) => ({ ...d, description: v }))} multiline /> : <span>{data.description || '—'}</span>}</Row>
      </Section>

      {/* 世界设定 */}
      <Section title="世界设定">
        {WS_FIELDS.map(({ key, label }) => (
          <Row key={key} label={label}>{edit ? <TextInput value={wsStr(data.world_setting, key)} onChange={(v) => updateWS(key, v)} /> : <span>{wsStr(data.world_setting, key) || '—'}</span>}</Row>
        ))}
        <Row label="标签">{edit ? <TextInput value={tagsText} onChange={(v) => updateWS('tags', v.split(/[,，、]/).map((s) => s.trim()).filter(Boolean))} placeholder="逗号分隔" /> : <span>{tagsText || '—'}</span>}</Row>
        <Row label="开场钩子">{edit ? <TextInput value={wsStr(data.world_setting, 'player_brief')} onChange={(v) => updateWS('player_brief', v)} multiline placeholder="玩家开场就合法知道的动机/处境（不含待发现的线索/真相）" /> : <span className="whitespace-pre-wrap">{wsStr(data.world_setting, 'player_brief') || '—'}</span>}</Row>
      </Section>

      {/* 场景 */}
      <Section title={`场景（${data.scenes.length}）`} onAdd={edit ? () => setData((d) => ({ ...d, scenes: [...d.scenes, { id: genId('scene'), name: '', description: '', connections: [] }] })) : undefined}>
        {data.scenes.map((s, i) => (
          <ItemCard key={s.id || i} onRemove={edit ? () => removeAt('scenes', i) : undefined}>
            <Row label="名称">{edit ? <TextInput value={sceneName(s) === '(未命名场景)' ? '' : sceneName(s)} onChange={(v) => upd('scenes', i, { name: v })} /> : <span className="font-semibold">{sceneName(s)}</span>}</Row>
            <Row label="描述">{edit ? <TextInput value={s.description || ''} onChange={(v) => upd('scenes', i, { description: v })} multiline /> : <span className="whitespace-pre-wrap">{s.description || '—'}</span>}</Row>
            <Row label="连接">{edit ? <TextInput value={(s.connections || []).join(', ')} onChange={(v) => upd('scenes', i, { connections: v.split(/[,，]/).map((x) => x.trim()).filter(Boolean) })} placeholder="目标场景 id，逗号分隔" /> : <span className="text-xs" style={{ color: 'var(--color-text-secondary)' }}>{(s.connections || []).join('、') || '—'}　id: {s.id}</span>}</Row>
          </ItemCard>
        ))}
        {data.scenes.length === 0 && <Empty />}
      </Section>

      {/* NPC */}
      <Section title={`NPC（${data.npcs.length}）`} onAdd={edit ? () => setData((d) => ({ ...d, npcs: [...d.npcs, { id: genId('npc'), name: '', description: '', personality: '', secrets: [], initial_location: '', skills: {} }] })) : undefined}>
        {data.npcs.map((n, i) => (
          <ItemCard key={n.id || i} onRemove={edit ? () => removeAt('npcs', i) : undefined}>
            <Row label="姓名">{edit ? <TextInput value={n.name || ''} onChange={(v) => upd('npcs', i, { name: v })} /> : <span className="font-semibold">{n.name || '(未命名)'}</span>}</Row>
            <Row label="描述">{edit ? <TextInput value={n.description || ''} onChange={(v) => upd('npcs', i, { description: v })} multiline /> : <span className="whitespace-pre-wrap">{n.description || '—'}</span>}</Row>
            <Row label="性格">{edit ? <TextInput value={n.personality || ''} onChange={(v) => upd('npcs', i, { personality: v })} /> : <span>{n.personality || '—'}</span>}</Row>
            <Row label="初始位置">{edit ? <TextInput value={n.initial_location || ''} onChange={(v) => upd('npcs', i, { initial_location: v })} placeholder="场景 id" /> : <span className="text-xs">{n.initial_location || '—'}</span>}</Row>
            <Row label={<span style={{ color: 'var(--color-danger)' }} className="inline-flex items-center gap-0.5"><GiPadlock />秘密</span>}>{edit ? <TextInput value={(n.secrets || []).join('\n')} onChange={(v) => upd('npcs', i, { secrets: v.split('\n') })} multiline placeholder="每行一条，仅 KP 可见" /> : <span className="whitespace-pre-wrap" style={{ color: 'var(--color-danger)' }}>{(n.secrets || []).join('\n') || '—'}</span>}</Row>
            <Row label="技能">{edit ? <TextInput value={skillsToText(n.skills)} onChange={(v) => upd('npcs', i, { skills: parseSkills(v) })} multiline placeholder="每行 技能: 数值，如 侦查: 60" /> : <span className="text-xs">{skillsToText(n.skills).replace(/\n/g, '、') || '—'}</span>}</Row>
          </ItemCard>
        ))}
        {data.npcs.length === 0 && <Empty />}
      </Section>

      {/* 线索 */}
      <Section title={`线索（${data.clues.length}）`} onAdd={edit ? () => setData((d) => ({ ...d, clues: [...d.clues, { id: genId('clue'), name: '', description: '', location: '', trigger_condition: '' }] })) : undefined}>
        {data.clues.map((c, i) => (
          <ItemCard key={c.id || i} onRemove={edit ? () => removeAt('clues', i) : undefined}>
            <Row label="名称">{edit ? <TextInput value={c.name || ''} onChange={(v) => upd('clues', i, { name: v })} /> : <span className="font-semibold" style={{ color: 'var(--color-danger)' }}>{c.name || '(未命名)'}</span>}</Row>
            <Row label="内容">{edit ? <TextInput value={c.description || ''} onChange={(v) => upd('clues', i, { description: v })} multiline /> : <span className="whitespace-pre-wrap" style={{ color: 'var(--color-danger)' }}>{c.description || '—'}</span>}</Row>
            <Row label="位置">{edit ? <TextInput value={c.location || ''} onChange={(v) => upd('clues', i, { location: v })} placeholder="场景 id" /> : <span className="text-xs">{c.location || '—'}</span>}</Row>
            <Row label="发现条件">{edit ? <TextInput value={c.trigger_condition || ''} onChange={(v) => upd('clues', i, { trigger_condition: v })} multiline /> : <span className="whitespace-pre-wrap">{c.trigger_condition || '—'}</span>}</Row>
          </ItemCard>
        ))}
        {data.clues.length === 0 && <Empty />}
      </Section>
      </>
      )}
    </div>
  )
}

function skillsToText(skills?: Record<string, number>) {
  return Object.entries(skills || {}).map(([k, v]) => `${k}: ${v}`).join('\n')
}
function parseSkills(text: string): Record<string, number> {
  const out: Record<string, number> = {}
  for (const line of text.split('\n')) {
    const m = line.match(/^\s*(.+?)\s*[:：]\s*(\d+)\s*$/)
    if (m) out[m[1]] = Number(m[2])
  }
  return out
}

function Section({ title, onAdd, children }: { title: string; onAdd?: () => void; children: React.ReactNode }) {
  return (
    <div className="card mb-4">
      <div className="flex items-center justify-between mb-2">
        <h3 className="card-title !mb-0">{title}</h3>
        {onAdd && <button onClick={onAdd} className="btn-secondary flex items-center gap-1 text-xs !px-2 !py-1"><Plus size={13} /> 添加</button>}
      </div>
      {children}
    </div>
  )
}

function Row({ label, children }: { label: React.ReactNode; children: React.ReactNode }) {
  return (
    <div className="flex gap-3 py-1 text-sm items-start">
      <span className="flex-shrink-0 w-20 text-xs pt-1.5" style={{ color: 'var(--color-text-secondary)' }}>{label}</span>
      <div className="flex-1 min-w-0">{children}</div>
    </div>
  )
}

function ItemCard({ onRemove, children }: { onRemove?: () => void; children: React.ReactNode }) {
  return (
    <div className="rounded-md p-2 mb-2 relative" style={{ background: 'var(--color-bg-tertiary)', border: '1px solid var(--color-border)' }}>
      {onRemove && (
        <button onClick={onRemove} className="absolute top-1.5 right-1.5 p-1 rounded hover:bg-[var(--color-danger)] hover:text-white transition-colors" style={{ color: 'var(--color-danger)' }} title="删除">
          <Trash2 size={13} />
        </button>
      )}
      {children}
    </div>
  )
}

function TextInput({ value, onChange, multiline, placeholder }: { value: string; onChange: (v: string) => void; multiline?: boolean; placeholder?: string }) {
  const cls = 'w-full px-2 py-1 rounded text-sm'
  const style = { background: 'var(--color-bg-card)', border: '1px solid var(--color-border)', color: 'var(--color-text-primary)' }
  return multiline
    ? <textarea value={value} onChange={(e) => onChange(e.target.value)} placeholder={placeholder} rows={2} className={cls} style={style} />
    : <input value={value} onChange={(e) => onChange(e.target.value)} placeholder={placeholder} className={cls} style={style} />
}

function Empty() {
  return <p className="text-xs text-center py-3" style={{ color: 'var(--color-text-secondary)' }}>暂无</p>
}
