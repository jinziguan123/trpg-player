import { type PointerEvent as ReactPointerEvent, useEffect, useRef, useState } from 'react'
import { toast } from 'sonner'
import { api, getServerUrl } from '@/api/client'
import { GiRollingDices, GiScrollUnfurled } from 'react-icons/gi'
import {
  BookOpen,
  Bot,
  ChevronDown,
  ChevronUp,
  Crosshair,
  Image,
  Info,
  Mic,
  MicOff,
  Maximize2,
  Move,
  NotebookPen,
  RefreshCw,
  Search,
  Send,
  Swords,
  WandSparkles,
  X,
} from 'lucide-react'
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from '@/components/ui/tooltip'

type KpAction =
  | 'narration'
  | 'dialogue'
  | 'dice_check'
  | 'opposed_check'
  | 'generic_roll'
  | 'san_check'
  | 'scene_change'
  | 'set_flag'
  | 'clear_flag'
  | 'handout'
  | 'hp_change'
  | 'start_combat'

type PanelTab = 'tools' | 'advisor' | 'assets' | 'director' | 'source'

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
  player_missing?: boolean
  image_suggestions?: ImageSuggestion[]
  has_ai_teammates: boolean
  has_unprocessed_player_turn: boolean
  signals: DirectorSignals
  catalogs: {
    characters: CatalogItem[]
    scenes: CatalogItem[]
    npcs: CatalogItem[]
    handouts: CatalogItem[]
  }
}

interface ModuleSource {
  id: string
  title: string
  description: string
  raw_content: string
  world_setting: Record<string, unknown>
  truth: string
  scenes: unknown[]
  npcs: unknown[]
  clues: unknown[]
  triggers: unknown[]
  handouts: unknown[]
  maps: unknown[]
  rag_status: string
  chunks: Array<{ ordinal: number; scene_hint?: string | null; text: string }>
}

interface KpInputProps {
  name: string
  placeholder: string
  list?: string
  fields: Record<string, string>
  onChange: (name: string, value: string) => void
}

function KpInput({ name, placeholder, list, fields, onChange }: KpInputProps) {
  return (
    <input
      value={fields[name] || ''}
      onChange={(event) => onChange(name, event.target.value)}
      placeholder={placeholder}
      list={list}
      className="input min-w-0 flex-1 text-xs"
    />
  )
}

interface ActorSelectProps {
  label: string
  value: string
  workspace: Workspace | null
  onChange: (value: string) => void
  includeGroup?: boolean
  disabledValue?: string
}

function ActorSelect({
  label, value, workspace, onChange, includeGroup = false, disabledValue,
}: ActorSelectProps) {
  return (
    <select
      value={value}
      onChange={(event) => onChange(event.target.value)}
      className="input min-w-40 flex-1 text-xs"
      aria-label={label}
    >
      <option value="">选择{label}</option>
      {includeGroup && <option value="在场">在场所有角色</option>}
      {!!workspace?.catalogs.characters?.length && (
        <optgroup label="游戏角色">
          {workspace.catalogs.characters.map((item) => {
            const ref = `character:${item.id}`
            return <option key={ref} value={ref} disabled={ref === disabledValue}>{item.name}</option>
          })}
        </optgroup>
      )}
      {!!workspace?.catalogs.npcs.length && (
        <optgroup label="NPC">
          {workspace.catalogs.npcs.map((item) => {
            const ref = `npc:${item.id}`
            return <option key={ref} value={ref} disabled={ref === disabledValue}>{item.name}</option>
          })}
        </optgroup>
      )}
    </select>
  )
}

function DiceAdjustment({
  label, bonus, penalty, onChange,
}: { label: string; bonus: string; penalty: string; onChange: (bonus: string, penalty: string) => void }) {
  const value = bonus && bonus !== '0' ? `bonus:${bonus}` : penalty && penalty !== '0' ? `penalty:${penalty}` : 'none'
  return (
    <select
      value={value}
      onChange={(event) => {
        const [kind, count = ''] = event.target.value.split(':')
        onChange(kind === 'bonus' ? count : '', kind === 'penalty' ? count : '')
      }}
      className="input w-auto text-xs"
      aria-label={label}
    >
      <option value="none">无奖惩骰</option>
      <option value="bonus:1">奖励骰 +1</option>
      <option value="bonus:2">奖励骰 +2</option>
      <option value="penalty:1">惩罚骰 +1</option>
      <option value="penalty:2">惩罚骰 +2</option>
    </select>
  )
}

function BlindToggle({ checked, onChange }: { checked: boolean; onChange: (checked: boolean) => void }) {
  return (
    <label className="inline-flex shrink-0 items-center gap-1.5 text-xs" style={{ color: 'var(--color-text-secondary)' }}>
      <input type="checkbox" checked={checked} onChange={(event) => onChange(event.target.checked)} />
      暗投
    </label>
  )
}

function AdvisorHelp({ label, text }: { label: string; text: string }) {
  return (
    <TooltipProvider delayDuration={200}>
      <Tooltip>
        <TooltipTrigger asChild>
          <button
            type="button"
            aria-label={`${label}说明`}
            className="inline-flex h-6 w-6 shrink-0 items-center justify-center rounded-full transition-colors hover:bg-[var(--color-bg-secondary)] focus-visible:outline-2 focus-visible:outline-offset-1 focus-visible:outline-[var(--color-accent)]"
            style={{ color: 'var(--color-text-accent)' }}
          >
            <Info size={15} aria-hidden="true" />
          </button>
        </TooltipTrigger>
        <TooltipContent side="top" className="max-w-72 whitespace-normal leading-relaxed">
          {text}
        </TooltipContent>
      </Tooltip>
    </TooltipProvider>
  )
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
  rulebook_id?: string
  ordinal?: number
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
  generic_roll: '通用骰',
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
  { id: 'source', label: '模组资料', icon: BookOpen },
]

function imageUrl(src: string): string {
  if (/^https?:\/\//i.test(src)) return src
  return `${getServerUrl()}${src}`
}

function highlightText(text: string, query: string) {
  const needle = query.trim()
  if (!needle) return text
  const escaped = needle.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
  const parts = text.split(new RegExp(`(${escaped})`, 'ig'))
  return parts.map((part, index) => (
    part.toLocaleLowerCase() === needle.toLocaleLowerCase()
      ? (
          <mark
            key={`${part}-${index}`}
            className="rounded px-0.5 font-semibold"
            style={{ background: 'var(--color-accent)', color: 'var(--color-on-accent)' }}
          >
            {part}
          </mark>
        )
      : part
  ))
}

export function HumanKpPanel({ sessionId, turnReady = false }: Props) {
  const [collapsed, setCollapsed] = useState(false)
  const [tab, setTab] = useState<PanelTab>('tools')
  const [action, setAction] = useState<KpAction>('narration')
  const [busy, setBusy] = useState('')
  const [fields, setFields] = useState<Record<string, string>>({})
  const [lastActionResult, setLastActionResult] = useState('')
  const [workspace, setWorkspace] = useState<Workspace | null>(null)
  const [source, setSource] = useState<ModuleSource | null>(null)
  const [sourceView, setSourceView] = useState<'raw' | 'parsed'>('raw')
  const [sourceOpen, setSourceOpen] = useState(false)
  const [sourceQuery, setSourceQuery] = useState('')
  const [sourceTargetOrdinal, setSourceTargetOrdinal] = useState<number | null>(null)
  const [sourcePosition, setSourcePosition] = useState({ x: 0, y: 0 })
  const [sourceSize, setSourceSize] = useState({ width: 640, height: 680 })
  const [notes, setNotes] = useState('')
  const [draftInstruction, setDraftInstruction] = useState('')
  const [draft, setDraft] = useState('')
  const [planFocus, setPlanFocus] = useState('')
  const [plan, setPlan] = useState<AdvisorPlan | null>(null)
  const [lookupScope, setLookupScope] = useState<'rule' | 'module'>('module')
  const [lookupQuery, setLookupQuery] = useState('')
  const [lookupHits, setLookupHits] = useState<LookupHit[]>([])
  const [lookupOpen, setLookupOpen] = useState(false)
  const [imageTitle, setImageTitle] = useState('当前场景')
  const [imagePrompt, setImagePrompt] = useState('')
  const [previewUrl, setPreviewUrl] = useState('')
  const [previewSuggestionKey, setPreviewSuggestionKey] = useState('')
  const [dictating, setDictating] = useState(false)
  const recognitionRef = useRef<SpeechRecognitionLike | null>(null)
  const sourceChunkRefs = useRef<Record<number, HTMLDivElement | null>>({})
  const sourceDragRef = useRef<{ startX: number; startY: number; originX: number; originY: number } | null>(null)
  const sourceResizeRef = useRef<{
    startX: number
    startY: number
    width: number
    height: number
    originX: number
  } | null>(null)

  const refreshWorkspace = async () => {
    try {
      const result = await api.get<Workspace>(`/sessions/${sessionId}/kp/workspace`)
      setWorkspace(result)
      setNotes(result.notes)
    } catch (error) {
      toast.error(error instanceof Error ? error.message : 'KP 工作区加载失败')
    }
  }

  const openModuleSource = async (force = false): Promise<ModuleSource | null> => {
    setSourceOpen(true)
    if (source && !force) return source
    setBusy('source')
    try {
      const loaded = await api.get<ModuleSource>(`/sessions/${sessionId}/kp/source`)
      setSource(loaded)
      return loaded
    } catch (error) {
      setSourceOpen(false)
      toast.error(error instanceof Error ? error.message : '模组资料加载失败')
      return null
    } finally {
      setBusy('')
    }
  }

  const onSourceDragStart = (event: ReactPointerEvent<HTMLDivElement>) => {
    if ((event.target as HTMLElement).closest('button, input')) return
    event.currentTarget.setPointerCapture(event.pointerId)
    sourceDragRef.current = {
      startX: event.clientX,
      startY: event.clientY,
      originX: sourcePosition.x,
      originY: sourcePosition.y,
    }
  }

  const onSourceDragMove = (event: ReactPointerEvent<HTMLDivElement>) => {
    const drag = sourceDragRef.current
    if (!drag) return
    setSourcePosition({
      x: drag.originX + event.clientX - drag.startX,
      y: drag.originY + event.clientY - drag.startY,
    })
  }

  const onSourceDragEnd = () => {
    sourceDragRef.current = null
  }

  const onSourceResizeStart = (event: ReactPointerEvent<HTMLDivElement>) => {
    event.stopPropagation()
    event.currentTarget.setPointerCapture(event.pointerId)
    sourceResizeRef.current = {
      startX: event.clientX,
      startY: event.clientY,
      width: sourceSize.width,
      height: sourceSize.height,
      originX: sourcePosition.x,
    }
  }

  const onSourceResizeMove = (event: ReactPointerEvent<HTMLDivElement>) => {
    const resize = sourceResizeRef.current
    if (!resize) return
    const maxWidth = typeof window === 'undefined' ? 960 : Math.max(360, window.innerWidth - 32)
    const maxHeight = typeof window === 'undefined' ? 900 : Math.max(320, window.innerHeight - 96)
    const width = Math.min(maxWidth, Math.max(360, resize.width + event.clientX - resize.startX))
    const height = Math.min(maxHeight, Math.max(320, resize.height + event.clientY - resize.startY))
    setSourceSize({ width, height })
    setSourcePosition((current) => ({
      ...current,
      x: resize.originX + width - resize.width,
    }))
  }

  const onSourceResizeEnd = () => {
    sourceResizeRef.current = null
  }

  useEffect(() => {
    void refreshWorkspace()
    return () => recognitionRef.current?.stop()
  }, [sessionId])

  useEffect(() => {
    if (tab === 'director' || tab === 'assets') void refreshWorkspace()
  }, [tab, turnReady])

  useEffect(() => {
    sourceChunkRefs.current = {}
    setSourcePosition({ x: 0, y: 0 })
    setSourceSize({ width: 640, height: 680 })
    setSourceQuery('')
    setSourceTargetOrdinal(null)
    setSource(null)
    setSourceOpen(false)
  }, [sessionId])

  useEffect(() => {
    if (!sourceOpen || sourceView !== 'parsed' || sourceTargetOrdinal === null) return
    const frame = window.requestAnimationFrame(() => {
      sourceChunkRefs.current[sourceTargetOrdinal]?.scrollIntoView({ behavior: 'smooth', block: 'center' })
    })
    return () => window.cancelAnimationFrame(frame)
  }, [sourceOpen, sourceView, sourceTargetOrdinal, source])

  const setField = (key: string, value: string) => {
    setFields((current) => ({ ...current, [key]: value }))
  }

  const setDiceAdjustment = (prefix: string, bonus: string, penalty: string) => {
    setFields((current) => ({
      ...current,
      [`${prefix}bonus`]: bonus,
      [`${prefix}penalty`]: penalty,
    }))
  }

  const postAction = async (nextAction: KpAction, payload: Record<string, unknown>, success: string) => {
    setBusy(`action:${nextAction}`)
    try {
      const response = await api.post<{ result?: string }>(`/sessions/${sessionId}/kp/action`, { action: nextAction, payload })
      const result = response.result || success
      setLastActionResult(result)
      toast.success(success)
      return result
    } catch (error) {
      toast.error(error instanceof Error ? error.message : 'KP 动作执行失败')
      return null
    } finally {
      setBusy('')
    }
  }

  const submit = async () => {
    if (busy) return
    const defaultActor = workspace?.catalogs.characters?.[0]
      ? `character:${workspace.catalogs.characters[0].id}`
      : ''
    const nextFields = { ...fields }
    if (action === 'dice_check' && !nextFields.char) nextFields.char = defaultActor
    if (action === 'opposed_check' && !nextFields.a) nextFields.a = defaultActor
    const payload = Object.fromEntries(
      Object.entries(nextFields).filter(([, value]) => value.trim()),
    )
    if (await postAction(action, payload, `${ACTION_LABELS[action]}已发布`)) {
      // 连续 NPC 台词时保留说话人，只清空上一句正文。
      setFields(action === 'dialogue' ? { npc_id: fields.npc_id || '' } : {})
    }
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
      setLookupOpen(true)
    } catch (error) {
      toast.error(error instanceof Error ? error.message : '资料检索失败')
    } finally {
      setBusy('')
    }
  }

  const openModuleAtHit = async (hit: LookupHit) => {
    setLookupOpen(false)
    setSourceView('parsed')
    setSourceQuery(hit.text.slice(0, 120))
    setSourceTargetOrdinal(typeof hit.ordinal === 'number' ? hit.ordinal : null)
    const loaded = await openModuleSource()
    if (!loaded?.chunks?.length) setSourceView('raw')
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

  const signals = workspace?.signals
  const defaultActorRef = workspace?.catalogs.characters?.[0]
    ? `character:${workspace.catalogs.characters[0].id}`
    : ''
  const sceneLabel = (sceneId: string | null | undefined) => (
    workspace?.catalogs.scenes.find((scene) => scene.id === sceneId)?.name || sceneId || '模组原文片段'
  )
  const sourceSections: Array<[string, unknown]> = [
    ['世界设定', source?.world_setting],
    ['幕后真相', source?.truth],
    ['场景', source?.scenes],
    ['NPC', source?.npcs],
    ['线索', source?.clues],
    ['触发器', source?.triggers],
    ['手书', source?.handouts],
    ['地图', source?.maps],
  ]
  const normalizedSourceQuery = sourceQuery.trim().toLocaleLowerCase()
  const sourceChunks = source?.chunks || []
  const visibleSourceChunks = sourceChunks.filter((chunk) => (
    !normalizedSourceQuery
    || chunk.text.toLocaleLowerCase().includes(normalizedSourceQuery)
    || String(chunk.scene_hint || '').toLocaleLowerCase().includes(normalizedSourceQuery)
  ))

  return (
    <section
      className="mx-3 mb-2 rounded-md px-3 py-2"
      style={{ background: 'var(--color-bg-tertiary)', border: '1px solid var(--color-border-strong)' }}
    >
      <div className={`${collapsed ? '' : 'mb-2'} flex items-center gap-1 overflow-x-auto`}>
        <span className="mr-2 whitespace-nowrap text-xs font-semibold" style={{ color: 'var(--color-text-accent)' }}>
          真人 KP
        </span>
        {!collapsed && TABS.map(({ id, label, icon: Icon }) => (
          <button
            key={id}
            type="button"
            onClick={() => {
              setTab(id)
              if (id === 'source') {
                if (sourceOpen) setSourceOpen(false)
                else void openModuleSource()
              }
            }}
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
        <button
          type="button"
          onClick={() => setCollapsed((value) => !value)}
          className="btn-secondary !p-1.5"
          aria-expanded={!collapsed}
          title={collapsed ? '展开 KP 控制台' : '收起 KP 控制台'}
        >
          {collapsed ? <ChevronDown size={15} /> : <ChevronUp size={15} />}
        </button>
      </div>

      {!collapsed && tab === 'tools' && (
        <div>
          {workspace?.player_missing && (
            <div className="mb-2 rounded border px-2 py-1.5 text-xs" style={{ borderColor: 'var(--color-warning)', color: 'var(--color-warning)' }}>
              当前还没有真人玩家角色。玩家入座并选择角色后，参谋才能生成针对具体玩家的裁定建议。
            </div>
          )}
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
            {action === 'dialogue' && <>
              <KpInput name="npc_id" placeholder="NPC 名称或 ID" list="kp-npcs" fields={fields} onChange={setField} />
              <textarea
                value={fields.content || ''}
                onChange={(event) => setField('content', event.target.value)}
                onKeyDown={(event) => {
                  if ((event.metaKey || event.ctrlKey) && event.key === 'Enter') {
                    event.preventDefault()
                    void submit()
                  }
                }}
                placeholder="台词内容（可连续输入；按 Ctrl/Cmd+Enter 发布）"
                className="input min-h-16 min-w-48 flex-1 resize-y text-xs"
              />
            </>}
            {action === 'dice_check' && <>
              <KpInput name="skill" placeholder="技能，如 幸运、侦查" fields={fields} onChange={setField} />
              <ActorSelect
                label="检定对象"
                value={fields.char || defaultActorRef}
                workspace={workspace}
                includeGroup
                onChange={(value) => setField('char', value)}
              />
              <select value={fields.difficulty || 'normal'} onChange={(event) => setField('difficulty', event.target.value)} className="input w-auto text-xs">
                <option value="normal">普通</option><option value="hard">困难</option><option value="extreme">极难</option>
              </select>
              <DiceAdjustment
                label="检定奖惩骰"
                bonus={fields.bonus || ''}
                penalty={fields.penalty || ''}
                onChange={(bonus, penalty) => setDiceAdjustment('', bonus, penalty)}
              />
              <BlindToggle checked={fields.visibility === 'blind'} onChange={(checked) => setField('visibility', checked ? 'blind' : '')} />
              <KpInput name="source" placeholder="目标/来源（可选）" fields={fields} onChange={setField} />
            </>}
            {action === 'opposed_check' && <>
              <div className="flex w-full flex-wrap items-center gap-2">
                <span className="w-10 shrink-0 text-xs font-semibold" style={{ color: 'var(--color-text-secondary)' }}>甲方</span>
                <ActorSelect
                  label="甲方对象"
                  value={fields.a || defaultActorRef}
                  workspace={workspace}
                  disabledValue={fields.b}
                  onChange={(value) => setField('a', value)}
                />
                <KpInput name="a_skill" placeholder="甲方技能" fields={fields} onChange={setField} />
                <DiceAdjustment
                  label="甲方奖惩骰"
                  bonus={fields.a_bonus || ''}
                  penalty={fields.a_penalty || ''}
                  onChange={(bonus, penalty) => setDiceAdjustment('a_', bonus, penalty)}
                />
              </div>
              <div className="flex w-full flex-wrap items-center gap-2">
                <span className="w-10 shrink-0 text-xs font-semibold" style={{ color: 'var(--color-text-secondary)' }}>乙方</span>
                <ActorSelect
                  label="乙方对象"
                  value={fields.b || ''}
                  workspace={workspace}
                  disabledValue={fields.a || defaultActorRef}
                  onChange={(value) => setField('b', value)}
                />
                <KpInput name="b_skill" placeholder="乙方技能" fields={fields} onChange={setField} />
                <DiceAdjustment
                  label="乙方奖惩骰"
                  bonus={fields.b_bonus || ''}
                  penalty={fields.b_penalty || ''}
                  onChange={(bonus, penalty) => setDiceAdjustment('b_', bonus, penalty)}
                />
              </div>
              <BlindToggle checked={fields.visibility === 'blind'} onChange={(checked) => setField('visibility', checked ? 'blind' : '')} />
            </>}
            {action === 'generic_roll' && <>
              <input type="number" min="1" max="20" value={fields.count || '1'} onChange={(event) => setField('count', event.target.value)} className="input w-20 text-xs" aria-label="骰子数量" title="骰子数量" />
              <span className="self-center text-xs" style={{ color: 'var(--color-text-secondary)' }}>颗</span>
              <input type="number" min="2" max="1000" value={fields.sides || '6'} onChange={(event) => setField('sides', event.target.value)} className="input w-24 text-xs" aria-label="骰子面数" title="骰子面数" />
              <span className="self-center text-xs" style={{ color: 'var(--color-text-secondary)' }}>面</span>
              <input type="number" min="-10000" max="10000" value={fields.modifier || '0'} onChange={(event) => setField('modifier', event.target.value)} className="input w-24 text-xs" aria-label="结果修正值" title="结果修正值" />
              <KpInput name="reason" placeholder="掷骰用途，如 参战敌人数" fields={fields} onChange={setField} />
              <BlindToggle checked={fields.visibility === 'blind'} onChange={(checked) => setField('visibility', checked ? 'blind' : '')} />
            </>}
            {action === 'san_check' && <>
              <KpInput name="chars" placeholder="目睹者，空=全队" fields={fields} onChange={setField} /><KpInput name="source" placeholder="恐怖源" fields={fields} onChange={setField} />
              <KpInput name="success_loss" placeholder="成功损失，如 0" fields={fields} onChange={setField} /><KpInput name="failure_loss" placeholder="失败损失，如 1d6" fields={fields} onChange={setField} />
            </>}
            {action === 'scene_change' && <KpInput name="scene_id" placeholder="场景 ID 或名称" list="kp-scenes" fields={fields} onChange={setField} />}
            {(action === 'set_flag' || action === 'clear_flag') && <KpInput name="flag" placeholder="剧情标志" fields={fields} onChange={setField} />}
            {action === 'handout' && <><GiScrollUnfurled size={16} /><KpInput name="id" placeholder="手书 ID" list="kp-handouts" fields={fields} onChange={setField} /></>}
            {action === 'hp_change' && <><KpInput name="target" placeholder="角色名" fields={fields} onChange={setField} /><KpInput name="delta" placeholder="变化值，如 -3 或 2" fields={fields} onChange={setField} /><KpInput name="reason" placeholder="原因（可选）" fields={fields} onChange={setField} /></>}
            {action === 'start_combat' && <><Swords size={16} /><KpInput name="enemies" placeholder="敌人名称，多个用逗号分隔" list="kp-npcs" fields={fields} onChange={setField} /><KpInput name="trigger" placeholder="开战原因（可选）" fields={fields} onChange={setField} /></>}
            <button onClick={() => void submit()} disabled={!!busy} className="btn-primary inline-flex items-center gap-1 text-xs">
              {(['dice_check', 'opposed_check', 'generic_roll'] as KpAction[]).includes(action) ? <GiRollingDices size={13} /> : <Send size={13} />}
              {busy ? '处理中…' : ACTION_LABELS[action]}
            </button>
          </div>
          {lastActionResult && (
            <div className="mt-2 flex items-start gap-2 border-t pt-2 text-xs" style={{ borderColor: 'var(--color-border)', color: 'var(--color-text-secondary)' }} aria-live="polite">
              <strong className="shrink-0" style={{ color: 'var(--color-text-primary)' }}>最近结果</strong>
              <span className="min-w-0 flex-1 whitespace-pre-wrap">{lastActionResult}</span>
              <button type="button" onClick={() => setLastActionResult('')} className="btn-secondary !p-1" title="清除最近结果"><X size={12} /></button>
            </div>
          )}
        </div>
      )}

      {!collapsed && tab === 'advisor' && (
        <div className="space-y-3">
          <div className="flex flex-wrap gap-2 border-b pb-3" style={{ borderColor: 'var(--color-border)' }}>
            <input value={planFocus} onChange={(event) => setPlanFocus(event.target.value)} placeholder="裁定重点（可选）" className="input min-w-48 flex-1 text-xs" />
            <button onClick={() => void generatePlan()} disabled={!!busy} className="btn-secondary inline-flex items-center gap-1 text-xs">
              <Bot size={13} /> {busy === 'plan' ? '分析中…' : '生成裁定建议'}
            </button>
            <AdvisorHelp
              label="裁定建议"
              text="根据当前场景、玩家行动和可用规则生成仅 KP 可见的裁定参考。生成建议不会改变游戏；只有点击“照此发起”或“执行”后，对应检定或状态变化才会生效。"
            />
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
            <AdvisorHelp
              label="叙事草稿"
              text="根据当前游戏上下文生成仅 KP 可见、可继续编辑的候选旁白。生成草稿不会推进游戏；确认并点击“发布草稿”后，内容才会展示给玩家。"
            />
            {draft && <>
              <textarea value={draft} onChange={(event) => setDraft(event.target.value)} className="input min-h-24 w-full resize-y text-xs" />
              <button onClick={() => void postAction('narration', { content: draft }, '叙事草稿已发布')} className="btn-primary inline-flex items-center gap-1 text-xs"><Send size={13} />发布草稿</button>
            </>}
          </div>

          <div className="flex flex-wrap gap-2">
            <div className="inline-flex items-center gap-1 rounded border p-0.5" style={{ borderColor: 'var(--color-border)' }} role="tablist" aria-label="检索范围">
              {(['module', 'rule'] as const).map((scope) => (
                <button
                  key={scope}
                  type="button"
                  onClick={() => { setLookupScope(scope); setLookupHits([]); setLookupOpen(false) }}
                  className="btn-secondary !border-0 !px-2 !py-1 text-xs"
                  style={lookupScope === scope ? { background: 'var(--color-bg-tertiary)', color: 'var(--color-text-accent)' } : undefined}
                  role="tab"
                  aria-selected={lookupScope === scope}
                >
                  {scope === 'module' ? '模组原文' : '规则书'}
                </button>
              ))}
            </div>
            <input value={lookupQuery} onChange={(event) => setLookupQuery(event.target.value)} placeholder="输入检索关键词" className="input min-w-48 flex-1 text-xs" />
            <button onClick={() => void runLookup()} disabled={!!busy} className="btn-secondary inline-flex items-center gap-1 text-xs"><BookOpen size={13} />{busy === 'lookup' ? '检索中…' : '速查'}</button>
            {!!lookupHits.length && <button type="button" onClick={() => setLookupOpen(true)} className="btn-secondary !px-2 !py-1 text-xs">查看 {lookupHits.length} 条结果</button>}
          </div>
        </div>
      )}

      {!collapsed && tab === 'assets' && (
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

      {!collapsed && tab === 'director' && (
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

      {!collapsed && <>
        <datalist id="kp-scenes">{workspace?.catalogs.scenes.map((item) => <option key={item.id} value={item.id}>{item.name}</option>)}</datalist>
        <datalist id="kp-npcs">{workspace?.catalogs.npcs.map((item) => <option key={item.id} value={item.name}>{item.id}</option>)}</datalist>
        <datalist id="kp-handouts">{workspace?.catalogs.handouts.map((item) => <option key={item.id} value={item.id}>{item.name}</option>)}</datalist>
      </>}

      {sourceOpen && !collapsed && (
        <aside
          className="fixed right-4 top-20 z-50 flex max-h-[calc(100vh-5rem)] max-w-[calc(100vw-2rem)] flex-col overflow-hidden rounded-md border shadow-xl backdrop-blur-md"
          style={{
            background: 'color-mix(in srgb, var(--color-bg-secondary) 88%, transparent)',
            borderColor: 'var(--color-border-strong)',
            width: `${sourceSize.width}px`,
            height: `${sourceSize.height}px`,
            transform: `translate3d(${sourcePosition.x}px, ${sourcePosition.y}px, 0)`,
          }}
          aria-label="KP 模组资料"
        >
          <div
            className="flex cursor-move touch-none items-center gap-2 border-b px-3 py-2"
            style={{ borderColor: 'var(--color-border)' }}
            onPointerDown={onSourceDragStart}
            onPointerMove={onSourceDragMove}
            onPointerUp={onSourceDragEnd}
            onPointerCancel={onSourceDragEnd}
            title="拖动资料窗"
          >
            <Move size={14} style={{ color: 'var(--color-text-secondary)' }} />
            <BookOpen size={15} style={{ color: 'var(--color-text-accent)' }} />
            <span className="min-w-0 flex-1 truncate text-sm font-semibold">{source?.title || '模组资料'}</span>
            <button type="button" onClick={() => void openModuleSource(true)} disabled={busy === 'source'} className="btn-secondary !p-1.5" title="刷新模组资料">
              <RefreshCw size={13} className={busy === 'source' ? 'animate-spin' : ''} />
            </button>
            <button type="button" onClick={() => setSourceOpen(false)} className="btn-secondary !p-1.5" title="关闭模组资料">
              <X size={15} />
            </button>
          </div>
          <div className="flex items-center gap-1 border-b px-3 py-2" style={{ borderColor: 'var(--color-border)' }} role="tablist" aria-label="资料视图">
            <button type="button" onClick={() => setSourceView('raw')} className="btn-secondary !px-2 !py-1 text-xs" style={sourceView === 'raw' ? { color: 'var(--color-text-accent)' } : undefined} role="tab" aria-selected={sourceView === 'raw'}>原文</button>
            <button type="button" onClick={() => setSourceView('parsed')} className="btn-secondary !px-2 !py-1 text-xs" style={sourceView === 'parsed' ? { color: 'var(--color-text-accent)' } : undefined} role="tab" aria-selected={sourceView === 'parsed'}>解析内容</button>
            <span className="ml-auto text-[11px]" style={{ color: 'var(--color-text-secondary)' }}>仅 KP 可见</span>
          </div>
          <div className="flex items-center gap-2 border-b px-3 py-2" style={{ borderColor: 'var(--color-border)' }}>
            <Search size={14} style={{ color: 'var(--color-text-secondary)' }} />
            <input
              value={sourceQuery}
              onChange={(event) => { setSourceQuery(event.target.value); setSourceTargetOrdinal(null) }}
              placeholder="搜索原文或解析内容"
              className="input min-w-0 flex-1 text-xs"
              aria-label="搜索模组资料"
            />
            {sourceQuery && <button type="button" onClick={() => { setSourceQuery(''); setSourceTargetOrdinal(null) }} className="btn-secondary !p-1.5" title="清除搜索"><X size={13} /></button>}
            <span className="shrink-0 text-[11px]" style={{ color: 'var(--color-text-secondary)' }}>
              {sourceView === 'parsed' && sourceChunks.length ? `${visibleSourceChunks.length}/${sourceChunks.length} 段` : '全文'}
            </span>
          </div>
          <div className="min-h-0 flex-1 overflow-y-auto p-3">
            {!source ? (
              <div className="py-12 text-center text-sm" style={{ color: 'var(--color-text-secondary)' }}>正在加载模组资料…</div>
            ) : sourceView === 'raw' ? (
              <pre className="whitespace-pre-wrap text-xs leading-6" style={{ color: 'var(--color-text-primary)' }}>{highlightText(source.raw_content || source.description || '该模组没有保存原文。', sourceQuery)}</pre>
            ) : (
              <div className="space-y-3">
                {sourceChunks.length > 0 && (
                  <section className="border-b pb-3" style={{ borderColor: 'var(--color-border)' }}>
                    <div className="mb-1 flex items-center gap-2">
                      <h4 className="min-w-0 flex-1 text-xs font-semibold" style={{ color: 'var(--color-text-accent)' }}>原文分段</h4>
                      <span className="text-[11px]" style={{ color: 'var(--color-text-secondary)' }}>按检索结果定位</span>
                    </div>
                    {visibleSourceChunks.length ? visibleSourceChunks.map((chunk) => (
                      <div
                        key={chunk.ordinal}
                        ref={(element) => { sourceChunkRefs.current[chunk.ordinal] = element }}
                        className="mb-2 rounded border p-2 last:mb-0"
                        style={{
                          borderColor: chunk.ordinal === sourceTargetOrdinal ? 'var(--color-accent)' : 'var(--color-border)',
                          background: chunk.ordinal === sourceTargetOrdinal ? 'color-mix(in srgb, var(--color-accent) 12%, transparent)' : 'transparent',
                        }}
                      >
                        <div className="mb-1 text-[11px]" style={{ color: 'var(--color-text-secondary)' }}>第 {chunk.ordinal + 1} 段{chunk.scene_hint ? ` · ${sceneLabel(chunk.scene_hint)}` : ''}</div>
                        <pre className="whitespace-pre-wrap text-xs leading-6" style={{ color: 'var(--color-text-primary)' }}>{highlightText(chunk.text, sourceQuery)}</pre>
                      </div>
                    )) : <div className="py-4 text-center text-xs" style={{ color: 'var(--color-text-secondary)' }}>没有匹配的原文分段。</div>}
                  </section>
                )}
                {sourceSections.map(([label, value]) => {
                  const text = typeof value === 'string' ? value : JSON.stringify(value ?? {}, null, 2)
                  if (!text || text === '{}' || text === '[]') return null
                  if (normalizedSourceQuery && !text.toLocaleLowerCase().includes(normalizedSourceQuery)) return null
                  return (
                    <section key={label} className="border-b pb-3" style={{ borderColor: 'var(--color-border)' }}>
                      <div className="mb-1 flex items-center gap-2">
                        <h4 className="min-w-0 flex-1 text-xs font-semibold" style={{ color: 'var(--color-text-accent)' }}>{label}</h4>
                        <button
                          type="button"
                          onClick={() => {
                            setImagePrompt(text.slice(0, 1500))
                            setTab('assets')
                            setSourceOpen(false)
                          }}
                          className="btn-secondary inline-flex items-center gap-1 !px-1.5 !py-0.5 text-[11px]"
                          title="将该段内容带入配图提示"
                        >
                          <Image size={11} /> 用于配图
                        </button>
                      </div>
                      <pre className="whitespace-pre-wrap text-xs leading-6" style={{ color: 'var(--color-text-primary)' }}>{highlightText(text, sourceQuery)}</pre>
                    </section>
                  )
                })}
              </div>
            )}
          </div>
          <div
            className="absolute bottom-1 right-1 flex h-5 w-5 cursor-se-resize touch-none items-end justify-end"
            onPointerDown={onSourceResizeStart}
            onPointerMove={onSourceResizeMove}
            onPointerUp={onSourceResizeEnd}
            onPointerCancel={onSourceResizeEnd}
            title="调整资料窗大小"
            aria-label="调整资料窗大小"
          >
            <Maximize2 size={13} style={{ color: 'var(--color-text-secondary)' }} />
          </div>
        </aside>
      )}

      {lookupOpen && !collapsed && (
        <div
          className="fixed inset-0 z-50 flex items-start justify-center bg-black/45 p-4 pt-16"
          role="dialog"
          aria-modal="true"
          aria-label={`${lookupScope === 'module' ? '模组原文' : '规则书'}检索结果`}
          onMouseDown={(event) => { if (event.target === event.currentTarget) setLookupOpen(false) }}
        >
          <div className="w-full max-w-3xl overflow-hidden rounded-md" style={{ background: 'var(--color-bg-secondary)', border: '1px solid var(--color-border-strong)', boxShadow: '0 6px 18px var(--shadow-color-strong)' }}>
            <div className="flex items-center gap-2 border-b px-4 py-3" style={{ borderColor: 'var(--color-border)' }}>
              <BookOpen size={16} style={{ color: 'var(--color-text-accent)' }} />
              <div className="min-w-0 flex-1 text-sm font-semibold">{lookupScope === 'module' ? '模组原文' : '规则书'}检索结果</div>
              <span className="text-xs" style={{ color: 'var(--color-text-secondary)' }}>{lookupHits.length} 条</span>
              <button type="button" onClick={() => setLookupOpen(false)} className="btn-secondary !p-1.5" title="关闭检索结果"><X size={15} /></button>
            </div>
            <div className="max-h-[70vh] space-y-3 overflow-y-auto p-4">
              {lookupHits.length ? lookupHits.map((hit, index) => (
                <article key={`${hit.page || hit.scene_hint || hit.ordinal || ''}-${index}`} className="border-l-2 pl-3 text-xs" style={{ borderColor: 'var(--color-border-strong)', color: 'var(--color-text-secondary)' }}>
                  <div className="mb-1 flex items-center gap-2 font-semibold" style={{ color: 'var(--color-text-primary)' }}>
                    {lookupScope === 'rule' ? (hit.page ? `规则书第 ${hit.page} 页` : '规则书片段') : sceneLabel(hit.scene_hint)}
                    {typeof hit.score === 'number' && <span className="ml-2 font-normal opacity-70">相关度 {hit.score.toFixed(3)}</span>}
                    {lookupScope === 'module' && <button type="button" onClick={() => void openModuleAtHit(hit)} className="btn-secondary ml-auto inline-flex shrink-0 items-center gap-1 !px-1.5 !py-0.5 text-[11px]" title="在模组资料窗定位"><Crosshair size={11} />定位到资料</button>}
                  </div>
                  <div className="whitespace-pre-wrap leading-6">{hit.text}</div>
                </article>
              )) : (
                <div className="py-10 text-center text-sm" style={{ color: 'var(--color-text-secondary)' }}>
                  暂无匹配结果。{lookupScope === 'rule' && '规则书检索需要先在规则书管理中完成索引。'}
                </div>
              )}
            </div>
          </div>
        </div>
      )}
    </section>
  )
}
