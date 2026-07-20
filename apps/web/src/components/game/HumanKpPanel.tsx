import { useEffect, useRef, useState } from 'react'
import { toast } from 'sonner'
import { api, getServerUrl } from '@/api/client'
import { GiRollingDices, GiScrollUnfurled } from 'react-icons/gi'
import {
  BookOpen,
  Bot,
  Image,
  Mic,
  MicOff,
  NotebookPen,
  RefreshCw,
  Send,
  Swords,
  WandSparkles,
} from 'lucide-react'

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

type PanelTab = 'tools' | 'advisor' | 'assets' | 'director'

interface CatalogItem {
  id: string
  name: string
}

interface DirectorSignals {
  spotlight_starved: string[]
  stuck: boolean
  stuck_turns: number
  unresolved_threads: string[]
  monotonous: boolean
}

interface ImageSuggestion {
  key: string
  title: string
  prompt: string
  image_kind: string
  image_item_id?: string
  image_field?: string
  preview_url?: string
  source_event_id?: string
}

interface Workspace {
  notes: string
  auto_ai_teammates: boolean
  image_suggestions?: ImageSuggestion[]
  has_ai_teammates: boolean
  has_unprocessed_player_turn: boolean
  signals: DirectorSignals
  catalogs: {
    scenes: CatalogItem[]
    npcs: CatalogItem[]
    handouts: CatalogItem[]
  }
}

interface AdvisorPlan {
  player_intent?: string
  requires_check?: boolean
  auto_outcome?: string
  auto_outcome_reason?: string
  check?: { skill?: string; difficulty?: string; chars?: string; reason?: string; bonus?: number; penalty?: number }
  clue_policy?: { candidate_clue_ids?: string[]; reveal_level?: string; notes?: string }
  npc_policy?: { speakers?: string[]; reaction?: string }
  scene_policy?: { scene_change?: string | null; set_flags?: string[]; clear_flags?: string[] }
  combat?: { should_start?: boolean; enemies?: string[]; trigger?: string }
  sanity?: { trigger?: boolean; source?: string; success_loss?: string; failure_loss?: string; witnesses?: string[] }
  direction?: { pacing?: string; spotlight?: string[]; nudge?: string; foreshadow?: string }
  narration_brief?: string[]
}

interface LookupHit {
  text: string
  page?: number
  scene_hint?: string | null
  score?: number
}

interface SpeechRecognitionResultEventLike {
  results: ArrayLike<{ 0: { transcript: string } }>
}

interface SpeechRecognitionLike {
  lang: string
  interimResults: boolean
  continuous: boolean
  onresult: ((event: SpeechRecognitionResultEventLike) => void) | null
  onerror: (() => void) | null
  onend: (() => void) | null
  start: () => void
  stop: () => void
}

type SpeechRecognitionCtor = new () => SpeechRecognitionLike

interface Props {
  sessionId: string
  turnReady?: boolean
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

const TABS: Array<{ id: PanelTab; label: string; icon: typeof WandSparkles }> = [
  { id: 'tools', label: '主持', icon: WandSparkles },
  { id: 'advisor', label: '参谋', icon: Bot },
  { id: 'assets', label: '配图与队友', icon: Image },
  { id: 'director', label: '导演台', icon: NotebookPen },
]

function imageUrl(src: string): string {
  if (/^https?:\/\//i.test(src)) return src
  return `${getServerUrl()}${src}`
}

export function HumanKpPanel({ sessionId, turnReady = false }: Props) {
  const [tab, setTab] = useState<PanelTab>('tools')
  const [action, setAction] = useState<KpAction>('narration')
  const [busy, setBusy] = useState('')
  const [fields, setFields] = useState<Record<string, string>>({})
  const [workspace, setWorkspace] = useState<Workspace | null>(null)
  const [notes, setNotes] = useState('')
  const [draftInstruction, setDraftInstruction] = useState('')
  const [draft, setDraft] = useState('')
  const [planFocus, setPlanFocus] = useState('')
  const [plan, setPlan] = useState<AdvisorPlan | null>(null)
  const [lookupScope, setLookupScope] = useState<'rule' | 'module'>('module')
  const [lookupQuery, setLookupQuery] = useState('')
  const [lookupHits, setLookupHits] = useState<LookupHit[]>([])
  const [imageTitle, setImageTitle] = useState('当前场景')
  const [imagePrompt, setImagePrompt] = useState('')
  const [previewUrl, setPreviewUrl] = useState('')
  const [previewSuggestionKey, setPreviewSuggestionKey] = useState('')
  const [dictating, setDictating] = useState(false)
  const recognitionRef = useRef<SpeechRecognitionLike | null>(null)

  const refreshWorkspace = async () => {
    try {
      const result = await api.get<Workspace>(`/sessions/${sessionId}/kp/workspace`)
      setWorkspace(result)
      setNotes(result.notes)
    } catch (error) {
      toast.error(error instanceof Error ? error.message : 'KP 工作区加载失败')
    }
  }

  useEffect(() => {
    void refreshWorkspace()
    return () => recognitionRef.current?.stop()
  }, [sessionId])

  useEffect(() => {
    if (tab === 'director' || tab === 'assets') void refreshWorkspace()
  }, [tab, turnReady])

  const setField = (key: string, value: string) => {
    setFields((current) => ({ ...current, [key]: value }))
  }

  const postAction = async (nextAction: KpAction, payload: Record<string, unknown>, success: string) => {
    setBusy(`action:${nextAction}`)
    try {
      await api.post(`/sessions/${sessionId}/kp/action`, { action: nextAction, payload })
      toast.success(success)
      return true
    } catch (error) {
      toast.error(error instanceof Error ? error.message : 'KP 动作执行失败')
      return false
    } finally {
      setBusy('')
    }
  }

  const submit = async () => {
    const payload = Object.fromEntries(
      Object.entries(fields).filter(([, value]) => value.trim()),
    )
    if (await postAction(action, payload, `${ACTION_LABELS[action]}已发布`)) setFields({})
  }

  const toggleDictation = () => {
    if (dictating) {
      recognitionRef.current?.stop()
      return
    }
    const speechWindow = window as Window & {
      SpeechRecognition?: SpeechRecognitionCtor
      webkitSpeechRecognition?: SpeechRecognitionCtor
    }
    const Recognition = speechWindow.SpeechRecognition || speechWindow.webkitSpeechRecognition
    if (!Recognition) {
      toast.error('当前浏览器不支持语音转文字')
      return
    }
    const recognition = new Recognition()
    recognition.lang = 'zh-CN'
    recognition.interimResults = false
    recognition.continuous = true
    recognition.onresult = (event) => {
      const transcript = Array.from(event.results).map((result) => result[0].transcript).join('')
      setFields((current) => ({
        ...current,
        content: `${current.content || ''}${transcript}`,
      }))
    }
    recognition.onerror = () => { setDictating(false); toast.error('语音识别中断') }
    recognition.onend = () => setDictating(false)
    recognitionRef.current = recognition
    setDictating(true)
    recognition.start()
  }

  const generateDraft = async () => {
    setBusy('draft')
    try {
      const result = await api.post<{ draft: string }>(`/sessions/${sessionId}/kp/advisor/draft`, {
        instruction: draftInstruction,
      })
      setDraft(result.draft)
    } catch (error) {
      toast.error(error instanceof Error ? error.message : '叙事草稿生成失败')
    } finally {
      setBusy('')
    }
  }

  const generatePlan = async () => {
    setBusy('plan')
    try {
      const result = await api.post<{ plan: AdvisorPlan }>(`/sessions/${sessionId}/kp/advisor/plan`, {
        focus: planFocus,
      })
      setPlan(result.plan)
    } catch (error) {
      toast.error(error instanceof Error ? error.message : '裁定建议生成失败')
    } finally {
      setBusy('')
    }
  }

  const runLookup = async () => {
    if (!lookupQuery.trim()) return
    setBusy('lookup')
    try {
      const params = new URLSearchParams({ scope: lookupScope, q: lookupQuery.trim() })
      const result = await api.get<{ hits: LookupHit[] }>(`/sessions/${sessionId}/kp/lookup?${params}`)
      setLookupHits(result.hits)
    } catch (error) {
      toast.error(error instanceof Error ? error.message : '资料检索失败')
    } finally {
      setBusy('')
    }
  }

  const updateAutoTeammates = async (enabled: boolean) => {
    setBusy('workspace')
    try {
      const result = await api.patch<Workspace>(`/sessions/${sessionId}/kp/workspace`, {
        auto_ai_teammates: enabled,
      })
      setWorkspace(result)
      toast.success(enabled ? '已开启 AI 队友自动行动' : '已关闭 AI 队友自动行动')
    } catch (error) {
      toast.error(error instanceof Error ? error.message : '设置保存失败')
    } finally {
      setBusy('')
    }
  }

  const runTeamTurn = async () => {
    setBusy('team')
    try {
      await api.post(`/sessions/${sessionId}/kp/team-turn`)
      toast.success('AI 队友正在处理本回合')
      setWorkspace((current) => current ? { ...current, has_unprocessed_player_turn: false } : current)
    } catch (error) {
      toast.error(error instanceof Error ? error.message : 'AI 队友行动失败')
    } finally {
      setBusy('')
    }
  }

  const endTurn = async () => {
    setBusy('end-turn')
    try {
      await api.post(`/sessions/${sessionId}/kp/end-turn`)
      toast.success('已开放下一回合')
      await refreshWorkspace()
    } catch (error) {
      toast.error(error instanceof Error ? error.message : '开放下一回合失败')
    } finally {
      setBusy('')
    }
  }

  const generateImage = async (suggestion?: ImageSuggestion) => {
    const title = suggestion?.title || imageTitle
    const prompt = suggestion?.prompt || imagePrompt
    if (!prompt.trim()) return
    if (suggestion) {
      setImageTitle(title)
      setImagePrompt(prompt)
      setPreviewSuggestionKey(suggestion.key)
      if (suggestion.preview_url) {
        setPreviewUrl(suggestion.preview_url)
        toast.success('已载入缓存预览，仅 KP 可见')
        return
      }
    }
    setBusy('image')
    try {
      const result = await api.post<{ url: string }>(`/sessions/${sessionId}/kp/images/preview`, {
        prompt,
        title,
      })
      setPreviewUrl(result.url)
      toast.success('图片已生成，仅 KP 可见')
    } catch (error) {
      toast.error(error instanceof Error ? error.message : '图片生成失败')
    } finally {
      setBusy('')
    }
  }

  const publishImage = async () => {
    setBusy('publish-image')
    try {
      await api.post(`/sessions/${sessionId}/kp/images/publish`, {
        url: previewUrl,
        title: imageTitle,
        suggestion_key: previewSuggestionKey,
      })
      toast.success('配图已发布给全桌')
      setPreviewUrl('')
      setPreviewSuggestionKey('')
      await refreshWorkspace()
    } catch (error) {
      toast.error(error instanceof Error ? error.message : '配图发布失败')
    } finally {
      setBusy('')
    }
  }

  const saveNotes = async () => {
    setBusy('notes')
    try {
      const result = await api.patch<Workspace>(`/sessions/${sessionId}/kp/workspace`, { notes })
      setWorkspace(result)
      toast.success('KP 笔记已保存')
    } catch (error) {
      toast.error(error instanceof Error ? error.message : '笔记保存失败')
    } finally {
      setBusy('')
    }
  }

  const Input = ({ name, placeholder, list }: { name: string; placeholder: string; list?: string }) => (
    <input
      value={fields[name] || ''}
      onChange={(event) => setField(name, event.target.value)}
      placeholder={placeholder}
      list={list}
      className="input min-w-0 flex-1 text-xs"
    />
  )

  const signals = workspace?.signals

  return (
    <section
      className="mx-3 mb-2 rounded-md px-3 py-2"
      style={{ background: 'var(--color-bg-tertiary)', border: '1px solid var(--color-border-strong)' }}
    >
      <div className="mb-2 flex items-center gap-1 overflow-x-auto">
        <span className="mr-2 whitespace-nowrap text-xs font-semibold" style={{ color: 'var(--color-text-accent)' }}>
          真人 KP
        </span>
        {TABS.map(({ id, label, icon: Icon }) => (
          <button
            key={id}
            type="button"
            onClick={() => setTab(id)}
            className="btn-secondary inline-flex items-center gap-1 whitespace-nowrap !px-2 !py-1 text-xs"
            style={tab === id ? { borderColor: 'var(--color-accent)', color: 'var(--color-text-accent)' } : undefined}
          >
            <Icon size={13} /> {label}
          </button>
        ))}
        <button
          type="button"
          onClick={() => void endTurn()}
          disabled={!!busy}
          className="btn-primary ml-auto inline-flex items-center gap-1 whitespace-nowrap !px-2 !py-1 text-xs"
        >
          <Send size={13} /> {busy === 'end-turn' ? '处理中…' : '开放行动'}
        </button>
      </div>

      {tab === 'tools' && (
        <div>
          <div className="mb-2 flex items-center gap-2">
            <select
              value={action}
              onChange={(event) => { setAction(event.target.value as KpAction); setFields({}) }}
              className="input !w-auto text-xs"
              aria-label="KP 动作"
            >
              {Object.entries(ACTION_LABELS).map(([value, label]) => (
                <option key={value} value={value}>{label}</option>
              ))}
            </select>
          </div>
          <div className="flex flex-wrap gap-2">
            {action === 'narration' && (
              <div className="flex w-full items-start gap-2">
                <textarea
                  value={fields.content || ''}
                  onChange={(event) => setField('content', event.target.value)}
                  placeholder="输入要发布给全桌的旁白"
                  className="input min-h-16 min-w-0 flex-1 resize-y text-xs"
                />
                <button type="button" onClick={toggleDictation} className="btn-secondary !p-2" title="语音转文字">
                  {dictating ? <MicOff size={15} /> : <Mic size={15} />}
                </button>
              </div>
            )}
            {action === 'dialogue' && <><Input name="npc_id" placeholder="NPC 名称或 ID" list="kp-npcs" /><Input name="content" placeholder="台词内容" /></>}
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
            {action === 'scene_change' && <Input name="scene_id" placeholder="场景 ID 或名称" list="kp-scenes" />}
            {(action === 'set_flag' || action === 'clear_flag') && <Input name="flag" placeholder="剧情标志" />}
            {action === 'handout' && <><GiScrollUnfurled size={16} /><Input name="id" placeholder="手书 ID" list="kp-handouts" /></>}
            {action === 'hp_change' && <><Input name="target" placeholder="角色名" /><Input name="delta" placeholder="变化值，如 -3 或 2" /><Input name="reason" placeholder="原因（可选）" /></>}
            {action === 'start_combat' && <><Swords size={16} /><Input name="enemies" placeholder="敌人名称，多个用逗号分隔" list="kp-npcs" /><Input name="trigger" placeholder="开战原因（可选）" /></>}
            <button onClick={() => void submit()} disabled={!!busy} className="btn-primary inline-flex items-center gap-1 text-xs">
              {action === 'dice_check' ? <GiRollingDices size={13} /> : <Send size={13} />}
              {busy ? '处理中…' : ACTION_LABELS[action]}
            </button>
          </div>
        </div>
      )}

      {tab === 'advisor' && (
        <div className="space-y-3">
          <div className="flex flex-wrap gap-2 border-b pb-3" style={{ borderColor: 'var(--color-border)' }}>
            <input value={planFocus} onChange={(event) => setPlanFocus(event.target.value)} placeholder="裁定重点（可选）" className="input min-w-48 flex-1 text-xs" />
            <button onClick={() => void generatePlan()} disabled={!!busy} className="btn-secondary inline-flex items-center gap-1 text-xs">
              <Bot size={13} /> {busy === 'plan' ? '分析中…' : '生成裁定建议'}
            </button>
            {plan && (
              <div className="w-full space-y-1 text-xs" style={{ color: 'var(--color-text-secondary)' }}>
                <div><strong style={{ color: 'var(--color-text-primary)' }}>玩家意图：</strong>{plan.player_intent || '未识别'}</div>
                {plan.requires_check && plan.check?.skill && (
                  <div className="flex flex-wrap items-center gap-2">
                    <span>建议检定：{plan.check.skill} / {plan.check.difficulty || 'normal'} {plan.check.reason && `· ${plan.check.reason}`}</span>
                    <button className="btn-secondary !px-2 !py-0.5" onClick={() => void postAction('dice_check', plan.check || {}, '已按建议发起检定')}>照此发起</button>
                  </div>
                )}
                {plan.auto_outcome && plan.auto_outcome !== 'none' && <div>免检结论：{plan.auto_outcome} · {plan.auto_outcome_reason}</div>}
                {!!plan.clue_policy?.candidate_clue_ids?.length && <div>候选线索：{plan.clue_policy.candidate_clue_ids.join('、')} · {plan.clue_policy.notes}</div>}
                {plan.npc_policy?.reaction && <div>NPC 反应：{plan.npc_policy.reaction}</div>}
                {plan.scene_policy?.scene_change && (
                  <div className="flex items-center gap-2">建议切场：{plan.scene_policy.scene_change}<button className="btn-secondary !px-2 !py-0.5" onClick={() => void postAction('scene_change', { scene_id: plan.scene_policy?.scene_change }, '已按建议切换场景')}>执行</button></div>
                )}
                {!!plan.scene_policy?.set_flags?.length && <div className="flex flex-wrap items-center gap-2">建议推进标志：{plan.scene_policy.set_flags.map((flag) => <button key={flag} className="btn-secondary !px-2 !py-0.5" onClick={() => void postAction('set_flag', { flag }, `已推进标志 ${flag}`)}>{flag}</button>)}</div>}
                {!!plan.scene_policy?.clear_flags?.length && <div className="flex flex-wrap items-center gap-2">建议解除标志：{plan.scene_policy.clear_flags.map((flag) => <button key={flag} className="btn-secondary !px-2 !py-0.5" onClick={() => void postAction('clear_flag', { flag }, `已解除标志 ${flag}`)}>{flag}</button>)}</div>}
                {plan.sanity?.trigger && (
                  <div className="flex items-center gap-2">建议理智检定：{plan.sanity.source}<button className="btn-secondary !px-2 !py-0.5" onClick={() => void postAction('san_check', { chars: plan.sanity?.witnesses?.join('、'), source: plan.sanity?.source, success_loss: plan.sanity?.success_loss, failure_loss: plan.sanity?.failure_loss }, '已按建议进行理智检定')}>执行</button></div>
                )}
                {plan.combat?.should_start && (
                  <div className="flex items-center gap-2">建议开战：{plan.combat.enemies?.join('、')}<button className="btn-secondary !px-2 !py-0.5" onClick={() => void postAction('start_combat', { enemies: plan.combat?.enemies?.join('、'), trigger: plan.combat?.trigger }, '已按建议开始战斗')}>执行</button></div>
                )}
                {(plan.direction?.nudge || plan.direction?.foreshadow) && <div>导演建议：{plan.direction.nudge || plan.direction.foreshadow}</div>}
              </div>
            )}
          </div>

          <div className="flex flex-wrap gap-2 border-b pb-3" style={{ borderColor: 'var(--color-border)' }}>
            <input value={draftInstruction} onChange={(event) => setDraftInstruction(event.target.value)} placeholder="希望草稿呈现的内容、语气或长度" className="input min-w-48 flex-1 text-xs" />
            <button onClick={() => void generateDraft()} disabled={!!busy} className="btn-secondary inline-flex items-center gap-1 text-xs">
              <WandSparkles size={13} /> {busy === 'draft' ? '起草中…' : '生成叙事草稿'}
            </button>
            {draft && <>
              <textarea value={draft} onChange={(event) => setDraft(event.target.value)} className="input min-h-24 w-full resize-y text-xs" />
              <button onClick={() => void postAction('narration', { content: draft }, '叙事草稿已发布')} className="btn-primary inline-flex items-center gap-1 text-xs"><Send size={13} />发布草稿</button>
            </>}
          </div>

          <div className="flex flex-wrap gap-2">
            <select value={lookupScope} onChange={(event) => setLookupScope(event.target.value as 'rule' | 'module')} className="input !w-auto text-xs">
              <option value="module">模组原文</option><option value="rule">规则书</option>
            </select>
            <input value={lookupQuery} onChange={(event) => setLookupQuery(event.target.value)} placeholder="输入检索关键词" className="input min-w-48 flex-1 text-xs" />
            <button onClick={() => void runLookup()} disabled={!!busy} className="btn-secondary inline-flex items-center gap-1 text-xs"><BookOpen size={13} />{busy === 'lookup' ? '检索中…' : '速查'}</button>
            {!!lookupHits.length && <div className="max-h-40 w-full space-y-2 overflow-y-auto text-xs">{lookupHits.map((hit, index) => <div key={`${hit.page || hit.scene_hint || ''}-${index}`} className="border-l-2 pl-2" style={{ borderColor: 'var(--color-border-strong)', color: 'var(--color-text-secondary)' }}><div className="mb-1 font-semibold">{hit.page ? `第 ${hit.page} 页` : hit.scene_hint || `片段 ${index + 1}`}</div><div className="whitespace-pre-wrap">{hit.text}</div></div>)}</div>}
          </div>
        </div>
      )}

      {tab === 'assets' && (
        <div className="space-y-3">
          <div className="flex flex-wrap items-center gap-3 border-b pb-3" style={{ borderColor: 'var(--color-border)' }}>
            <label className="inline-flex items-center gap-2 text-xs">
              <input type="checkbox" checked={workspace?.auto_ai_teammates || false} disabled={!workspace?.has_ai_teammates || !!busy} onChange={(event) => void updateAutoTeammates(event.target.checked)} />
              玩家提交后让 AI 队友自动行动
            </label>
            <button onClick={() => void runTeamTurn()} disabled={!workspace?.has_ai_teammates || !workspace?.has_unprocessed_player_turn || !!busy} className="btn-secondary inline-flex items-center gap-1 text-xs">
              <Bot size={13} /> {busy === 'team' ? '行动中…' : '让 AI 队友行动'}
            </button>
          </div>
          {!!workspace?.image_suggestions?.length && (
            <div className="space-y-2 border-b pb-3" style={{ borderColor: 'var(--color-border)' }}>
              <div className="flex items-center gap-1 text-xs font-semibold" style={{ color: 'var(--color-text-primary)' }}>
                <Image size={13} /> 待审核配图
              </div>
              <div className="space-y-2">
                {workspace.image_suggestions.map((suggestion) => (
                  <div key={suggestion.key} className="flex flex-wrap items-center gap-2 text-xs" style={{ color: 'var(--color-text-secondary)' }}>
                    {suggestion.preview_url && <img src={imageUrl(suggestion.preview_url)} alt="" className="h-10 w-14 rounded object-cover" />}
                    <span className="min-w-0 flex-1">{suggestion.title}</span>
                    <button type="button" onClick={() => void generateImage(suggestion)} disabled={!!busy} className="btn-secondary inline-flex items-center gap-1 !px-2 !py-1 text-xs">
                      <Image size={12} /> {suggestion.preview_url ? '采用缓存预览' : '生成预览'}
                    </button>
                  </div>
                ))}
              </div>
            </div>
          )}
          <div className="flex flex-wrap gap-2">
            <input value={imageTitle} onChange={(event) => setImageTitle(event.target.value)} placeholder="配图标题" className="input w-36 text-xs" />
            <input value={imagePrompt} onChange={(event) => setImagePrompt(event.target.value)} placeholder="描述希望生成的画面" className="input min-w-48 flex-1 text-xs" />
            <button onClick={() => void generateImage()} disabled={!!busy || !imagePrompt.trim()} className="btn-secondary inline-flex items-center gap-1 text-xs"><Image size={13} />{busy === 'image' ? '生成中…' : '生成预览'}</button>
            {previewUrl && <div className="w-full"><img src={imageUrl(previewUrl)} alt={imageTitle} className="max-h-64 w-full rounded object-contain" /><div className="mt-2 flex justify-end"><button onClick={() => void publishImage()} disabled={!!busy} className="btn-primary inline-flex items-center gap-1 text-xs"><Send size={13} />发布给全桌</button></div></div>}
          </div>
        </div>
      )}

      {tab === 'director' && (
        <div className="space-y-3">
          <div className="flex items-start gap-2 border-b pb-3 text-xs" style={{ borderColor: 'var(--color-border)' }}>
            <div className="min-w-0 flex-1 space-y-1" style={{ color: 'var(--color-text-secondary)' }}>
              {!!signals?.spotlight_starved.length && <div>冷场角色：{signals.spotlight_starved.join('、')}</div>}
              {signals?.stuck && <div>卡关迹象：约 {signals.stuck_turns} 个玩家回合没有实质进展</div>}
              {signals?.monotonous && <div>节奏单调：近期调查动作密集，可加入人物互动或情绪换气</div>}
              {!!signals?.unresolved_threads.length && <div>待回收：{signals.unresolved_threads.join('；')}</div>}
              {!signals?.spotlight_starved.length && !signals?.stuck && !signals?.monotonous && !signals?.unresolved_threads.length && <div>当前没有明显的节奏风险。</div>}
            </div>
            <button onClick={() => void refreshWorkspace()} className="btn-secondary !p-1.5" title="刷新导演信号"><RefreshCw size={13} /></button>
          </div>
          <textarea value={notes} onChange={(event) => setNotes(event.target.value)} placeholder="记录尚未公开的秘密、临场裁定与后续安排" className="input min-h-28 w-full resize-y text-xs" />
          <button onClick={() => void saveNotes()} disabled={!!busy} className="btn-primary inline-flex items-center gap-1 text-xs"><NotebookPen size={13} />{busy === 'notes' ? '保存中…' : '保存私有笔记'}</button>
        </div>
      )}

      <datalist id="kp-scenes">{workspace?.catalogs.scenes.map((item) => <option key={item.id} value={item.id}>{item.name}</option>)}</datalist>
      <datalist id="kp-npcs">{workspace?.catalogs.npcs.map((item) => <option key={item.id} value={item.name}>{item.id}</option>)}</datalist>
      <datalist id="kp-handouts">{workspace?.catalogs.handouts.map((item) => <option key={item.id} value={item.id}>{item.name}</option>)}</datalist>
    </section>
  )
}
