import { useEffect, useLayoutEffect, useState, useRef, useCallback, useMemo } from 'react'
import { useParams, useNavigate, useLocation } from 'react-router-dom'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { toast } from 'sonner'
import { api, connectSSE } from '../api/client'
import { useSessionStore, type ChatMessage } from '../stores/sessionStore'
import { CharacterPanel } from '../components/character/CharacterPanel'
import { PartyRoster } from '../components/game/PartyRoster'
import { SeatIcon, type SeatKind } from '../components/game/SeatIcon'
import { MapView, type TileMap, type MapEntity } from '../components/module/MapView'
import { useMapAssets } from '../components/module/useMapAssets'
import { GiReturnArrow, GiRollingDices, GiScrollUnfurled, GiTreasureMap, GiPositionMarker } from 'react-icons/gi'
import { Copy, Bot, Map as MapIcon, ChevronUp, RotateCcw, Search, X, PanelRightOpen, PanelRightClose, Pencil, Trash2 } from 'lucide-react'
import { ConfirmDialog } from '../components/ui/confirm-dialog'

interface SceneFloor { name: string; map: TileMap; entities: MapEntity[] }
interface SceneMapPayload { scene_id: string | null; scene_name: string | null; floors: SceneFloor[] }
interface KnownLocation { id: string; name: string; current: boolean; visited: boolean }
interface SearchHit { id: string; sequence_num: number; event_type: string; actor_name: string; content: string }

const CMD_TAG_RE = /\[(DICE_CHECK|NPC_ACT|SCENE_CHANGE|SAY|GROUP|MOVE)[^\]]*\]|\[\/SAY\]/g
const OOC_RE = /（[^（）]*）|\([^()]*\)/g

// KP 偶尔会在叙述里夹带 HTML 标签（如 <b>…</b>）。叙述用 ReactMarkdown 渲染但未开 rehype-raw
// （刻意不渲染 LLM 产出的原始 HTML，防 XSS），故这些标签会原样显示。这里把常见格式化标签剥掉，
// 保留标签内的正文（需要强调时 KP 应改用 markdown，如 **加粗**）。
const HTML_TAG_RE = /<\/?(?:b|i|u|s|em|strong|br|p|span|div|h[1-6]|ul|ol|li|code|pre|blockquote|hr|a)\b[^>]*>/gi

function stripCommandTags(text: string): string {
  return text
    .replace(CMD_TAG_RE, '')
    .replace(HTML_TAG_RE, '')
    .replace(/\n{3,}/g, '\n\n')
    .trim()
}

// 行内 markdown：把加粗/斜体等渲染出来，但 p 退化为 span 以贴合气泡（不换行、不留段距）。
function InlineMd({ text }: { text: string }) {
  return (
    <ReactMarkdown
      remarkPlugins={[remarkGfm]}
      components={{ p: ({ children }) => <>{children}</> }}
    >
      {text}
    </ReactMarkdown>
  )
}

/** 拆出正式行动与 OOC（小括号场外）内容，与后端 split_ooc 对齐。 */
function splitOOC(text: string): { inChar: string; ooc: string } {
  const parts = text.match(OOC_RE) || []
  const inChar = text.replace(OOC_RE, '').trim()
  const ooc = parts.map((p) => p.slice(1, -1).trim()).filter(Boolean).join(' ')
  return { inChar, ooc }
}

function fmtTime(ts?: number): string {
  if (!ts) return ''
  const d = new Date(ts)
  return `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`
}

/** 检定结果按成败取强调色。兼容引擎英文枚举与 SAN 检定的中文。 */
function diceAccent(outcome: string): string {
  const s = String(outcome || '')
  if (s.includes('critical') || s.includes('大成功')) return '#d4af37'        // 大成功：金黄
  if (s.includes('fumble') || s.includes('大失败')) return '#1a1a1a'          // 大失败：黑
  if (s.includes('success') || s === '成功') return 'var(--color-success)'    // 其余成功：绿
  if (s.includes('fail') || s.includes('失败')) return 'var(--color-danger)'  // 普通失败：红
  return 'var(--color-text-secondary)'
}

interface Character {
  id: string
  name: string
  module_id: string
  base_attributes: Record<string, number>
  skills: Record<string, number>
  system_data: Record<string, unknown>
  backstory: string
  status: string
}

interface ChunkPayload {
  type: string
  content?: string
  actor_name?: string
  actor_id?: string
  id?: string
  metadata?: Record<string, unknown>
}

export function GameSessionPage() {
  const { sessionId } = useParams<{ sessionId: string }>()
  const navigate = useNavigate()
  const location = useLocation()
  const isNew = (location.state as { isNew?: boolean })?.isNew
  const {
    currentSession, messages, addMessage, removeMessage, updateMessage, clearMessages,
    setCurrentSession, loadHistory, loadOlderEvents,
    hasMoreHistory, loadingOlder,
    startStreamMessage, appendToStream, endStream,
  } = useSessionStore()

  const [panelChar, setPanelChar] = useState<Character | null>(null)
  const [panelCharId, setPanelCharId] = useState<string | null>(null)
  const [refreshTick, setRefreshTick] = useState(0)
  const [showPanel, setShowPanel] = useState(true)
  const [showMap, setShowMap] = useState(false)
  const [showBigMap, setShowBigMap] = useState(false)         // 大地图（已知地点前往）
  const [locations, setLocations] = useState<KnownLocation[]>([])
  const [confirmTravel, setConfirmTravel] = useState<KnownLocation | null>(null)  // 前往二次确认
  const [splitView, setSplitView] = useState(true)            // 分头行动分栏（检测到多组时生效）
  const [hiddenGroups, setHiddenGroups] = useState<Set<string>>(new Set())  // 被收起的分组
  const [sceneMap, setSceneMap] = useState<SceneMapPayload | null>(null)
  const [floorIdx, setFloorIdx] = useState(0)                 // 多层建筑：当前查看的楼层
  const mapAssets = useMapAssets()

  const primaryId = currentSession?.player_character_id ?? null
  // 多人：我在本房间认领的角色（无则回退到主角，兼容单人）
  const myCharId = currentSession?.participants?.find((p) => p.is_mine)?.character_id ?? primaryId
  const shownCharId = panelCharId ?? myCharId
  const [input, setInput] = useState('')
  const [streaming, setStreaming] = useState(false)
  // 生成已开始但还没吐出第一段内容（推理类模型先思考、此时无 token）→ 显示"KP 思考中"
  const [thinking, setThinking] = useState(false)
  // 历史检索：模糊搜索本局历史 + 跳转到对应消息
  const [showSearch, setShowSearch] = useState(false)
  const [searchQ, setSearchQ] = useState('')
  const [searchResults, setSearchResults] = useState<SearchHit[]>([])
  const searchTimer = useRef<ReturnType<typeof setTimeout> | null>(null)
  // 回合确认制：本回合各真人的确认进度
  const [turnState, setTurnState] = useState<{ confirmed_ids: string[]; total: number; ready: boolean } | null>(null)
  const [editingId, setEditingId] = useState<string | null>(null)
  const [editText, setEditText] = useState('')
  const scrollRef = useRef<HTMLDivElement>(null)
  const pinActive = useRef(false)   // 初次加载「持续钉底」窗口是否进行中（期间抑制平滑滚动，避免抢滚）
  const inputRef = useRef<HTMLTextAreaElement>(null)
  const openingTriggered = useRef(false)
  const composingRef = useRef(false)
  const [typingName, setTypingName] = useState('')
  const typingTimer = useRef<ReturnType<typeof setTimeout> | null>(null)
  const lastTypingSent = useRef(0)
  const myName = currentSession?.participants?.find((p) => p.is_mine)?.character_name ?? null
  const myNameRef = useRef<string | null>(null)
  myNameRef.current = myName
  const [liveConnected, setLiveConnected] = useState(true)

  // 角色名 → 归属（用于消息前的身份图标：我 / AI 队友 / 其他真人 / NPC）
  const partyByName = useMemo(() => {
    const m: Record<string, { isMine: boolean; role: string }> = {}
    for (const p of currentSession?.participants || []) {
      if (p.character_name) m[p.character_name] = { isMine: p.is_mine, role: p.role }
    }
    return m
  }, [currentSession?.participants])
  const actorKind = (name?: string, isPlayer?: boolean): SeatKind => {
    if (isPlayer) return 'me'
    const p = name ? partyByName[name] : undefined
    if (p?.isMine) return 'me'
    if (p?.role === 'ai') return 'ai'
    if (p?.role === 'human') return 'human'
    return 'npc'
  }

  const seenIds = useRef<Set<string>>(new Set())
  const liveTypeRef = useRef<string>('')
  const liveGroupRef = useRef<string>('')   // 当前流式 narration 所属分组（分头行动实时分栏）
  const myCharIdRef = useRef<string | null>(null)
  useEffect(() => { myCharIdRef.current = myCharId }, [myCharId])

  // 从数据库重新对齐历史（替换式），并重建去重集。用于：每次(重)连接、生成结束。
  const resyncHistory = useCallback(async () => {
    if (!sessionId) return
    await loadHistory(sessionId)
    const s = new Set<string>()
    for (const m of useSessionStore.getState().messages) if (m.id) s.add(m.id)
    seenIds.current = s
    liveTypeRef.current = ''
  }, [sessionId, loadHistory])

  // 节流刷新会话（席位/在线变更用）：合并 400ms 内的连续 presence/seat，避免风暴
  const refetchTimer = useRef<ReturnType<typeof setTimeout> | null>(null)
  const refetchSession = useCallback(() => {
    if (!sessionId || refetchTimer.current) return
    refetchTimer.current = setTimeout(() => {
      refetchTimer.current = null
      api.get(`/sessions/${sessionId}`).then((s) => setCurrentSession(s as never)).catch(() => {})
    }, 400)
  }, [sessionId, setCurrentSession])

  // 处理一条房间实时事件（/live）。离散事件按 id 去重；叙述 token 流式拼接。
  const handleLiveChunk = useCallback((chunk: ChunkPayload) => {
    const t = chunk.type
    if (t === 'ready') { setLiveConnected(true); return }
    if (t === 'replay_done') return
    if (t === 'generating') { setStreaming(true); setThinking(true); return }
    if (t === 'turn_state') { setTurnState((chunk.metadata as { confirmed_ids: string[]; total: number; ready: boolean }) || null); return }
    if (t === 'event_delete') { if (chunk.id) removeMessage(chunk.id); return }
    if (t === 'event_update') { if (chunk.id) updateMessage(chunk.id, chunk.content || ''); return }
    if (t === 'done') {
      endStream(); liveTypeRef.current = ''
      setStreaming(false); setThinking(false); setRefreshTick((x) => x + 1)
      setTurnState(null)  // 新回合开始：确认进度归零
      // 生成结束后从 DB 对齐：用持久化的最终叙述替换流式拼接的内容，
      // 同时兜住「刷新落在生成完成瞬间」时丢失的那段叙述。
      void resyncHistory()
      // 同步会话状态：刷新 world_state.pending_checks，使「投骰」按钮按待定检定增减。
      refetchSession()
      return
    }
    if (t === 'seat') {
      // 有人入座：刷新房间席位（更新队伍条与 is_mine），并提示一条系统消息
      refetchSession()
      endStream(); liveTypeRef.current = ''
      addMessage({ id: '', type: 'system', content: chunk.content || '有新成员入座', actor_name: chunk.actor_name })
      return
    }
    if (t === 'presence') {
      // 有人上/下线：刷新席位以更新队伍条上的在线点
      refetchSession()
      return
    }
    if (t === 'typing') {
      if (chunk.actor_name && chunk.actor_name !== myNameRef.current) {
        setTypingName(chunk.actor_name)
        if (typingTimer.current) clearTimeout(typingTimer.current)
        typingTimer.current = setTimeout(() => setTypingName(''), 3000)
      }
      return
    }
    if (t === 'narration') {
      setThinking(false)  // 第一段叙述 token 到达 → 不再是"思考中"
      // 分头行动按组生成时，narration chunk 带 metadata.group；切换分组要另起一条流式消息，
      // 否则多组叙述会被拼进同一条、实时分栏失效（done 后 resync 会再按落库分组对齐）。
      const grp = String((chunk.metadata as Record<string, unknown> | undefined)?.group || '')
      if (liveTypeRef.current !== 'narration' || liveGroupRef.current !== grp) {
        endStream()
        startStreamMessage('narration', 'KP', grp ? { group: grp } : undefined)
        liveTypeRef.current = 'narration'; liveGroupRef.current = grp
      }
      appendToStream(chunk.content || '')
      return
    }
    // 以下为离散事件：按 id 去重（与历史/重连对齐）
    if (chunk.id) {
      if (seenIds.current.has(chunk.id)) return
      seenIds.current.add(chunk.id)
    }
    setThinking(false)  // 任何具体内容（对话/检定/系统）到达 → 不再是"思考中"
    endStream(); liveTypeRef.current = ''
    const isPlayer = !!(myCharIdRef.current && chunk.actor_id === myCharIdRef.current)
    if (t === 'dialogue' || t === 'npc_dialogue') {
      addMessage({ id: chunk.id || '', type: 'dialogue', content: chunk.content || '', actor_name: chunk.actor_name, metadata: { ...(chunk.metadata || {}), is_player: isPlayer } })
    } else if (t === 'action') {
      addMessage({ id: chunk.id || '', type: 'action', content: chunk.content || '', actor_name: chunk.actor_name, metadata: { ...(chunk.metadata || {}), is_player: isPlayer } })
    } else if (t === 'narration_full') {
      addMessage({ id: chunk.id || '', type: 'narration', content: chunk.content || '', actor_name: 'KP' })
    } else if (t === 'dice' || t === 'system' || t === 'ooc') {
      addMessage({ id: chunk.id || '', type: t, content: chunk.content || '', actor_name: chunk.actor_name, metadata: chunk.metadata })
    } else if (t === 'check_request') {
      // 待定检定提示：作为系统消息存（metadata.check_request 携带 check_id），渲染时带「投骰」按钮
      addMessage({ id: chunk.id || '', type: 'system', content: chunk.content || '', actor_name: chunk.actor_name, metadata: { ...(chunk.metadata || {}), is_player: isPlayer } })
    }
  }, [addMessage, removeMessage, updateMessage, appendToStream, endStream, startStreamMessage, resyncHistory, refetchSession])

  useEffect(() => {
    if (!sessionId) return
    const ac = new AbortController()
    let cancelled = false
    seenIds.current = new Set()
    liveTypeRef.current = ''
    const init = async () => {
      clearMessages()
      // 直接拉新鲜会话状态，不信缓存列表——否则刚从大厅开局过来时缓存还是 setup，
      // 会与大厅页的 active 跳转来回弹跳（疯狂刷新 / 参与者被弹回 /game）。
      let session
      try {
        session = await api.get<{ id: string; status: string }>(`/sessions/${sessionId}`)
      } catch {
        navigate('/game', { replace: true }); return
      }
      if (cancelled) return
      if (!session) { navigate('/game', { replace: true }); return }
      if (session.status === 'setup') { navigate(`/room/${sessionId}`, { replace: true }); return }
      setCurrentSession(session as never)

      if (isNew && !openingTriggered.current) {
        openingTriggered.current = true
        setStreaming(true)
        api.post(`/sessions/${sessionId}/opening`).catch(() => {})
      }

      // /live 常驻消费 + 自动重连：连接断开（服务重启 / 网络抖动 / 休眠）后
      // 自动重连并每次从 DB 重新对齐，不再「悄悄停更直到手动刷新」。
      while (!cancelled) {
        try {
          await resyncHistory()
          if (cancelled) break
          const { generating } = await api.get<{ generating: boolean }>(`/sessions/${sessionId}/generating`)
          // 权威同步：每次（重）连按后端真实状态设定，避免重启/抖动后指示器卡在"生成中"不消失
          if (!cancelled) {
            setStreaming(generating)
            if (!generating) setThinking(false)
          }
          for await (const chunk of connectSSE(`/sessions/${sessionId}/live`, ac.signal)) {
            if (cancelled) break
            handleLiveChunk(chunk as ChunkPayload)
          }
        } catch { /* 连接断开或被取消 */ }
        if (cancelled) break
        setLiveConnected(false)  // 断开 → 显示「连接中…」，下次 ready 复位
        await new Promise((r) => setTimeout(r, 1500))  // 重连退避
      }
    }
    init()
    return () => { cancelled = true; ac.abort() }
  }, [sessionId])

  useEffect(() => {
    if (shownCharId) {
      api.get<Character>(`/characters/${shownCharId}`).then(setPanelChar)
    } else {
      setPanelChar(null)
    }
  }, [shownCharId, refreshTick])

  // 场景地图：展开时拉取「我」所在场景的地图+实体位置；场景切换/生成结束(refreshTick)后刷新。
  // 带 char_id → 分头行动时地图跟随我自己所在的场景，而非会话级单一场景。
  useEffect(() => {
    if (!showMap || !sessionId) return
    const q = myCharId ? `?char_id=${myCharId}` : ''
    api.get<SceneMapPayload>(`/sessions/${sessionId}/scene-map${q}`).then(setSceneMap).catch(() => setSceneMap(null))
  }, [showMap, sessionId, myCharId, currentSession?.current_scene_id, refreshTick])

  // 切换到新场景时，楼层回到第一层（入口层）
  useEffect(() => { setFloorIdx(0) }, [sceneMap?.scene_id])

  // 大地图（已知地点）：展开时拉取，前往后/生成结束刷新
  useEffect(() => {
    if (!showBigMap || !sessionId) return
    const q = myCharId ? `?char_id=${myCharId}` : ''
    api.get<{ locations: KnownLocation[] }>(`/sessions/${sessionId}/locations${q}`)
      .then((r) => setLocations(r.locations || [])).catch(() => setLocations([]))
  }, [showBigMap, sessionId, myCharId, currentSession?.current_scene_id, refreshTick])

  const travelTo = async (sceneId: string) => {
    if (!currentSession || streaming) return
    try {
      await api.post(`/sessions/${currentSession.id}/travel`, { scene_id: sceneId, acting_character_id: myCharId })
      setShowBigMap(false)
    } catch { /* 已在该地点 / 不可前往 等，由后端校验 */ }
    finally { setConfirmTravel(null) }
  }

  // 初次加载/刷新落底：此刻 markdown 等内容布局会在随后一段时间里持续膨胀，单次（甚至两帧）
  // 滚动都会朝偏小的 scrollHeight 落在半空。改为在一个有限窗口内「持续钉底」——每帧把主区与各
  // 分栏列都钉到底，直到用户主动滚动或窗口结束，内容再怎么回流也能兜住。用 hasMessages（布尔）
  // 驱动：只在「消息从无到有」时启动一次，加载期间消息增多不会重跑本副作用、也就不会中断钉底。
  const hasMessages = messages.length > 0
  useEffect(() => {
    if (!hasMessages) return
    const el = scrollRef.current
    if (!el) return
    pinActive.current = true
    let raf = 0
    const deadline = performance.now() + 1200
    const pin = () => {
      const e = scrollRef.current
      if (e) {
        e.scrollTop = e.scrollHeight
        e.querySelectorAll<HTMLElement>('[data-scene-col]').forEach((c) => { c.scrollTop = c.scrollHeight })
      }
      if (pinActive.current && performance.now() < deadline) raf = requestAnimationFrame(pin)
      else pinActive.current = false
    }
    raf = requestAnimationFrame(pin)
    const stop = () => { pinActive.current = false }  // 用户一动就停，别打断他往上翻
    el.addEventListener('wheel', stop, { passive: true })
    el.addEventListener('touchmove', stop, { passive: true })
    return () => {
      pinActive.current = false
      if (raf) cancelAnimationFrame(raf)
      el.removeEventListener('wheel', stop)
      el.removeEventListener('touchmove', stop)
    }
  }, [hasMessages])

  // 后续新消息：平滑到底（初次钉底窗口期间交给钉底循环，避免两者抢滚）。
  useEffect(() => {
    if (!hasMessages || pinActive.current) return
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight, behavior: 'smooth' })
  }, [messages.length])

  // 分头行动的每个场景列是各自独立滚动的容器，主页面的「滚到底」管不到它们。初次钉底窗口
  // 期间由上面的钉底循环负责；窗口结束后（如实时新增的分栏内容）在此把各列各自滚到底。
  useLayoutEffect(() => {
    if (!splitView || pinActive.current) return
    const snap = () => scrollRef.current
      ?.querySelectorAll<HTMLElement>('[data-scene-col]')
      .forEach((el) => { el.scrollTop = el.scrollHeight })
    snap()
    requestAnimationFrame(snap)
  }, [messages.length, splitView])

  const handleScroll = useCallback(() => {
    const el = scrollRef.current
    if (!el || !sessionId || loadingOlder || !hasMoreHistory) return
    if (el.scrollTop < 80) {
      const prevHeight = el.scrollHeight
      loadOlderEvents(sessionId).then(() => {
        requestAnimationFrame(() => {
          if (scrollRef.current) {
            scrollRef.current.scrollTop = scrollRef.current.scrollHeight - prevHeight
          }
        })
      })
    }
  }, [sessionId, loadingOlder, hasMoreHistory, loadOlderEvents])

  useEffect(() => {
    const el = scrollRef.current
    if (!el) return
    el.addEventListener('scroll', handleScroll)
    return () => el.removeEventListener('scroll', handleScroll)
  }, [handleScroll])

  // 玩家「申请」检定：只报技能，难度交 KP 裁定（玩家不指定）；intent 是顺带说明的检定目标，
  // 场景里同时有多条线索/多个可疑点时，光报技能名 KP 猜不出玩家具体想查什么。
  const rollCheck = async (skill: string, intent: string) => {
    if (!currentSession || streaming) {
      if (streaming) toast.error('KP 正在叙事，请稍候')
      return
    }
    try {
      setStreaming(true)
      await api.post(`/sessions/${currentSession.id}/check`, {
        skill, intent, acting_character_id: myCharId,
      })
    } catch (e: unknown) {
      setStreaming(false)
      toast.error(e instanceof Error ? e.message : '检定申请失败')
    }
  }

  // 重新生成最新一轮 KP：打断卡住的生成 → 回滚上一轮 KP 叙事 → 用玩家/队友的既有输入重跑
  // （保留已定骰子）。高风险，仅供生成卡住或结果明显有问题时用，经二次确认后才会走到这里。
  const regenerate = async () => {
    if (!currentSession) return
    try {
      setStreaming(true)
      setThinking(true)   // 立即进入「KP 思考中」
      await api.post(`/sessions/${currentSession.id}/regenerate`, {})
      // 后端此时已回滚上一轮 KP 叙事（DB 也已删除），立刻按最新历史对齐——
      // 旧叙事从界面上「消失」，只剩思考中，随后 /live 流式推入重生成的内容。
      await resyncHistory()
    } catch (e: unknown) {
      setStreaming(false)
      setThinking(false)
      toast.error(e instanceof Error ? e.message : '重新生成失败')
    }
  }

  // 删除自己本回合尚未推进的暂存发言。
  const deleteEvent = async (id: string) => {
    if (!currentSession) return
    try {
      await api.delete(`/sessions/${currentSession.id}/events/${id}?acting_character_id=${encodeURIComponent(myCharId ?? '')}`)
    } catch (e: unknown) {
      toast.error(e instanceof Error ? e.message : '删除失败')
    }
  }

  // 保存对暂存发言的改写。
  const saveEdit = async (id: string) => {
    const text = editText.trim()
    if (!currentSession || !text) { setEditingId(null); return }
    try {
      await api.patch(`/sessions/${currentSession.id}/events/${id}`, { content: text, acting_character_id: myCharId })
      setEditingId(null); setEditText('')
    } catch (e: unknown) {
      toast.error(e instanceof Error ? e.message : '修改失败')
    }
  }

  // 回合确认制：点「推进本回合」——记录本人确认；所有真人都确认后由后端整批交 KP。
  const advanceTurn = async () => {
    if (!currentSession || streaming) return
    try {
      await api.post(`/sessions/${currentSession.id}/advance`, { acting_character_id: myCharId })
    } catch (e: unknown) {
      toast.error(e instanceof Error ? e.message : '推进失败')
    }
  }

  // 历史检索：输入防抖后查后端；结果点击可跳转到对应消息。
  const runSearch = (q: string) => {
    setSearchQ(q)
    if (searchTimer.current) clearTimeout(searchTimer.current)
    if (!q.trim() || !currentSession) { setSearchResults([]); return }
    const sid = currentSession.id
    searchTimer.current = setTimeout(async () => {
      try {
        const r = await api.get<{ results: SearchHit[] }>(
          `/sessions/${sid}/search?q=${encodeURIComponent(q.trim())}`,
        )
        setSearchResults(r.results || [])
      } catch { setSearchResults([]) }
    }, 250)
  }

  // 跳转到某条历史：若未加载则不断向前翻页直到出现，再滚动居中并短暂高亮。
  const jumpToEvent = async (eventId: string) => {
    if (!currentSession) return
    setShowSearch(false)
    const find = () => document.querySelector<HTMLElement>(`[data-mid="${eventId}"]`)
    let el = find()
    let guard = 0
    while (!el && useSessionStore.getState().hasMoreHistory && guard < 80) {
      await loadOlderEvents(currentSession.id)
      await new Promise((res) => requestAnimationFrame(() => res(null)))
      el = find()
      guard++
    }
    if (!el) { toast.error('未能定位到该记录'); return }
    el.scrollIntoView({ behavior: 'smooth', block: 'center' })
    el.classList.add('search-hit')
    setTimeout(() => el?.classList.remove('search-hit'), 2200)
  }

  // 玩家点「投骰」：对一个待定检定掷骰。
  const submitRoll = async (checkId: string) => {
    if (!currentSession || streaming) {
      if (streaming) toast.error('KP 正在叙事，请稍候')
      return
    }
    try {
      setStreaming(true)
      await api.post(`/sessions/${currentSession.id}/roll`, { check_id: checkId })
    } catch (e: unknown) {
      setStreaming(false)
      toast.error(e instanceof Error ? e.message : '投骰失败')
    }
  }

  const sendMessage = async () => {
    if (!input.trim() || !currentSession || streaming) return
    const text = input.trim()
    setInput('')
    if (inputRef.current) inputRef.current.style.height = 'auto'

    // fire-and-forget：不做本地乐观回显，自己的消息同样经 /live 广播回来渲染，
    // 保证与其他成员看到的内容/顺序一致。
    const { inChar } = splitOOC(text)
    const body = { content: text, acting_character_id: myCharId }
    try {
      if (!inChar) {
        await api.post(`/sessions/${currentSession.id}/ooc`, body)
      } else {
        // 回合确认制：发言只进入「本回合暂存」（不进 streaming），等点「推进」且所有真人确认后才交 KP。
        await api.post(`/sessions/${currentSession.id}/chat`, body)
      }
    } catch (e: unknown) {
      const msg = e instanceof Error ? e.message : '发送失败'
      toast.error(msg)
    }
  }

  if (!currentSession) {
    return <div className="flex items-center justify-center h-full text-sm" style={{ color: 'var(--color-text-secondary)' }}>加载中...</div>
  }

  // 分头行动：带 metadata.group 的消息属于某个场景列，无组的是共享主线（全宽）。
  // 出现 ≥2 个场景组时提供分栏（各场景并排，主线内容按时间顺序穿插其间）。
  const sceneGroups: string[] = []
  for (const m of messages) {
    const g = String(m.metadata?.group || '').trim()
    if (g && !sceneGroups.includes(g)) sceneGroups.push(g)
  }
  const splitAvailable = sceneGroups.length >= 2
  const toggleGroup = (g: string) => setHiddenGroups((prev) => {
    const next = new Set(prev)
    if (next.has(g)) next.delete(g); else next.add(g)
    return next
  })

  return (
    <div className="flex h-full gap-4">
      <div className="flex flex-col flex-1 min-w-0">
        <div className="flex items-center gap-3 pb-2 mb-2 border-b" style={{ borderColor: 'var(--color-border)' }}>
          <button
            onClick={() => navigate('/game')}
            className="btn-secondary flex items-center gap-1 !px-2 !py-1 text-sm"
          >
            <GiReturnArrow /> 返回列表
          </button>
          <span className="text-sm font-semibold" style={{ color: 'var(--color-text-accent)' }}>
            {currentSession.module_title || '游戏中'}
          </span>
          {currentSession.room_code && (
            <button
              onClick={() => { navigator.clipboard?.writeText(currentSession.room_code || ''); toast.success(`房间码 ${currentSession.room_code} 已复制`) }}
              className="text-xs px-2 py-0.5 rounded border inline-flex items-center gap-1"
              style={{ borderColor: 'var(--color-border)', color: 'var(--color-text-secondary)' }}
              title="点击复制房间码，分享给队友加入"
            >
              房间码 {currentSession.room_code} <Copy size={11} />
            </button>
          )}
          <div className="ml-auto flex items-center gap-2">
            <button
              onClick={() => setShowSearch((v) => !v)}
              className="text-xs btn-secondary !px-2 !py-0.5 flex items-center gap-1"
              title="检索本局历史记录"
            >
              <Search size={12} /> 检索
            </button>
            <button
              onClick={() => { setConfirmTravel(null); setShowBigMap((v) => !v) }}
              className="text-xs btn-secondary !px-2 !py-0.5 flex items-center gap-1"
              title="大地图：前往已知地点"
            >
              <GiTreasureMap size={13} /> {showBigMap ? '收起大地图' : '大地图'}
            </button>
            <button
              onClick={() => setShowMap(!showMap)}
              className="text-xs btn-secondary !px-2 !py-0.5 flex items-center gap-1"
            >
              <MapIcon size={12} /> {showMap ? '收起地图' : '地图'}
            </button>
            {!(showPanel && panelChar) && (
              <button
                onClick={() => setShowPanel(true)}
                className="text-xs btn-secondary !px-2 !py-0.5 flex items-center gap-1"
                title="展开角色卡"
              >
                <PanelRightOpen size={13} />
              </button>
            )}
          </div>
        </div>
        {showSearch && (
          // 历史检索悬浮窗：遮罩 + 居中浮层，点遮罩 / Esc / X 关闭；不占据聊天区布局。
          <div
            className="fixed inset-0 z-50 flex items-start justify-center"
            style={{ paddingTop: '12vh', background: 'rgba(0,0,0,0.35)' }}
            onClick={() => setShowSearch(false)}
          >
            <div
              className="w-full max-w-xl mx-4 rounded-lg overflow-hidden shadow-2xl"
              style={{ background: 'var(--color-bg-card)', border: '1px solid var(--color-border)' }}
              onClick={(e) => e.stopPropagation()}
            >
              <div className="flex items-center gap-2 px-3 py-2 border-b" style={{ borderColor: 'var(--color-border)' }}>
                <Search size={16} style={{ color: 'var(--color-text-secondary)' }} />
                <input
                  autoFocus
                  value={searchQ}
                  onChange={(e) => runSearch(e.target.value)}
                  onKeyDown={(e) => { if (e.key === 'Escape') setShowSearch(false) }}
                  placeholder="模糊检索本局历史（旁白 / 对话 / 行动 / 骰子 / 场外）…"
                  className="input flex-1 !py-1 text-sm"
                />
                <button
                  onClick={() => { setShowSearch(false); setSearchQ(''); setSearchResults([]) }}
                  title="关闭检索（Esc）"
                  style={{ color: 'var(--color-text-secondary)' }}
                >
                  <X size={16} />
                </button>
              </div>
              <div className="max-h-[55vh] overflow-y-auto chat-scroll p-2 flex flex-col gap-1">
                {!searchQ.trim() ? (
                  <div className="text-xs px-2 py-4 text-center" style={{ color: 'var(--color-text-secondary)' }}>
                    输入关键词以检索本局历史记录，点结果可跳转到对应位置
                  </div>
                ) : searchResults.length === 0 ? (
                  <div className="text-xs px-2 py-4 text-center" style={{ color: 'var(--color-text-secondary)' }}>
                    无匹配记录
                  </div>
                ) : searchResults.map((h) => (
                  <button
                    key={h.id}
                    onClick={() => jumpToEvent(h.id)}
                    className="text-left text-xs px-2 py-1.5 rounded hover:opacity-80"
                    style={{ background: 'var(--color-bg-tertiary)', border: '1px solid var(--color-border)' }}
                    title="跳转到该记录"
                  >
                    <span style={{ color: 'var(--color-text-accent)' }}>{h.actor_name || '旁白'}</span>
                    <span className="ml-1" style={{ color: 'var(--color-text-secondary)' }}>{h.content}</span>
                  </button>
                ))}
              </div>
            </div>
          </div>
        )}
        {currentSession.participants && currentSession.participants.length > 1 && (
          <div className="pb-2 mb-2 border-b" style={{ borderColor: 'var(--color-border)' }}>
            <PartyRoster
              participants={currentSession.participants}
              selectedId={shownCharId}
              onSelect={(id) => { setPanelCharId(id); setShowPanel(true) }}
            />
          </div>
        )}
        {showBigMap && (
          <div className="pb-2 mb-2 border-b" style={{ borderColor: 'var(--color-border)' }}>
            <div className="rounded-md p-3" style={{ background: 'var(--color-bg-tertiary)', border: '1px solid var(--color-border)' }}>
              <div className="flex items-center justify-between mb-2">
                <span className="text-xs font-semibold inline-flex items-center gap-1" style={{ color: 'var(--color-text-accent)' }}>
                  <GiTreasureMap size={13} /> 大地图 · 前往已知地点
                </span>
                <button onClick={() => { setConfirmTravel(null); setShowBigMap(false) }} title="收起大地图" style={{ color: 'var(--color-text-secondary)' }}><ChevronUp size={14} /></button>
              </div>
              {locations.length === 0 ? (
                <p className="text-xs" style={{ color: 'var(--color-text-secondary)' }}>暂无已知的可前往地点。</p>
              ) : (
                <div className="flex flex-wrap gap-2">
                  {locations.map((loc) => (
                    <button
                      key={loc.id}
                      disabled={loc.current || streaming}
                      onClick={() => setConfirmTravel(loc)}
                      className="text-xs px-2.5 py-1 rounded border inline-flex items-center gap-1"
                      style={{
                        borderColor: loc.current ? 'var(--color-accent)' : 'var(--color-border)',
                        background: loc.current ? 'var(--color-accent)' : 'transparent',
                        color: loc.current ? '#fff' : 'var(--color-text-primary)',
                        opacity: streaming && !loc.current ? 0.5 : 1,
                        cursor: loc.current || streaming ? 'default' : 'pointer',
                      }}
                      title={loc.current ? '你正在此处' : (loc.visited ? '前往（已探索）' : '前往（新地点）')}
                    >
                      {loc.current ? <GiPositionMarker size={12} /> : <GiReturnArrow size={12} style={{ transform: 'scaleX(-1)' }} />}
                      {loc.name}{loc.current ? '（当前）' : ''}
                    </button>
                  ))}
                </div>
              )}
              {confirmTravel ? (
                <div className="mt-2 rounded-md px-3 py-2 text-xs flex items-center gap-3 flex-wrap"
                  style={{ background: 'var(--color-bg-secondary)', border: '1px solid var(--color-accent)' }}>
                  <span style={{ color: 'var(--color-text-primary)' }}>
                    确定前往「{confirmTravel.name}」？{confirmTravel.visited ? '' : '（你尚未去过此地）'}
                  </span>
                  <div className="flex items-center gap-2 ml-auto">
                    <button onClick={() => travelTo(confirmTravel.id)} disabled={streaming}
                      className="btn-primary !px-2.5 !py-1 inline-flex items-center gap-1"
                      style={streaming ? { opacity: 0.5 } : undefined}>
                      <GiReturnArrow size={12} style={{ transform: 'scaleX(-1)' }} /> 确认前往
                    </button>
                    <button onClick={() => setConfirmTravel(null)}
                      className="btn-secondary !px-2.5 !py-1">取消</button>
                  </div>
                </div>
              ) : (
                <p className="text-[11px] mt-2" style={{ color: 'var(--color-text-secondary)', opacity: 0.7 }}>
                  只显示你已知晓的地点；前往后由 KP 叙述抵达见闻。
                </p>
              )}
            </div>
          </div>
        )}
        {showMap && (() => {
          const floors = sceneMap?.floors || []
          const cur = floors[Math.min(floorIdx, floors.length - 1)] || null
          return (
            <div className="pb-2 mb-2 border-b" style={{ borderColor: 'var(--color-border)' }}>
              {cur ? (
                <div className="rounded-md p-2 overflow-auto" style={{ background: 'var(--color-bg-tertiary)', border: '1px solid var(--color-border)' }}>
                  <div className="flex items-center justify-between mb-1">
                    <span className="text-xs font-semibold inline-flex items-center gap-1" style={{ color: 'var(--color-text-accent)' }}><MapIcon size={12} />{sceneMap?.scene_name || '当前场景'}</span>
                    <button onClick={() => setShowMap(false)} title="收起地图" style={{ color: 'var(--color-text-secondary)' }}><ChevronUp size={14} /></button>
                  </div>
                  {floors.length > 1 && (
                    <div className="flex flex-wrap gap-1 mb-2">
                      {floors.map((f, i) => (
                        <button key={i} onClick={() => setFloorIdx(i)}
                          className="text-xs px-2 py-0.5 rounded border"
                          style={{
                            borderColor: i === floorIdx ? 'var(--color-accent)' : 'var(--color-border)',
                            background: i === floorIdx ? 'var(--color-accent)' : 'transparent',
                            color: i === floorIdx ? '#fff' : 'var(--color-text-secondary)',
                          }}>{f.name || `第 ${i + 1} 层`}</button>
                      ))}
                    </div>
                  )}
                  <MapView map={cur.map} entities={cur.entities} assets={mapAssets} />
                </div>
              ) : (
                <p className="text-xs text-center py-3" style={{ color: 'var(--color-text-secondary)' }}>当前场景暂无地图——可在模组「地图」视图里生成。</p>
              )}
            </div>
          )
        })()}
        {!liveConnected && (
          <div className="text-center text-xs py-1 mb-1 rounded" style={{ color: 'var(--color-text-secondary)', background: 'var(--color-bg-tertiary)' }}>
            与房间连接中断，正在重连…
          </div>
        )}
        {splitAvailable && (
          <div className="flex items-center gap-1.5 px-1 pb-1 mb-1 text-xs flex-wrap" style={{ borderBottom: '1px solid var(--color-border)' }}>
            <span style={{ color: 'var(--color-text-secondary)' }}>分头行动：</span>
            <button
              onClick={() => setSplitView((v) => !v)}
              className="px-2 py-0.5 rounded border"
              style={{ borderColor: 'var(--color-border)', color: 'var(--color-text-accent)' }}
            >{splitView ? '合并为单列' : '分栏显示'}</button>
            {splitView && sceneGroups.map((g) => {
              const on = !hiddenGroups.has(g)
              return (
                <button key={g} onClick={() => toggleGroup(g)} className="px-2 py-0.5 rounded border"
                  style={{
                    borderColor: on ? 'var(--color-accent)' : 'var(--color-border)',
                    background: on ? 'var(--color-accent)' : 'transparent',
                    color: on ? '#fff' : 'var(--color-text-secondary)',
                  }}>{g}</button>
              )
            })}
          </div>
        )}
        <div ref={scrollRef} className="flex-1 overflow-auto pb-4 chat-scroll">
          {loadingOlder && (
            <div className="text-center py-2 text-xs" style={{ color: 'var(--color-text-secondary)' }}>
              加载更早的记录...
            </div>
          )}
          {(() => {
          // 是否可增删改：自己本回合尚未推进的暂存发言（action/dialogue + pending_turn + 本人）。
          const canEditMsg = (m: ChatMessage) =>
            !streaming && !!m.id && (m.type === 'action' || m.type === 'dialogue')
            && !!m.metadata?.pending_turn && m.metadata?.is_player === true
          // 每条消息外包一层带 data-mid 的容器（供检索跳转定位）；自己的暂存发言叠加编辑/删除。
          const renderRow = (msg: ChatMessage) => {
            if (editingId && editingId === msg.id) {
              return (
                <div key={msg.id} data-mid={msg.id} className="px-3 py-2">
                  <textarea
                    value={editText}
                    onChange={(e) => setEditText(e.target.value)}
                    rows={2}
                    autoFocus
                    className="input w-full text-sm"
                    style={{ resize: 'none' }}
                    onKeyDown={(e) => {
                      if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); saveEdit(msg.id!) }
                      if (e.key === 'Escape') { setEditingId(null); setEditText('') }
                    }}
                  />
                  <div className="flex gap-2 mt-1 justify-end">
                    <button onClick={() => { setEditingId(null); setEditText('') }} className="btn-secondary text-xs !px-2 !py-0.5">取消</button>
                    <button onClick={() => saveEdit(msg.id!)} className="text-xs px-3 py-0.5 rounded font-semibold cursor-pointer" style={{ background: 'var(--color-text-accent)', color: '#f0e6d3' }}>保存</button>
                  </div>
                </div>
              )
            }
            return (
              <div key={msg.id || `s${msg.sequence_num ?? ''}`} data-mid={msg.id || undefined} className="msg-row relative">
                {renderOne(msg)}
                {canEditMsg(msg) && (
                  <div className="msg-actions absolute top-1 right-1 flex gap-1">
                    <button
                      onClick={() => { setEditingId(msg.id!); setEditText(msg.content) }}
                      title="编辑这条暂存发言"
                      className="p-1 rounded hover:opacity-80"
                      style={{ background: 'var(--color-bg-tertiary)', border: '1px solid var(--color-border)', color: 'var(--color-text-secondary)' }}
                    >
                      <Pencil size={12} />
                    </button>
                    <button
                      onClick={() => deleteEvent(msg.id!)}
                      title="删除这条暂存发言"
                      className="p-1 rounded hover:opacity-80"
                      style={{ background: 'var(--color-bg-tertiary)', border: '1px solid var(--color-border)', color: 'var(--color-danger)' }}
                    >
                      <Trash2 size={12} />
                    </button>
                  </div>
                )}
              </div>
            )
          }
          const renderOne = (msg: ChatMessage) => {
            const isPlayer = !!msg.metadata?.is_player
            const showLabel = msg.actor_name && (msg.type === 'dialogue' || msg.type === 'action')
            if (msg.type === 'ooc') {
              return (
                <div key={msg.id} className="chat-msg chat-msg--ooc py-1">
                  <span
                    className="text-xs italic"
                    style={{ color: 'var(--color-text-secondary)', opacity: 0.85 }}
                  >
                    （场外·{msg.actor_name || '玩家'}）{msg.content}
                  </span>
                </div>
              )
            }
            if (msg.type === 'dice') {
              // 暗投/暗骰：结果对玩家隐藏 → 用中性灰、不按成败着色
              const blind = !!msg.metadata?.blind
              const accent = blind
                ? 'var(--color-text-secondary)'
                : diceAccent(String(msg.metadata?.outcome ?? ''))
              // 去掉历史数据里可能残留的旧 🎲 前缀，统一用矢量骰子图标
              const diceText = msg.content.replace(/^🎲\s*/, '')
              return (
                <div key={msg.id} className="chat-msg py-1">
                  <div className="rounded-md px-3 py-2 text-sm flex items-start gap-2"
                    style={{ background: 'var(--color-bg-tertiary)', borderLeft: `3px solid ${accent}`, width: 'fit-content', maxWidth: '100%' }}>
                    <GiRollingDices style={{ color: accent, fontSize: '1.1rem', flexShrink: 0, marginTop: '0.1rem' }} />
                    <span className="whitespace-pre-wrap">{diceText}</span>
                    {fmtTime(msg.ts) && <span className="self-end" style={{ fontSize: '0.6rem', opacity: 0.5, flexShrink: 0 }}>{fmtTime(msg.ts)}</span>}
                  </div>
                </div>
              )
            }
            if (msg.type === 'system') {
              // 背景导语卡：开场前展示模组类型/年代/难度等公开元信息 + 一句话前提，给玩家定位
              if (msg.metadata?.kind === 'module_intro') {
                const title = String(msg.metadata?.title || '模组')
                const meta = String(msg.metadata?.meta || '')
                return (
                  <div key={msg.id} className="chat-msg py-2 flex justify-center">
                    <div className="rounded-lg px-4 py-3 max-w-2xl w-full"
                      style={{ background: 'var(--color-bg-tertiary)', border: '1px solid var(--color-border)' }}>
                      <div className="flex items-center gap-2 mb-1" style={{ color: 'var(--color-text-accent)' }}>
                        <GiScrollUnfurled />
                        <span className="font-semibold">{title}</span>
                      </div>
                      {meta && <div className="text-xs mb-2" style={{ color: 'var(--color-text-secondary)' }}>{meta}</div>}
                      {msg.content && <div className="text-sm whitespace-pre-wrap" style={{ color: 'var(--color-text-primary)' }}>{msg.content}</div>}
                    </div>
                  </div>
                )
              }
              // 待定检定提示：携带 check_request 元数据时，渲染成带「投骰」按钮的卡片
              const checkId = msg.metadata?.check_request ? String(msg.metadata?.id ?? '') : ''
              if (checkId) {
                const pending = (currentSession?.world_state as Record<string, unknown> | undefined)?.pending_checks as Record<string, unknown> | undefined
                const stillPending = !!pending && checkId in pending
                const mine = !msg.metadata?.char_id || msg.metadata?.char_id === myCharId
                return (
                  <div key={msg.id} className="chat-msg py-1 flex justify-center">
                    <div className="rounded-md px-3 py-2 text-sm flex items-center gap-3"
                      style={{ background: 'var(--color-bg-tertiary)', borderLeft: '3px solid var(--color-accent)', maxWidth: '100%' }}>
                      <GiRollingDices style={{ color: 'var(--color-accent)', fontSize: '1.1rem', flexShrink: 0 }} />
                      <span className="whitespace-pre-wrap">{msg.content}</span>
                      {stillPending && mine && (
                        <button onClick={() => submitRoll(checkId)} disabled={streaming}
                          className="btn-primary text-xs !px-2.5 !py-1 flex items-center gap-1 flex-shrink-0"
                          style={streaming ? { opacity: 0.5 } : undefined}>
                          <GiRollingDices size={13} /> 投骰
                        </button>
                      )}
                      {!stillPending && <span className="text-xs flex-shrink-0" style={{ color: 'var(--color-text-secondary)', opacity: 0.6 }}>已投骰</span>}
                    </div>
                  </div>
                )
              }
              return (
                <div key={msg.id} className="chat-msg py-1 text-center">
                  <span className="inline-block text-xs px-2.5 py-1 rounded whitespace-pre-wrap"
                    style={{ background: 'var(--color-bg-tertiary)', color: 'var(--color-text-secondary)' }}>
                    {msg.content}
                  </span>
                </div>
              )
            }
            const kind = showLabel ? actorKind(msg.actor_name, isPlayer) : 'npc'
            return (
              <div key={msg.id} className={`chat-msg chat-msg--${msg.type}`}>
                {showLabel && (
                  <div className={`flex items-center gap-1 ${isPlayer ? 'justify-end chat-actor-player' : 'chat-actor'}`}>
                    {kind !== 'npc' && <SeatIcon kind={kind} size={12} />}
                    {msg.actor_name}
                    {fmtTime(msg.ts) && <span style={{ marginLeft: 6, fontSize: '0.6rem', opacity: 0.5 }}>{fmtTime(msg.ts)}</span>}
                  </div>
                )}
                {isPlayer && msg.type === 'dialogue' ? (
                  <div className="chat-player">
                    <span className="chat-bubble-player">{msg.content}</span>
                  </div>
                ) : !isPlayer && msg.type === 'dialogue' ? (
                  <div>
                    <span className="chat-bubble-npc"><InlineMd text={msg.content} /></span>
                  </div>
                ) : msg.type === 'action' ? (
                  <div className={isPlayer ? 'chat-player' : ''}>
                    <span className="chat-bubble-action">{isPlayer ? msg.content : <InlineMd text={msg.content} />}</span>
                  </div>
                ) : msg.type === 'narration' ? (
                  <div className="chat-content markdown-body">
                    <ReactMarkdown remarkPlugins={[remarkGfm]}>
                      {stripCommandTags(msg.content)}
                    </ReactMarkdown>
                  </div>
                ) : (
                  <div className="chat-content">
                    <span className="whitespace-pre-wrap">{msg.content}</span>
                  </div>
                )}
              </div>
            )
          }
          // 分头行动：带 group 的消息进入场景列，无组消息是共享主线（全宽）。
          // 按时间顺序把消息切成「主线段（全宽）」与「分栏段（连续的场景组并排）」，
          // 这样每个场景列＝该场景的「玩家行动 + KP 叙事」自成一体，主线穿插其间保序。
          const sceneOf = (m: ChatMessage) => String(m.metadata?.group || '').trim()
          if (!splitView || sceneGroups.length < 2) {
            return messages.map(renderRow)
          }
          type Seg = { split: boolean; msgs: ChatMessage[] }
          const segments: Seg[] = []
          for (const m of messages) {
            const isSplit = !!sceneOf(m)
            const last = segments[segments.length - 1]
            if (!last || last.split !== isSplit) segments.push({ split: isSplit, msgs: [m] })
            else last.msgs.push(m)
          }
          return segments.map((seg, i) => {
            if (!seg.split) return <div key={`s${i}`}>{seg.msgs.map(renderRow)}</div>
            const labels: string[] = []
            for (const m of seg.msgs) { const g = sceneOf(m); if (!labels.includes(g)) labels.push(g) }
            const shown = labels.filter((g) => !hiddenGroups.has(g))
            return (
              <div key={`c${i}`} className="flex gap-3 overflow-x-auto items-start my-1">
                {shown.map((g) => (
                  // 每个场景列各自独立滚动：长短不一时互不牵连，可单独翻看某一条线
                  <div key={g} data-scene-col className="flex-1 min-w-[280px] overflow-y-auto chat-scroll pr-1"
                    style={{ borderLeft: '2px solid var(--color-border)', paddingLeft: 10, maxHeight: 'calc(100vh - 230px)' }}>
                    <div className="text-xs font-semibold mb-1 sticky top-0 z-10 py-1"
                      style={{ color: 'var(--color-text-accent)', background: 'var(--color-bg-primary)' }}>
                      {g}
                    </div>
                    {seg.msgs.filter((m) => sceneOf(m) === g).map(renderRow)}
                  </div>
                ))}
              </div>
            )
          })
          })()}
          {streaming && (
            <div className="chat-loading flex items-center gap-2">
              <span className="dot-pulse" />
              {thinking && (
                <span className="text-xs italic" style={{ color: 'var(--color-text-secondary)' }}>
                  KP 正在思考……
                </span>
              )}
            </div>
          )}
        </div>

        {typingName && (
          <div className="px-3 pb-1 text-xs italic" style={{ color: 'var(--color-text-secondary)' }}>
            {typingName} 正在输入…
          </div>
        )}
        {!streaming && (
          <div className="px-3 pb-1 flex items-center justify-between gap-2">
            <div className="flex items-center gap-2">
              <button
                onClick={advanceTurn}
                disabled={!!(turnState && myCharId && turnState.confirmed_ids.includes(myCharId))}
                className="text-xs px-3 py-1 rounded font-semibold transition-colors cursor-pointer"
                style={{
                  background: 'var(--color-text-accent)',
                  color: '#f0e6d3',
                  opacity: (turnState && myCharId && turnState.confirmed_ids.includes(myCharId)) ? 0.5 : 1,
                }}
                title="所有真人都点「推进」后，本回合发言才整批交给 KP"
              >
                {turnState && myCharId && turnState.confirmed_ids.includes(myCharId) ? '已确认 · 等待其他人' : '推进本回合'}
              </button>
              {turnState && turnState.total > 0 && (
                <span className="text-xs" style={{ color: 'var(--color-text-secondary)' }}>
                  已确认 {turnState.confirmed_ids.length}/{turnState.total}
                </span>
              )}
            </div>
            {messages.some((m) => m.type === 'narration') && (
              <ConfirmDialog
                title="重新生成最新一轮"
                description="将删除最新一轮 KP 的叙事（旁白与 NPC 台词），用本轮玩家与队友的既有输入重新生成；已投出的骰子结果会保留、不重掷。此操作会打断当前生成、可能明显改变剧情走向——仅在生成卡住或结果明显有问题时使用。"
                confirmLabel="重新生成"
                onConfirm={regenerate}
              >
                {(open) => (
                  <button
                    onClick={open}
                    title="重新生成最新一轮 KP 叙事（高风险，慎用）"
                    className="inline-flex items-center gap-1 text-xs px-2 py-1 rounded transition-colors hover:opacity-80"
                    style={{ color: 'var(--color-text-secondary)' }}
                  >
                    <RotateCcw size={12} /> 重新生成
                  </button>
                )}
              </ConfirmDialog>
            )}
          </div>
        )}
        <div className="chat-input-bar">
          <textarea
            ref={inputRef}
            value={input}
            onChange={(e) => {
              setInput(e.target.value)
              e.target.style.height = 'auto'
              e.target.style.height = Math.min(e.target.scrollHeight, 120) + 'px'
              // 节流上报"正在输入"给同房间其他人
              const now = Date.now()
              if (currentSession && e.target.value && now - lastTypingSent.current > 2000) {
                lastTypingSent.current = now
                api.post(`/sessions/${currentSession.id}/typing`).catch(() => {})
              }
            }}
            onCompositionStart={() => { composingRef.current = true }}
            onCompositionEnd={() => { composingRef.current = false }}
            onKeyDown={(e) => {
              if (composingRef.current || e.nativeEvent.isComposing) return
              if (e.key === 'Enter' && !e.shiftKey) {
                e.preventDefault()
                sendMessage()
              }
            }}
            placeholder={'输入行动；用「」或""括住要说出口的台词，（圆括号）内为场外'}
            disabled={streaming}
            className="input flex-1"
            rows={1}
            style={{ resize: 'none' }}
          />
          <button onClick={sendMessage} disabled={streaming || !input.trim()} className="btn-primary">
            发送
          </button>
        </div>
      </div>

      {showPanel && panelChar && (
        <aside
          className="w-64 flex-shrink-0 border-l overflow-y-auto"
          style={{ borderColor: 'var(--color-border)', background: 'var(--color-bg-card)' }}
        >
          <div
            className="flex items-center justify-between px-3 py-1.5 text-xs border-b"
            style={{ borderColor: 'var(--color-border)', color: 'var(--color-text-secondary)' }}
          >
            <span className="inline-flex items-center gap-1">
              {shownCharId !== myCharId ? (<><Bot size={12} /> 其他角色卡</>) : '角色卡'}
            </span>
            <span className="inline-flex items-center gap-1">
              {shownCharId !== myCharId && (
                <button
                  onClick={() => setPanelCharId(null)}
                  className="btn-secondary !px-2 !py-0.5"
                >
                  看我的角色
                </button>
              )}
              <button
                onClick={() => setShowPanel(false)}
                title="收起角色卡"
                className="p-0.5 rounded hover:opacity-80"
                style={{ color: 'var(--color-text-secondary)' }}
              >
                <PanelRightClose size={15} />
              </button>
            </span>
          </div>
          <CharacterPanel
            character={panelChar}
            onSkillCheck={shownCharId === myCharId ? rollCheck : undefined}
          />
        </aside>
      )}
    </div>
  )
}
