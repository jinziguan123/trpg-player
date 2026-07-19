import { useEffect, useState, useCallback } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import { toast } from 'sonner'
import { api } from '../api/client'
import { Select, SelectTrigger, SelectValue, SelectContent, SelectItem } from '@/components/ui/select'
import { GiReturnArrow, GiScrollUnfurled, GiPadlock } from 'react-icons/gi'
import { Plus, Trash2, Pencil, Save, X, Eye, Network, FileText, GitBranch } from 'lucide-react'
import { ModuleGraph } from '../components/module/ModuleGraph'
import { ModuleTimeline } from '../components/module/ModuleTimeline'
import { MODULE_DIFFICULTIES } from '../lib/module'

// 场景瓦片地图已下线；旧模组数据里的 scenes[].map / states[].map 字段容忍存在（保存时原样透传，不读取）
interface SceneState { when?: string[]; danger?: string; atmosphere?: string; description?: string }
interface NpcState { when?: string[]; personality?: string; initial_location?: string; alive?: boolean }
interface SceneEvent { trigger?: string; kind?: string; san_loss?: string; skill?: string; damage?: string; note?: string }
interface Scene { id: string; name?: string; title?: string; description?: string; danger?: string; atmosphere?: string; connections?: string[]; events?: SceneEvent[]; states?: SceneState[] }
interface NPC { id: string; name?: string; description?: string; personality?: string; background?: string; secrets?: string[]; initial_location?: string; skills?: Record<string, number>; attributes?: Record<string, number>; hp?: number; armor?: number; weapon?: string; goals?: string[]; states?: NpcState[] }
interface Clue { id: string; name?: string; description?: string; location?: string; trigger_condition?: string }
interface Trigger { id: string; when?: string; set_flags?: string[]; clear_flags?: string[]; description?: string }
interface ModuleData {
  id?: string
  title: string
  rule_system: string
  description: string
  world_setting: Record<string, unknown>
  scenes: Scene[]
  npcs: NPC[]
  clues: Clue[]
  triggers: Trigger[]
  truth: string
}

const BLANK: ModuleData = {
  title: '', rule_system: 'coc', description: '',
  world_setting: { era: '', location: '', tone: '', player_count: '', region: '', difficulty: '', tags: [], player_brief: '', intro: '' },
  scenes: [], npcs: [], clues: [], triggers: [], truth: '',
}

const EVENT_KINDS: { value: string; label: string }[] = [
  { value: 'san_check', label: '理智检定' },
  { value: 'dice_check', label: '技能检定' },
  { value: 'damage', label: '伤害' },
  { value: 'note', label: '提示' },
]
const eventKindLabel = (v?: string) => EVENT_KINDS.find((k) => k.value === v)?.label || '提示'
const eventValue = (e: SceneEvent) => e.kind === 'san_check' ? e.san_loss : e.kind === 'dice_check' ? e.skill : e.kind === 'damage' ? e.damage : ''

const COC_ATTRS = ['STR', 'CON', 'SIZ', 'DEX', 'APP', 'INT', 'POW', 'EDU', 'LUCK']
const csv = (a?: string[]) => (a || []).join(', ')
const parseCsv = (v: string) => v.split(/[,，、]/).map((s) => s.trim()).filter(Boolean)

const WS_FIELDS: { key: string; label: string }[] = [
  { key: 'era', label: '年代' },
  { key: 'region', label: '地区' },
  { key: 'location', label: '地点' },
  { key: 'tone', label: '基调' },
  { key: 'player_count', label: '人数' },
]

const DANGER_OPTS: { value: string; label: string; color: string }[] = [
  { value: 'calm', label: '平静', color: 'var(--color-text-secondary)' },
  { value: 'uneasy', label: '不安', color: '#cfa93f' },
  { value: 'dangerous', label: '危险', color: '#d1703c' },
  { value: 'deadly', label: '致命', color: 'var(--color-danger)' },
]
const dangerMeta = (v?: string) => DANGER_OPTS.find((o) => o.value === v)

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
  const [view, setView] = useState<'detail' | 'graph' | 'timeline'>('detail')
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
  const updScene = useCallback((i: number, patch: Partial<Scene>) =>
    setData((d) => ({ ...d, scenes: d.scenes.map((it, j) => (j === i ? { ...it, ...patch } : it)) })), [])
  const updNpc = useCallback((i: number, patch: Partial<NPC>) =>
    setData((d) => ({ ...d, npcs: d.npcs.map((it, j) => (j === i ? { ...it, ...patch } : it)) })), [])
  const updClue = useCallback((i: number, patch: Partial<Clue>) =>
    setData((d) => ({ ...d, clues: d.clues.map((it, j) => (j === i ? { ...it, ...patch } : it)) })), [])
  const removeAt = (key: 'scenes' | 'npcs' | 'clues', i: number) =>
    setData((d) => ({ ...d, [key]: (d[key] as unknown[]).filter((_, j) => j !== i) }))

  // 剧情变体（场景/NPC 的 states）增删改
  const addSceneState = (i: number) => updScene(i, { states: [...(data.scenes[i]?.states || []), { when: [] }] })
  const updSceneState = (i: number, j: number, patch: Partial<SceneState>) =>
    updScene(i, { states: (data.scenes[i]?.states || []).map((st, jj) => jj === j ? { ...st, ...patch } : st) })
  const rmSceneState = (i: number, j: number) =>
    updScene(i, { states: (data.scenes[i]?.states || []).filter((_, jj) => jj !== j) })
  const addNpcState = (i: number) => updNpc(i, { states: [...(data.npcs[i]?.states || []), { when: [] }] })
  const updNpcState = (i: number, j: number, patch: Partial<NpcState>) =>
    updNpc(i, { states: (data.npcs[i]?.states || []).map((st, jj) => jj === j ? { ...st, ...patch } : st) })
  const rmNpcState = (i: number, j: number) =>
    updNpc(i, { states: (data.npcs[i]?.states || []).filter((_, jj) => jj !== j) })

  // 触发器（模组级 triggers）增删改
  const addTrigger = () => setData((d) => ({ ...d, triggers: [...d.triggers, { id: genId('trig'), when: '', set_flags: [], clear_flags: [] }] }))
  const updTrigger = (i: number, patch: Partial<Trigger>) => setData((d) => ({ ...d, triggers: d.triggers.map((t, ii) => ii === i ? { ...t, ...patch } : t) }))
  const rmTrigger = (i: number) => setData((d) => ({ ...d, triggers: d.triggers.filter((_, ii) => ii !== i) }))

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
        npcs: data.npcs.map((n) => ({
          ...n,
          secrets: (n.secrets || []).filter((s) => s.trim()),
          goals: (n.goals || []).filter((g) => g.trim()),
        })),
        clues: data.clues,
        triggers: data.triggers,
        truth: data.truth,
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

  const graph = view === 'graph'
  const wide = graph && !edit
  const tabBtn = (v: 'detail' | 'graph' | 'timeline', icon: React.ReactNode, label: string) => (
    <button onClick={() => setView(v)} className="flex items-center gap-1 px-2 py-1" style={view === v ? { background: 'var(--color-accent)', color: 'var(--color-on-accent)' } : { color: 'var(--color-text-secondary)' }}>{icon} {label}</button>
  )
  return (
    <div className={wide ? 'max-w-6xl' : 'max-w-3xl'}>
      <div className="flex items-center gap-3 mb-4">
        <button onClick={() => navigate('/modules')} className="btn-secondary flex items-center gap-1 !px-2 !py-1 text-sm">
          <GiReturnArrow /> 返回
        </button>
        <h2 className="page-title !mb-0 flex items-center gap-2"><GiScrollUnfurled />{isNew ? '新建模组' : edit ? '编辑模组' : '查看模组'}</h2>
        <div className="ml-auto flex gap-2 items-center">
          {!edit && !isNew && (
            <div className="flex rounded overflow-hidden text-sm" style={{ border: '1px solid var(--color-border)' }}>
              {tabBtn('detail', <FileText size={14} />, '详情')}
              {tabBtn('graph', <Network size={14} />, '关系图')}
              {tabBtn('timeline', <GitBranch size={14} />, '时间线')}
            </div>
          )}
          {!isNew && !edit && view === 'detail' && (
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
          <Eye size={15} /> 剧透警告：{view === 'graph' ? '关系图含线索归属等剧情结构' : view === 'timeline' ? '时间线含剧情推进与 NPC 生死等结构' : '以下含 NPC 秘密、线索与真相'}。若你打算亲自游玩本模组，请勿继续阅读。
        </div>
      )}

      {view === 'graph' && !edit ? (
        <ModuleGraph scenes={data.scenes} npcs={data.npcs} clues={data.clues} />
      ) : view === 'timeline' && !edit ? (
        <ModuleTimeline scenes={data.scenes} npcs={data.npcs} triggers={data.triggers} />
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
        <Row label="难度">
          {edit ? (
            <Select value={wsStr(data.world_setting, 'difficulty') || '__none'} onValueChange={(v) => updateWS('difficulty', v === '__none' ? '' : v)}>
              <SelectTrigger className="w-40"><SelectValue placeholder="未设定" /></SelectTrigger>
              <SelectContent>
                <SelectItem value="__none">未设定</SelectItem>
                {MODULE_DIFFICULTIES.map((d) => <SelectItem key={d} value={d}>{d}</SelectItem>)}
              </SelectContent>
            </Select>
          ) : <span>{wsStr(data.world_setting, 'difficulty') || '—'}</span>}
        </Row>
        <Row label="标签">{edit ? <TextInput value={tagsText} onChange={(v) => updateWS('tags', v.split(/[,，、]/).map((s) => s.trim()).filter(Boolean))} placeholder="逗号分隔" /> : <span>{tagsText || '—'}</span>}</Row>
        <Row label="世界观导入">{edit ? <TextInput value={wsStr(data.world_setting, 'intro')} onChange={(v) => updateWS('intro', v)} multiline placeholder="开场朗读用的世界观/基调铺陈（年代、风物、是哪一类故事），无剧透，区别于开场钩子" /> : <span className="whitespace-pre-wrap">{wsStr(data.world_setting, 'intro') || '—'}</span>}</Row>
        <Row label="开场钩子">{edit ? <TextInput value={wsStr(data.world_setting, 'player_brief')} onChange={(v) => updateWS('player_brief', v)} multiline placeholder="玩家开场就合法知道的动机/处境（不含待发现的线索/真相）" /> : <span className="whitespace-pre-wrap">{wsStr(data.world_setting, 'player_brief') || '—'}</span>}</Row>
      </Section>

      {/* 幕后真相（守秘人资讯，KP 专属） */}
      <Section title="幕后真相（守秘人专属）">
        {edit ? (
          <TextInput value={data.truth} onChange={(v) => setData((d) => ({ ...d, truth: v }))} multiline
            placeholder="整个事件的来龙去脉：真凶、动机、时间线——KP 专属参考，玩家永不可见" />
        ) : (
          <p className="whitespace-pre-wrap text-sm" style={{ color: 'var(--color-danger)' }}>
            {data.truth || '—（旧模组无此段，重新导入可解析出）'}
          </p>
        )}
      </Section>

      {/* 场景 */}
      <Section title={`场景（${data.scenes.length}）`} onAdd={edit ? () => setData((d) => ({ ...d, scenes: [...d.scenes, { id: genId('scene'), name: '', description: '', danger: 'calm', atmosphere: '', connections: [] }] })) : undefined}>
        {data.scenes.map((s, i) => (
          <ItemCard key={s.id || i} onRemove={edit ? () => removeAt('scenes', i) : undefined}>
            <Row label="名称">{edit ? <TextInput value={sceneName(s) === '(未命名场景)' ? '' : sceneName(s)} onChange={(v) => updScene(i, { name: v })} /> : <span className="font-semibold">{sceneName(s)}</span>}</Row>
            <Row label="描述">{edit ? <TextInput value={s.description || ''} onChange={(v) => updScene(i, { description: v })} multiline /> : <span className="whitespace-pre-wrap">{s.description || '—'}</span>}</Row>
            <Row label="危险度">{edit ? (
              <Select value={s.danger || 'calm'} onValueChange={(v) => updScene(i, { danger: v })}>
                <SelectTrigger className="w-28"><SelectValue /></SelectTrigger>
                <SelectContent>{DANGER_OPTS.map((o) => <SelectItem key={o.value} value={o.value}>{o.label}</SelectItem>)}</SelectContent>
              </Select>
            ) : <span className="badge" style={{ color: dangerMeta(s.danger)?.color, borderColor: dangerMeta(s.danger)?.color }}>{dangerMeta(s.danger)?.label || '平静'}</span>}</Row>
            <Row label="氛围">{edit ? <TextInput value={s.atmosphere || ''} onChange={(v) => updScene(i, { atmosphere: v })} placeholder="感官+情绪基调，如『腐臭、低压、随时塌方』" /> : <span style={{ color: 'var(--color-text-secondary)' }}>{s.atmosphere || '—'}</span>}</Row>
            <Row label="连接">{edit ? <TextInput value={(s.connections || []).join(', ')} onChange={(v) => updScene(i, { connections: v.split(/[,，]/).map((x) => x.trim()).filter(Boolean) })} placeholder="目标场景 id，逗号分隔" /> : <span className="text-xs" style={{ color: 'var(--color-text-secondary)' }}>{(s.connections || []).join('、') || '—'}　id: {s.id}</span>}</Row>
            <EventList events={s.events} edit={edit}
              onAdd={() => updScene(i, { events: [...(s.events || []), { trigger: '', kind: 'san_check', san_loss: '' }] })}
              onRemove={(j) => updScene(i, { events: (s.events || []).filter((_, jj) => jj !== j) })}
              onUpd={(j, patch) => updScene(i, { events: (s.events || []).map((e, jj) => (jj === j ? { ...e, ...patch } : e)) })} />
            <VariantList states={s.states} edit={edit} onAdd={() => addSceneState(i)} onRemove={(j) => rmSceneState(i, j)} onWhen={(j, f) => updSceneState(i, j, { when: f })}
              renderFields={(st, j) => (
                <>
                  <Row label="危险度">{edit ? (
                    <Select value={st.danger || 'calm'} onValueChange={(v) => updSceneState(i, j, { danger: v })}>
                      <SelectTrigger className="w-28"><SelectValue /></SelectTrigger>
                      <SelectContent>{DANGER_OPTS.map((o) => <SelectItem key={o.value} value={o.value}>{o.label}</SelectItem>)}</SelectContent>
                    </Select>
                  ) : <span className="badge" style={{ color: dangerMeta(st.danger)?.color, borderColor: dangerMeta(st.danger)?.color }}>{dangerMeta(st.danger)?.label || '—'}</span>}</Row>
                  <Row label="氛围">{edit ? <TextInput value={st.atmosphere || ''} onChange={(v) => updSceneState(i, j, { atmosphere: v })} placeholder="切换后的氛围" /> : <span className="text-xs">{st.atmosphere || '—'}</span>}</Row>
                  <Row label="描述">{edit ? <TextInput value={st.description || ''} onChange={(v) => updSceneState(i, j, { description: v })} multiline placeholder="（可选）切换后的场景描述" /> : <span className="whitespace-pre-wrap text-xs">{st.description || '—'}</span>}</Row>
                </>
              )} />
          </ItemCard>
        ))}
        {data.scenes.length === 0 && <Empty />}
      </Section>

      {/* NPC */}
      <Section title={`NPC（${data.npcs.length}）`} onAdd={edit ? () => setData((d) => ({ ...d, npcs: [...d.npcs, { id: genId('npc'), name: '', description: '', personality: '', secrets: [], initial_location: '', skills: {} }] })) : undefined}>
        {data.npcs.map((n, i) => (
          <ItemCard key={n.id || i} onRemove={edit ? () => removeAt('npcs', i) : undefined}>
            <Row label="姓名">{edit ? <TextInput value={n.name || ''} onChange={(v) => updNpc(i, { name: v })} /> : <span className="font-semibold">{n.name || '(未命名)'}</span>}</Row>
            <Row label="描述">{edit ? <TextInput value={n.description || ''} onChange={(v) => updNpc(i, { description: v })} multiline /> : <span className="whitespace-pre-wrap">{n.description || '—'}</span>}</Row>
            <Row label="性格">{edit ? <TextInput value={n.personality || ''} onChange={(v) => updNpc(i, { personality: v })} /> : <span>{n.personality || '—'}</span>}</Row>
            <Row label="生平">{edit ? <TextInput value={n.background || ''} onChange={(v) => updNpc(i, { background: v })} multiline placeholder="来历渊源（与秘密区分）" /> : <span className="whitespace-pre-wrap">{n.background || '—'}</span>}</Row>
            <Row label="初始位置">{edit ? <TextInput value={n.initial_location || ''} onChange={(v) => updNpc(i, { initial_location: v })} placeholder="场景 id" /> : <span className="text-xs">{n.initial_location || '—'}</span>}</Row>
            <Row label="属性">{<AttrGrid attrs={n.attributes} edit={edit} onChange={(a) => updNpc(i, { attributes: a })} />}</Row>
            <Row label={<span style={{ color: 'var(--color-danger)' }} className="inline-flex items-center gap-0.5"><GiPadlock />秘密</span>}>{edit ? <TextInput value={(n.secrets || []).join('\n')} onChange={(v) => updNpc(i, { secrets: v.split('\n') })} multiline placeholder="每行一条，仅 KP 可见" /> : <span className="whitespace-pre-wrap" style={{ color: 'var(--color-danger)' }}>{(n.secrets || []).join('\n') || '—'}</span>}</Row>
            <Row label="技能">{edit ? <TextInput value={skillsToText(n.skills)} onChange={(v) => updNpc(i, { skills: parseSkills(v) })} multiline placeholder="每行 技能: 数值，如 侦查: 60" /> : <span className="text-xs">{skillsToText(n.skills).replace(/\n/g, '、') || '—'}</span>}</Row>
            <Row label="战斗">{edit ? (
              <div className="flex items-center gap-3 text-xs flex-wrap">
                {([['hp', 'HP'], ['armor', '护甲']] as const).map(([k, lbl]) => (
                  <label key={k} className="flex items-center gap-1">
                    <span style={{ color: 'var(--color-text-secondary)' }}>{lbl}</span>
                    <input type="number" value={n[k] ?? ''} className="w-14 px-1 py-0.5 rounded"
                      style={{ background: 'var(--color-bg-card)', border: '1px solid var(--color-border)', color: 'var(--color-text-primary)' }}
                      onChange={(e) => updNpc(i, { [k]: e.target.value === '' ? undefined : Number(e.target.value) })} />
                  </label>
                ))}
                <label className="flex items-center gap-1 flex-1 min-w-32">
                  <span style={{ color: 'var(--color-text-secondary)' }}>武器</span>
                  <input value={n.weapon || ''} placeholder="如 匕首、撕咬" className="w-full px-1 py-0.5 rounded"
                    style={{ background: 'var(--color-bg-card)', border: '1px solid var(--color-border)', color: 'var(--color-text-primary)' }}
                    onChange={(e) => updNpc(i, { weapon: e.target.value })} />
                </label>
              </div>
            ) : (
              <span className="text-xs">{[
                n.hp != null ? `HP ${n.hp}` : '',
                n.armor != null ? `护甲 ${n.armor}` : '',
                n.weapon ? `武器 ${n.weapon}` : '',
              ].filter(Boolean).join('、') || '—'}</span>
            )}</Row>
            <Row label="目标">{edit ? <TextInput value={(n.goals || []).join('\n')} onChange={(v) => updNpc(i, { goals: v.split('\n') })} multiline placeholder="每行一条：该 NPC 想达成什么（幕后推演据此让其行动）" /> : <span className="whitespace-pre-wrap text-xs">{(n.goals || []).join('\n') || '—'}</span>}</Row>
            <VariantList states={n.states} edit={edit} onAdd={() => addNpcState(i)} onRemove={(j) => rmNpcState(i, j)} onWhen={(j, f) => updNpcState(i, j, { when: f })}
              renderFields={(st, j) => (
                <>
                  <Row label="性格">{edit ? <TextInput value={st.personality || ''} onChange={(v) => updNpcState(i, j, { personality: v })} placeholder="切换后的态度" /> : <span className="text-xs">{st.personality || '—'}</span>}</Row>
                  <Row label="位置">{edit ? <TextInput value={st.initial_location || ''} onChange={(v) => updNpcState(i, j, { initial_location: v })} placeholder="切换后的场景 id" /> : <span className="text-xs">{st.initial_location || '—'}</span>}</Row>
                  <Row label="存活">{edit ? (
                    <Select value={st.alive === false ? 'false' : 'true'} onValueChange={(v) => updNpcState(i, j, { alive: v === 'true' })}>
                      <SelectTrigger className="w-28"><SelectValue /></SelectTrigger>
                      <SelectContent><SelectItem value="true">存活</SelectItem><SelectItem value="false">死亡</SelectItem></SelectContent>
                    </Select>
                  ) : <span className="text-xs" style={st.alive === false ? { color: 'var(--color-danger)' } : undefined}>{st.alive === false ? '死亡' : '存活'}</span>}</Row>
                </>
              )} />
          </ItemCard>
        ))}
        {data.npcs.length === 0 && <Empty />}
      </Section>

      {/* 线索 */}
      <Section title={`线索（${data.clues.length}）`} onAdd={edit ? () => setData((d) => ({ ...d, clues: [...d.clues, { id: genId('clue'), name: '', description: '', location: '', trigger_condition: '' }] })) : undefined}>
        {data.clues.map((c, i) => (
          <ItemCard key={c.id || i} onRemove={edit ? () => removeAt('clues', i) : undefined}>
            <Row label="名称">{edit ? <TextInput value={c.name || ''} onChange={(v) => updClue(i, { name: v })} /> : <span className="font-semibold" style={{ color: 'var(--color-danger)' }}>{c.name || '(未命名)'}</span>}</Row>
            <Row label="内容">{edit ? <TextInput value={c.description || ''} onChange={(v) => updClue(i, { description: v })} multiline /> : <span className="whitespace-pre-wrap" style={{ color: 'var(--color-danger)' }}>{c.description || '—'}</span>}</Row>
            <Row label="位置">{edit ? <TextInput value={c.location || ''} onChange={(v) => updClue(i, { location: v })} placeholder="场景 id" /> : <span className="text-xs">{c.location || '—'}</span>}</Row>
            <Row label="发现条件">{edit ? <TextInput value={c.trigger_condition || ''} onChange={(v) => updClue(i, { trigger_condition: v })} multiline /> : <span className="whitespace-pre-wrap">{c.trigger_condition || '—'}</span>}</Row>
          </ItemCard>
        ))}
        {data.clues.length === 0 && <Empty />}
      </Section>

      {/* 触发器（剧情推进：何时置/清哪个标志） */}
      <Section title={`触发器（${data.triggers.length}）`} onAdd={edit ? addTrigger : undefined}>
        {edit && (
          <p className="text-xs mb-2" style={{ color: 'var(--color-text-secondary)', opacity: 0.8 }}>
            定义剧情推进：当某事发生时，置/清对应的剧情标志；标志名需与场景/NPC 变体的「条件」一致呼应。
          </p>
        )}
        {data.triggers.map((t, i) => (
          <ItemCard key={t.id || i} onRemove={edit ? () => rmTrigger(i) : undefined}>
            <Row label="触发条件">{edit ? <TextInput value={t.when || ''} onChange={(v) => updTrigger(i, { when: v })} placeholder="自然语言，如『玩家弄塌地下室水管』" /> : <span>{t.when || '—'}</span>}</Row>
            <Row label="置标志">{edit ? <TextInput value={csv(t.set_flags)} onChange={(v) => updTrigger(i, { set_flags: parseCsv(v) })} placeholder="标志名，逗号分隔" /> : <span className="text-xs" style={{ color: 'var(--color-text-accent)' }}>{csv(t.set_flags) || '—'}</span>}</Row>
            <Row label="清标志">{edit ? <TextInput value={csv(t.clear_flags)} onChange={(v) => updTrigger(i, { clear_flags: parseCsv(v) })} placeholder="（可选）状态消退时清除的标志" /> : <span className="text-xs" style={{ color: 'var(--color-text-secondary)' }}>{csv(t.clear_flags) || '—'}</span>}</Row>
          </ItemCard>
        ))}
        {data.triggers.length === 0 && <Empty />}
      </Section>
      </>
      )}
    </div>
  )
}

/** 场景机制点列表（events）：模组明文规定的「情景 → 理智检定/技能检定/伤害」，数值照抄原文。 */
function EventList({ events, edit, onAdd, onRemove, onUpd }: {
  events?: SceneEvent[]
  edit: boolean
  onAdd: () => void
  onRemove: (j: number) => void
  onUpd: (j: number, patch: Partial<SceneEvent>) => void
}) {
  const list = events || []
  if (!edit && list.length === 0) return null
  const inputStyle = { background: 'var(--color-bg-card)', border: '1px solid var(--color-border)', color: 'var(--color-text-primary)' }
  return (
    <div className="mt-1 rounded" style={{ border: '1px dashed var(--color-border)', padding: 8 }}>
      <div className="flex items-center justify-between mb-1">
        <span className="text-xs font-semibold" style={{ color: 'var(--color-text-secondary)' }}>机制点（进入/行动触发的检定与伤害，数值照抄模组）</span>
        {edit && <button onClick={onAdd} className="btn-secondary text-xs !px-1.5 !py-0.5 flex items-center gap-1"><Plus size={11} />机制点</button>}
      </div>
      {list.length === 0 && <p className="text-xs" style={{ color: 'var(--color-text-secondary)', opacity: 0.6 }}>无</p>}
      {list.map((e, j) => edit ? (
        <div key={j} className="rounded p-1.5 mb-1 relative" style={{ background: 'var(--color-bg-card)', border: '1px solid var(--color-border)' }}>
          <button onClick={() => onRemove(j)} className="absolute top-1 right-1 p-0.5" style={{ color: 'var(--color-danger)' }} title="删除机制点"><Trash2 size={11} /></button>
          <Row label="情景">{<TextInput value={e.trigger || ''} onChange={(v) => onUpd(j, { trigger: v })} placeholder="如 进入车厢即目睹尸体 / 翻动行李箱" />}</Row>
          <Row label="类型">
            <Select value={e.kind || 'note'} onValueChange={(v) => onUpd(j, { kind: v })}>
              <SelectTrigger className="w-32"><SelectValue /></SelectTrigger>
              <SelectContent>{EVENT_KINDS.map((k) => <SelectItem key={k.value} value={k.value}>{k.label}</SelectItem>)}</SelectContent>
            </Select>
          </Row>
          {e.kind === 'san_check' && <Row label="SAN 损失"><input value={e.san_loss || ''} placeholder="如 0/1d3" className="w-28 px-1 py-0.5 rounded text-sm" style={inputStyle} onChange={(ev) => onUpd(j, { san_loss: ev.target.value })} /></Row>}
          {e.kind === 'dice_check' && <Row label="技能"><input value={e.skill || ''} placeholder="如 侦查" className="w-28 px-1 py-0.5 rounded text-sm" style={inputStyle} onChange={(ev) => onUpd(j, { skill: ev.target.value })} /></Row>}
          {e.kind === 'damage' && <Row label="伤害"><input value={e.damage || ''} placeholder="如 1d6" className="w-28 px-1 py-0.5 rounded text-sm" style={inputStyle} onChange={(ev) => onUpd(j, { damage: ev.target.value })} /></Row>}
          <Row label="备注">{<TextInput value={e.note || ''} onChange={(v) => onUpd(j, { note: v })} placeholder="（可选）补充说明或后果" />}</Row>
        </div>
      ) : (
        <div key={j} className="flex items-start gap-2 text-xs py-0.5">
          <span className="badge flex-shrink-0" style={{ color: 'var(--color-dice-gold)', borderColor: 'var(--color-dice-gold)' }}>{eventKindLabel(e.kind)}</span>
          <span className="flex-1">{e.trigger || '—'}</span>
          {eventValue(e) && <span className="flex-shrink-0" style={{ color: 'var(--color-danger)', fontFamily: 'var(--font-mono)' }}>{eventValue(e)}</span>}
          {e.note && <span className="flex-shrink-0" style={{ color: 'var(--color-text-secondary)' }}>{e.note}</span>}
        </div>
      ))}
    </div>
  )
}

/** 剧情变体列表（场景/NPC 的 states）：每个变体一个「条件(when) + 覆盖字段」卡片。 */
function VariantList<T extends { when?: string[] }>({ states, edit, onAdd, onRemove, onWhen, renderFields }: {
  states?: T[]
  edit: boolean
  onAdd: () => void
  onRemove: (j: number) => void
  onWhen: (j: number, flags: string[]) => void
  renderFields: (st: T, j: number) => React.ReactNode
}) {
  const list = states || []
  if (!edit && list.length === 0) return null
  return (
    <div className="mt-1 rounded" style={{ border: '1px dashed var(--color-border)', padding: 8 }}>
      <div className="flex items-center justify-between mb-1">
        <span className="text-xs font-semibold" style={{ color: 'var(--color-text-secondary)' }}>剧情变体（随剧情切换）</span>
        {edit && <button onClick={onAdd} className="btn-secondary text-xs !px-1.5 !py-0.5 flex items-center gap-1"><Plus size={11} />变体</button>}
      </div>
      {list.length === 0 && <p className="text-xs" style={{ color: 'var(--color-text-secondary)', opacity: 0.6 }}>无</p>}
      {list.map((st, j) => (
        <div key={j} className="rounded p-1.5 mb-1 relative" style={{ background: 'var(--color-bg-card)', border: '1px solid var(--color-border)' }}>
          {edit && <button onClick={() => onRemove(j)} className="absolute top-1 right-1 p-0.5" style={{ color: 'var(--color-danger)' }} title="删除变体"><Trash2 size={11} /></button>}
          <Row label="条件">{edit ? <TextInput value={csv(st.when)} onChange={(v) => onWhen(j, parseCsv(v))} placeholder="标志名，逗号分隔（全部激活才生效）" /> : <span className="text-xs">{csv(st.when) || '（恒生效）'}</span>}</Row>
          {renderFields(st, j)}
        </div>
      ))}
    </div>
  )
}

/** CoC 九维属性编辑/展示（3 列网格）。 */
function AttrGrid({ attrs, edit, onChange }: { attrs?: Record<string, number>; edit: boolean; onChange: (a: Record<string, number>) => void }) {
  const a = attrs || {}
  if (!edit) {
    const txt = COC_ATTRS.filter((k) => a[k] != null).map((k) => `${k} ${a[k]}`).join('、')
    return <span className="text-xs">{txt || '—'}</span>
  }
  return (
    <div className="grid grid-cols-3 gap-1">
      {COC_ATTRS.map((k) => (
        <label key={k} className="flex items-center gap-1 text-xs">
          <span style={{ width: 32, color: 'var(--color-text-secondary)' }}>{k}</span>
          <input type="number" value={a[k] ?? ''} onChange={(e) => { const next = { ...a }; if (e.target.value === '') delete next[k]; else next[k] = Number(e.target.value); onChange(next) }}
            className="w-full px-1 py-0.5 rounded" style={{ background: 'var(--color-bg-card)', border: '1px solid var(--color-border)', color: 'var(--color-text-primary)' }} />
        </label>
      ))}
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
        <button onClick={onRemove} className="absolute top-1.5 right-1.5 p-1 rounded hover:bg-[var(--color-danger-deep)] hover:text-white transition-colors" style={{ color: 'var(--color-danger)' }} title="删除">
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
