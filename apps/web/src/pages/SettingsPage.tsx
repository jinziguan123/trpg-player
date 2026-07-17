import { useCallback, useEffect, useState, type CSSProperties } from 'react'
import { toast } from 'sonner'
import { api } from '../api/client'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select'
import { THEMES, getTheme, setTheme, type Theme } from '@/lib/theme'
import { useLocation, useNavigate } from 'react-router-dom'
import { getOnboardingReturnTo } from '@/features/onboarding/navigation'

/* ---------- 类型定义 ---------- */

interface AIProfile {
  id: string
  name: string
  protocol: 'openai' | 'anthropic'
  base_url: string
  model_name: string
  api_key: string
  is_active: boolean
  vision?: boolean
  context_window?: number
  reasoning_effort?: string
  image_model?: string
  image_base_url?: string
  image_api_key?: string
}

interface TestResult {
  success: boolean
  message: string
  latency_ms: number
}

type FormData = {
  name: string
  protocol: 'openai' | 'anthropic'
  base_url: string
  model_name: string
  api_key: string
  vision: boolean
  context_window: number
  reasoning_effort: string
  image_model: string
  image_base_url: string
  image_api_key: string
}

const EMPTY_FORM: FormData = {
  name: '',
  protocol: 'openai',
  base_url: '',
  model_name: '',
  api_key: '',
  vision: false,
  context_window: 0,
  reasoning_effort: '',
  image_model: '',
  image_base_url: '',
  image_api_key: '',
}

const PROTOCOL_INFO: Record<
  string,
  { urlPlaceholder: string; modelPlaceholder: string }
> = {
  openai: {
    urlPlaceholder: 'https://api.deepseek.com',
    modelPlaceholder: 'deepseek-chat',
  },
  anthropic: {
    urlPlaceholder: 'https://api.anthropic.com',
    modelPlaceholder: 'claude-sonnet-4-20250514',
  },
}

/* ---------- 二级导航项 ---------- */

const SETTINGS_TABS = [
  { key: 'ai', label: 'AI 配置' },
  { key: 'appearance', label: '外观' },
  { key: 'rag', label: 'RAG 统计' },
  // 未来扩展：{ key: 'game', label: '游戏设置' },
] as const

type SettingsTab = (typeof SETTINGS_TABS)[number]['key']

/* ---------- 组件 ---------- */

export function SettingsPage() {
  const [activeTab, setActiveTab] = useState<SettingsTab>('ai')
  const location = useLocation()
  const navigate = useNavigate()
  const returnTo = getOnboardingReturnTo(location.state)

  return (
    <div style={{ display: 'flex', gap: 0, height: '100%', minHeight: 0 }}>
      {/* 左侧二级导航 */}
      <nav
        style={{
          width: '10rem',
          flexShrink: 0,
          borderRight: '1px solid var(--color-border)',
          paddingTop: '1rem',
          background: 'var(--color-bg-secondary)',
        }}
      >
        <div
          style={{
            padding: '0 0.75rem 0.75rem',
            fontFamily: 'var(--font-title)',
            fontSize: '0.8rem',
            fontWeight: 600,
            color: 'var(--color-text-secondary)',
            letterSpacing: '0.05em',
            textTransform: 'uppercase',
          }}
        >
          设置
        </div>
        {SETTINGS_TABS.map((tab) => (
          <button
            key={tab.key}
            onClick={() => setActiveTab(tab.key)}
            style={{
              display: 'block',
              width: '100%',
              textAlign: 'left',
              padding: '0.5rem 0.75rem',
              margin: '0 0 2px 0',
              border: 'none',
              borderRadius: '3px',
              fontSize: '0.875rem',
              fontFamily: 'var(--font-ui)',
              cursor: 'pointer',
              transition: 'all 0.15s',
              background:
                activeTab === tab.key
                  ? 'rgba(212, 162, 78, 0.12)'
                  : 'transparent',
              color:
                activeTab === tab.key
                  ? 'var(--color-text-accent)'
                  : 'var(--color-text-secondary)',
              fontWeight: activeTab === tab.key ? 600 : 400,
            }}
          >
            {tab.label}
          </button>
        ))}
      </nav>

      {/* 右侧内容区 */}
      <div style={{ flex: 1, padding: '1rem 1.5rem', overflow: 'auto' }}>
        {activeTab === 'ai' && (
          <AISettingsPanel
            onTestSuccess={returnTo ? () => navigate(returnTo, { replace: true }) : undefined}
          />
        )}
        {activeTab === 'appearance' && <AppearanceSettingsPanel />}
        {activeTab === 'rag' && <RagStatsPanel />}
      </div>
    </div>
  )
}

/* ---------- 外观 / 主题面板 ---------- */

function AppearanceSettingsPanel() {
  const [theme, setThemeState] = useState<Theme>(() => getTheme())

  const choose = (t: Theme) => {
    setTheme(t) // 写 localStorage + 改 documentElement.dataset.theme，即时生效
    setThemeState(t)
  }

  return (
    <div>
      <h2 className="page-title">外观</h2>
      <div className="card">
        <h3 className="card-title">主题</h3>
        <p
          className="text-xs"
          style={{ color: 'var(--color-text-secondary)', marginBottom: '0.85rem' }}
        >
          切换即时生效，刷新后保持。
        </p>
        <div style={{ display: 'flex', gap: '0.75rem', flexWrap: 'wrap' }}>
          {THEMES.map((opt) => {
            const active = theme === opt.value
            return (
              <button
                key={opt.value}
                onClick={() => choose(opt.value)}
                style={{
                  display: 'flex',
                  flexDirection: 'column',
                  gap: '0.5rem',
                  padding: '0.75rem',
                  minWidth: '9rem',
                  textAlign: 'left',
                  cursor: 'pointer',
                  borderRadius: '4px',
                  border: `1px solid ${active ? 'var(--color-accent)' : 'var(--color-border-strong)'}`,
                  background: active
                    ? 'rgba(212, 162, 78, 0.08)'
                    : 'var(--color-input-bg)',
                  transition: 'border-color 0.2s',
                }}
              >
                {/* 色板预览 */}
                <div style={{ display: 'flex', gap: '4px' }}>
                  {opt.swatch.map((c) => (
                    <span
                      key={c}
                      style={{
                        width: 22,
                        height: 22,
                        borderRadius: '3px',
                        background: c,
                        border: '1px solid rgba(128,128,128,0.35)',
                      }}
                    />
                  ))}
                </div>
                <span
                  style={{
                    fontFamily: 'var(--font-title)',
                    fontSize: '0.9rem',
                    fontWeight: 600,
                    color: active
                      ? 'var(--color-text-accent)'
                      : 'var(--color-text-primary)',
                  }}
                >
                  {opt.label}
                </span>
                <span
                  style={{
                    fontSize: '0.72rem',
                    color: 'var(--color-text-secondary)',
                  }}
                >
                  {active ? '当前使用' : '点击切换'}
                </span>
              </button>
            )
          })}
        </div>
      </div>
    </div>
  )
}

/* ---------- RAG 统计面板 ---------- */

interface SessionListItem {
  id: string
  module_title: string | null
  character_name: string | null
  status?: string
}

interface RagQuadrant {
  calls: number
  empty: number
  total_hits: number
  hit_rate: number
  avg_top_score: number
}

interface RagSample {
  kind: string
  mode: string
  query: string
  n_hits: number
  top_score: number
}

interface RagStats {
  totals: { calls: number; total_hits: number; empty: number; hit_rate: number }
  by_kind_mode: Record<string, RagQuadrant>
  recent: RagSample[]
}

// 四象限固定顺序与中文标签（kind:mode）
const RAG_QUADRANTS: { key: string; label: string }[] = [
  { key: 'rule:active', label: '规则书 · 主动查阅' },
  { key: 'rule:passive', label: '规则书 · 被动注入' },
  { key: 'module:active', label: '模组原文 · 主动查阅' },
  { key: 'module:passive', label: '模组原文 · 被动注入' },
]

const pct = (x: number) => `${Math.round((x || 0) * 100)}%`
const sessionLabel = (s: SessionListItem) =>
  [s.module_title || '未命名模组', s.character_name || '—'].join(' · ')

function RagStatsPanel() {
  const [sessions, setSessions] = useState<SessionListItem[]>([])
  const [selected, setSelected] = useState<string>('')
  const [stats, setStats] = useState<RagStats | null>(null)
  const [loading, setLoading] = useState(false)

  useEffect(() => {
    api.get<SessionListItem[]>('/sessions')
      .then((list) => {
        setSessions(list)
        if (list.length && !selected) setSelected(list[0].id)
      })
      .catch(() => {})
    // 仅首次拉会话列表
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  const load = useCallback((sid: string) => {
    if (!sid) return
    setLoading(true)
    api.get<RagStats>(`/sessions/${sid}/rag-stats`)
      .then(setStats)
      .catch(() => setStats(null))
      .finally(() => setLoading(false))
  }, [])

  useEffect(() => {
    if (selected) load(selected)
    else setStats(null)
  }, [selected, load])

  const t = stats?.totals
  const empty = !t || t.calls === 0

  return (
    <div>
      <h2 className="page-title">RAG 统计</h2>
      <div className="card">
        <h3 className="card-title">检索用量与命中质量</h3>
        <p className="text-xs" style={{ color: 'var(--color-text-secondary)', marginBottom: '0.85rem' }}>
          按局统计规则书 / 模组原文检索（RAG）的调用次数与命中质量，判断这套检索对跑团的实际帮助。
          <br />
          主动＝KP 发起的查阅；被动＝建上下文时按情境预取。命中率低 / 空命中多，说明语料覆盖或检索组织有待改进。
        </p>

        <div style={{ display: 'flex', gap: '0.5rem', alignItems: 'center', marginBottom: '1rem' }}>
          <div style={{ flex: 1, minWidth: 0 }}>
            <Select value={selected} onValueChange={setSelected}>
              <SelectTrigger className="w-full">
                <SelectValue placeholder={sessions.length ? '选择一局游戏' : '暂无游戏'} />
              </SelectTrigger>
              <SelectContent>
                {sessions.map((s) => (
                  <SelectItem key={s.id} value={s.id}>{sessionLabel(s)}</SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
          <button
            className="btn-secondary text-xs"
            onClick={() => selected && load(selected)}
            disabled={!selected || loading}
            style={{ flexShrink: 0 }}
          >
            {loading ? '刷新中…' : '刷新'}
          </button>
        </div>

        {!selected ? (
          <p className="text-xs" style={{ color: 'var(--color-text-secondary)' }}>请选择一局游戏查看。</p>
        ) : empty ? (
          <p className="text-xs" style={{ color: 'var(--color-text-secondary)' }}>
            本局尚无 RAG 调用记录（未挂规则书/模组原文索引，或还没跑过需要检索的回合）。
          </p>
        ) : (
          <>
            {/* 总计 */}
            <div style={{ display: 'flex', gap: '1.25rem', flexWrap: 'wrap', marginBottom: '1rem' }}>
              <Stat label="总检索" value={String(t!.calls)} />
              <Stat label="命中率" value={pct(t!.hit_rate)} />
              <Stat label="命中片段" value={String(t!.total_hits)} />
              <Stat label="空命中" value={String(t!.empty)} />
            </div>

            {/* 四象限 */}
            <div style={{ overflowX: 'auto' }}>
              <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: '0.8rem' }}>
                <thead>
                  <tr style={{ color: 'var(--color-text-secondary)', textAlign: 'left' }}>
                    <th style={ragTh}>类别</th>
                    <th style={ragTh}>调用</th>
                    <th style={ragTh}>命中率</th>
                    <th style={ragTh}>空命中</th>
                    <th style={ragTh}>平均 top 分</th>
                  </tr>
                </thead>
                <tbody>
                  {RAG_QUADRANTS.map(({ key, label }) => {
                    const q = stats!.by_kind_mode[key]
                    return (
                      <tr key={key} style={{ borderTop: '1px solid var(--color-border)' }}>
                        <td style={ragTd}>{label}</td>
                        <td style={ragTd}>{q?.calls ?? 0}</td>
                        <td style={ragTd}>{q ? pct(q.hit_rate) : '—'}</td>
                        <td style={ragTd}>{q?.empty ?? 0}</td>
                        <td style={ragTd}>{q?.avg_top_score != null ? q.avg_top_score.toFixed(3) : '—'}</td>
                      </tr>
                    )
                  })}
                </tbody>
              </table>
            </div>

            {/* 最近样本 */}
            {stats!.recent.length > 0 && (
              <div style={{ marginTop: '1.25rem' }}>
                <div className="text-xs" style={{ color: 'var(--color-text-secondary)', marginBottom: '0.5rem' }}>
                  最近 {stats!.recent.length} 次检索
                </div>
                <div style={{ display: 'flex', flexDirection: 'column', gap: '0.35rem' }}>
                  {stats!.recent.map((r, i) => (
                    <div
                      key={i}
                      style={{
                        display: 'flex', gap: '0.6rem', alignItems: 'baseline',
                        fontSize: '0.78rem', color: 'var(--color-text-primary)',
                      }}
                    >
                      <span style={{ color: 'var(--color-text-secondary)', flexShrink: 0, width: '9.5rem' }}>
                        {RAG_QUADRANTS.find((x) => x.key === `${r.kind}:${r.mode}`)?.label
                          ?? `${r.kind}:${r.mode}`}
                      </span>
                      <span style={{ flex: 1, minWidth: 0, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                        {r.query || '（空 query）'}
                      </span>
                      <span style={{
                        flexShrink: 0,
                        color: r.n_hits ? 'var(--color-text-secondary)' : 'var(--color-accent)',
                      }}>
                        {r.n_hits ? `${r.n_hits} 命中 · ${r.top_score.toFixed(3)}` : '未命中'}
                      </span>
                    </div>
                  ))}
                </div>
              </div>
            )}
          </>
        )}
      </div>
    </div>
  )
}

const ragTh: CSSProperties = { padding: '0.4rem 0.6rem', fontWeight: 600 }
const ragTd: CSSProperties = { padding: '0.4rem 0.6rem' }

function Stat({ label, value }: { label: string; value: string }) {
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: '0.15rem' }}>
      <span style={{ fontSize: '0.72rem', color: 'var(--color-text-secondary)' }}>{label}</span>
      <span style={{ fontFamily: 'var(--font-title)', fontSize: '1.15rem', color: 'var(--color-text-primary)' }}>
        {value}
      </span>
    </div>
  )
}

/* ---------- AI 配置面板 ---------- */

function AISettingsPanel({ onTestSuccess }: { onTestSuccess?: () => void }) {
  const [profiles, setProfiles] = useState<AIProfile[]>([])
  const [loading, setLoading] = useState(true)
  const [editingId, setEditingId] = useState<string | null>(null) // null=列表模式, 'new'=新建, 其他=编辑
  const [form, setForm] = useState<FormData>(EMPTY_FORM)
  const [saving, setSaving] = useState(false)
  // 哪个配置的哪种测试在进行中（区分「测试连接」与「测试生图」，两按钮才不会一起变「测试中」）
  const [testing, setTesting] = useState<{ id: string; kind: 'conn' | 'image' } | null>(null)

  const fetchProfiles = useCallback(async () => {
    try {
      const data = await api.get<AIProfile[]>('/settings/ai/profiles')
      setProfiles(data)
    } catch {
      toast.error('加载配置列表失败')
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    fetchProfiles()
  }, [fetchProfiles])

  const activeProfile = profiles.find((p) => p.is_active)

  /* 开始新建 */
  const startCreate = () => {
    setEditingId('new')
    setForm(EMPTY_FORM)
  }

  /* 开始编辑 */
  const startEdit = (p: AIProfile) => {
    setEditingId(p.id)
    setForm({
      name: p.name,
      protocol: p.protocol,
      base_url: p.base_url,
      model_name: p.model_name,
      api_key: p.api_key,
      vision: !!p.vision,
      context_window: p.context_window || 0,
      reasoning_effort: p.reasoning_effort || '',
      image_model: p.image_model || '',
      image_base_url: p.image_base_url || '',
      image_api_key: p.image_api_key || '',
    })
  }

  /* 取消编辑 */
  const cancelEdit = () => {
    setEditingId(null)
    setForm(EMPTY_FORM)
  }

  /* 保存（新建或更新） */
  const handleSave = async () => {
    if (!form.name.trim()) {
      toast.error('请输入配置名称')
      return
    }
    setSaving(true)
    try {
      if (editingId === 'new') {
        await api.post('/settings/ai/profiles', form)
        toast.success('配置已创建')
      } else {
        await api.put(`/settings/ai/profiles/${editingId}`, form)
        toast.success('配置已更新')
      }
      setEditingId(null)
      setForm(EMPTY_FORM)
      await fetchProfiles()
    } catch (e) {
      toast.error(`保存失败: ${e instanceof Error ? e.message : '未知错误'}`)
    } finally {
      setSaving(false)
    }
  }

  /* 激活配置 */
  const handleActivate = async (id: string) => {
    try {
      await api.post(`/settings/ai/profiles/${id}/activate`)
      toast.success('已切换激活配置')
      await fetchProfiles()
    } catch {
      toast.error('激活失败')
    }
  }

  /* 删除配置 */
  const handleDelete = async (id: string, name: string) => {
    if (!window.confirm(`确定要删除配置「${name}」吗？`)) return
    try {
      await api.delete(`/settings/ai/profiles/${id}`)
      toast.success('配置已删除')
      if (editingId === id) cancelEdit()
      await fetchProfiles()
    } catch {
      toast.error('删除失败')
    }
  }

  /* 测试连接 */
  const handleTest = async (id: string) => {
    setTesting({ id, kind: 'conn' })
    try {
      const result = await api.post<TestResult>(
        `/settings/ai/profiles/${id}/test`,
      )
      if (result.success) {
        toast.success(`${result.message}（${result.latency_ms}ms）`)
        onTestSuccess?.()
      } else {
        toast.error(`连接失败: ${result.message}`)
      }
    } catch (e) {
      toast.error(`测试出错: ${e instanceof Error ? e.message : '未知错误'}`)
    } finally {
      setTesting(null)
    }
  }

  /* 测试文生图：真打一次 images 端点，判断该配置能否生图 */
  const handleTestImage = async (id: string) => {
    setTesting({ id, kind: 'image' })
    try {
      const result = await api.post<TestResult>(`/settings/ai/profiles/${id}/test-image`)
      if (result.success) toast.success(`${result.message}（${result.latency_ms}ms）`)
      else toast.error(`生图测试失败: ${result.message}`)
    } catch (e) {
      toast.error(`测试出错: ${e instanceof Error ? e.message : '未知错误'}`)
    } finally {
      setTesting(null)
    }
  }

  if (loading) {
    return (
      <p style={{ color: 'var(--color-text-secondary)' }}>加载中...</p>
    )
  }

  const info = PROTOCOL_INFO[form.protocol] || PROTOCOL_INFO.openai

  return (
    <div>
      <h2 className="page-title">AI 配置</h2>

      {/* 当前激活配置状态 */}
      <div
        className="card"
        style={{ marginBottom: '1rem', display: 'flex', alignItems: 'center', gap: '0.75rem' }}
      >
        <span
          style={{
            display: 'inline-block',
            width: 8,
            height: 8,
            borderRadius: '50%',
            background: activeProfile
              ? 'var(--color-success)'
              : 'var(--color-danger)',
            flexShrink: 0,
          }}
        />
        <span style={{ fontSize: '0.875rem' }}>
          {activeProfile ? (
            <>
              当前激活：
              <strong>{activeProfile.name}</strong>
              <span className="badge" style={{ marginLeft: '0.5rem' }}>
                {activeProfile.protocol === 'anthropic' ? 'Anthropic' : 'OpenAI 兼容'}
              </span>
              <span
                style={{
                  marginLeft: '0.5rem',
                  color: 'var(--color-text-secondary)',
                  fontSize: '0.8rem',
                }}
              >
                {activeProfile.model_name}
              </span>
            </>
          ) : (
            <span style={{ color: 'var(--color-text-secondary)' }}>
              暂无激活配置，将使用环境变量默认值
            </span>
          )}
        </span>
      </div>

      {/* 配置列表 */}
      {profiles.length > 0 && (
        <div style={{ display: 'flex', flexDirection: 'column', gap: '0.5rem', marginBottom: '1rem' }}>
          {profiles.map((p) => (
            <div
              key={p.id}
              className="card"
              style={{
                display: 'flex',
                alignItems: 'center',
                gap: '0.75rem',
                padding: '0.75rem 1rem',
                borderColor: p.is_active ? 'var(--color-accent)' : undefined,
              }}
            >
              {/* 配置信息 */}
              <div style={{ flex: 1, minWidth: 0 }}>
                <div style={{ display: 'flex', alignItems: 'center', gap: '0.5rem', marginBottom: '0.2rem' }}>
                  <strong style={{ fontSize: '0.9rem' }}>{p.name}</strong>
                  <span className="badge">
                    {p.protocol === 'anthropic' ? 'Anthropic' : 'OpenAI'}
                  </span>
                  {p.is_active && (
                    <span
                      className="badge"
                      style={{
                        background: 'rgba(45, 90, 30, 0.15)',
                        color: 'var(--color-success)',
                      }}
                    >
                      已激活
                    </span>
                  )}
                </div>
                <div
                  style={{
                    fontSize: '0.8rem',
                    color: 'var(--color-text-secondary)',
                    overflow: 'hidden',
                    textOverflow: 'ellipsis',
                    whiteSpace: 'nowrap',
                  }}
                >
                  {p.model_name}
                  {p.base_url && ` · ${p.base_url}`}
                </div>
              </div>

              {/* 操作按钮 */}
              <div style={{ display: 'flex', gap: '0.35rem', flexShrink: 0 }}>
                {!p.is_active && (
                  <button
                    className="btn-secondary"
                    style={{ padding: '0.25rem 0.6rem', fontSize: '0.8rem' }}
                    onClick={() => handleActivate(p.id)}
                  >
                    激活
                  </button>
                )}
                <button
                  className="btn-secondary"
                  style={{ padding: '0.25rem 0.6rem', fontSize: '0.8rem' }}
                  onClick={() => startEdit(p)}
                  disabled={editingId !== null}
                >
                  编辑
                </button>
                <button
                  className="btn-secondary"
                  style={{ padding: '0.25rem 0.6rem', fontSize: '0.8rem' }}
                  onClick={() => handleTest(p.id)}
                  disabled={testing !== null}
                >
                  {testing?.id === p.id && testing.kind === 'conn' ? '测试中...' : '测试'}
                </button>
                {p.image_model && (
                  <button
                    className="btn-secondary"
                    style={{ padding: '0.25rem 0.6rem', fontSize: '0.8rem' }}
                    onClick={() => handleTestImage(p.id)}
                    disabled={testing !== null}
                    title={`测试文生图（${p.image_model}）`}
                  >
                    {testing?.id === p.id && testing.kind === 'image' ? '测试中...' : '测试生图'}
                  </button>
                )}
                <button
                  className="btn-secondary"
                  style={{
                    padding: '0.25rem 0.6rem',
                    fontSize: '0.8rem',
                    color: 'var(--color-danger)',
                    borderColor: 'var(--color-danger)',
                  }}
                  onClick={() => handleDelete(p.id, p.name)}
                >
                  删除
                </button>
              </div>
            </div>
          ))}
        </div>
      )}

      {/* 新增按钮 / 编辑表单 */}
      {editingId === null ? (
        <button className="btn-primary" onClick={startCreate}>
          + 新增配置
        </button>
      ) : (
        <div className="card">
          <h3 className="card-title">
            {editingId === 'new' ? '新增配置' : '编辑配置'}
          </h3>
          <div style={{ display: 'flex', flexDirection: 'column', gap: '0.85rem' }}>
            {/* 配置名称 */}
            <div>
              <label
                className="block text-sm font-semibold mb-1"
                style={{ fontSize: '0.85rem' }}
              >
                配置名称
              </label>
              <input
                type="text"
                className="input w-full"
                placeholder="例如：DeepSeek 生产环境"
                value={form.name}
                onChange={(e) => setForm({ ...form, name: e.target.value })}
              />
            </div>

            {/* API 协议 */}
            <div>
              <label
                className="block text-sm font-semibold mb-1"
                style={{ fontSize: '0.85rem' }}
              >
                API 协议
              </label>
              <Select
                value={form.protocol}
                onValueChange={(v) =>
                  setForm({ ...form, protocol: v as 'openai' | 'anthropic' })
                }
              >
                <SelectTrigger className="w-full">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="openai">OpenAI 兼容</SelectItem>
                  <SelectItem value="anthropic">Anthropic</SelectItem>
                </SelectContent>
              </Select>
              <p
                className="text-xs mt-1"
                style={{ color: 'var(--color-text-secondary)' }}
              >
                {form.protocol === 'openai'
                  ? '兼容 OpenAI API 格式的服务（DeepSeek、OpenAI、Ollama 等）'
                  : 'Anthropic Claude Messages API'}
              </p>
            </div>

            {/* Base URL */}
            <div>
              <label
                className="block text-sm font-semibold mb-1"
                style={{ fontSize: '0.85rem' }}
              >
                Base URL
              </label>
              <input
                type="text"
                className="input w-full"
                placeholder={info.urlPlaceholder}
                value={form.base_url}
                onChange={(e) =>
                  setForm({ ...form, base_url: e.target.value })
                }
              />
              <p
                className="text-xs mt-1"
                style={{ color: 'var(--color-text-secondary)' }}
              >
                留空则使用默认地址（{info.urlPlaceholder}）
              </p>
            </div>

            {/* 模型名称 */}
            <div>
              <label
                className="block text-sm font-semibold mb-1"
                style={{ fontSize: '0.85rem' }}
              >
                模型名称
              </label>
              <input
                type="text"
                className="input w-full"
                placeholder={info.modelPlaceholder}
                value={form.model_name}
                onChange={(e) =>
                  setForm({ ...form, model_name: e.target.value })
                }
              />
            </div>

            {/* 高级配置（可选，默认收起）：推理档位 / 上下文窗口 / 视觉 */}
            <details
              className="rounded border"
              style={{ borderColor: 'var(--color-border)' }}
            >
              <summary
                className="cursor-pointer select-none text-sm font-semibold px-3 py-2"
                style={{ fontSize: '0.85rem', color: 'var(--color-text-secondary)' }}
              >
                高级配置（可选）
              </summary>
              <div className="px-3 pb-3 flex flex-col gap-4">
                {/* 推理档位 */}
                <div>
                  <label className="block text-sm font-semibold mb-1" style={{ fontSize: '0.85rem' }}>
                    推理档位（reasoning effort）
                  </label>
                  <select
                    className="input w-full"
                    value={form.reasoning_effort}
                    onChange={(e) => setForm({ ...form, reasoning_effort: e.target.value })}
                  >
                    <option value="">默认（不下发，用模型默认档）</option>
                    <option value="minimal">minimal</option>
                    <option value="low">low</option>
                    <option value="medium">medium</option>
                    <option value="high">high</option>
                    <option value="xhigh">xhigh</option>
                  </select>
                  <p className="text-xs mt-1" style={{ color: 'var(--color-text-secondary)' }}>
                    仅对支持推理的 OpenAI 兼容模型生效（如 gpt-5 系）。设定后会一并省略 temperature；
                    非推理模型请留「默认」，否则个别端点会因未知参数报错。
                  </p>
                </div>

                {/* 文生图模型（手书配图） */}
                <div>
                  <label className="block text-sm font-semibold mb-1" style={{ fontSize: '0.85rem' }}>
                    文生图模型（手书配图，可选）
                  </label>
                  <input
                    className="input w-full"
                    placeholder="留空=不生图；OpenAI 填 dall-e-3 或 gpt-image-1"
                    value={form.image_model}
                    onChange={(e) => setForm({ ...form, image_model: e.target.value })}
                  />
                  <p className="text-xs mt-1" style={{ color: 'var(--color-text-secondary)' }}>
                    与聊天模型分开的文生图模型。填好后**保存**，再到下方配置卡点「测试生图」确认能否生成。
                    KP 发手书（信件/报纸/照片等）时会据此配图。
                  </p>
                  {form.image_model && (
                    <div className="mt-2 flex flex-col gap-2">
                      <input
                        className="input w-full"
                        placeholder="生图地址（可选）：留空=复用上面的接口地址；生图另在一处就填这里"
                        value={form.image_base_url}
                        onChange={(e) => setForm({ ...form, image_base_url: e.target.value })}
                      />
                      <input
                        type="password"
                        className="input w-full"
                        placeholder="生图密钥（可选）：留空=复用上面的 API Key"
                        value={form.image_api_key}
                        onChange={(e) => setForm({ ...form, image_api_key: e.target.value })}
                      />
                      <p className="text-xs" style={{ color: 'var(--color-text-secondary)' }}>
                        生图与文本不在同一分组/供应商时，在此单独填地址与密钥；两者留空则复用上面文本模型的。
                      </p>
                    </div>
                  )}
                </div>

                {/* 多模态（视觉） */}
                <div>
                  <label className="flex items-center gap-2 text-sm font-semibold cursor-pointer" style={{ fontSize: '0.85rem' }}>
                    <input type="checkbox" checked={form.vision} onChange={(e) => setForm({ ...form, vision: e.target.checked })} />
                    支持视觉（多模态）
                  </label>
                  <p className="text-xs mt-1" style={{ color: 'var(--color-text-secondary)' }}>
                    勾选后才能用「据图片生成地图 / 图片模组解析」等看图功能。请确保所选模型确实支持视觉（如 GPT-4o / Claude / Gemini / Qwen-VL）。
                  </p>
                </div>

                {/* 上下文窗口 */}
                <div>
                  <label className="block text-sm font-semibold mb-1" style={{ fontSize: '0.85rem' }}>
                    上下文窗口（token）
                  </label>
                  <input
                    type="number"
                    min={0}
                    className="input w-full"
                    placeholder="留空/0：按模型名自动判断（如 deepseek≈64k、claude≈200k）"
                    value={form.context_window || ''}
                    onChange={(e) => setForm({ ...form, context_window: Number(e.target.value) || 0 })}
                  />
                  <p className="text-xs mt-1" style={{ color: 'var(--color-text-secondary)' }}>
                    用于游戏页「上下文占用」预估，判断模型还撑不撑得住继续跑团。填 0 则自动按模型名推断。
                  </p>
                </div>
              </div>
            </details>

            {/* API Key */}
            <div>
              <label
                className="block text-sm font-semibold mb-1"
                style={{ fontSize: '0.85rem' }}
              >
                API Key
              </label>
              <input
                type="password"
                className="input w-full"
                placeholder={
                  form.protocol === 'anthropic' ? 'sk-ant-...' : 'sk-...'
                }
                value={form.api_key}
                onChange={(e) =>
                  setForm({ ...form, api_key: e.target.value })
                }
              />
              <p
                className="text-xs mt-1"
                style={{ color: 'var(--color-text-secondary)' }}
              >
                如果使用本地模型（如 Ollama），可以留空
              </p>
            </div>

            {/* 按钮 */}
            <div style={{ display: 'flex', gap: '0.5rem', marginTop: '0.25rem' }}>
              <button
                className="btn-primary"
                onClick={handleSave}
                disabled={saving}
              >
                {saving ? '保存中...' : '保存'}
              </button>
              <button className="btn-secondary" onClick={cancelEdit}>
                取消
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
