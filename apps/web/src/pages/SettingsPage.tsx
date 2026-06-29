import { useCallback, useEffect, useState } from 'react'
import { toast } from 'sonner'
import { api } from '../api/client'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select'

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
}

const EMPTY_FORM: FormData = {
  name: '',
  protocol: 'openai',
  base_url: '',
  model_name: '',
  api_key: '',
  vision: false,
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
  // 未来扩展：{ key: 'game', label: '游戏设置' },
] as const

type SettingsTab = (typeof SETTINGS_TABS)[number]['key']

/* ---------- 组件 ---------- */

export function SettingsPage() {
  const [activeTab, setActiveTab] = useState<SettingsTab>('ai')

  return (
    <div style={{ display: 'flex', gap: 0, height: '100%', minHeight: 0 }}>
      {/* 左侧二级导航 */}
      <nav
        style={{
          width: '10rem',
          flexShrink: 0,
          borderRight: '1px solid var(--color-border)',
          paddingTop: '1rem',
          background: 'rgba(232, 220, 200, 0.3)',
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
              fontFamily: 'var(--font-body)',
              cursor: 'pointer',
              transition: 'all 0.15s',
              background:
                activeTab === tab.key
                  ? 'rgba(139, 37, 0, 0.08)'
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
        {activeTab === 'ai' && <AISettingsPanel />}
      </div>
    </div>
  )
}

/* ---------- AI 配置面板 ---------- */

function AISettingsPanel() {
  const [profiles, setProfiles] = useState<AIProfile[]>([])
  const [loading, setLoading] = useState(true)
  const [editingId, setEditingId] = useState<string | null>(null) // null=列表模式, 'new'=新建, 其他=编辑
  const [form, setForm] = useState<FormData>(EMPTY_FORM)
  const [saving, setSaving] = useState(false)
  const [testingId, setTestingId] = useState<string | null>(null)

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
    setTestingId(id)
    try {
      const result = await api.post<TestResult>(
        `/settings/ai/profiles/${id}/test`,
      )
      if (result.success) {
        toast.success(`${result.message}（${result.latency_ms}ms）`)
      } else {
        toast.error(`连接失败: ${result.message}`)
      }
    } catch (e) {
      toast.error(`测试出错: ${e instanceof Error ? e.message : '未知错误'}`)
    } finally {
      setTestingId(null)
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
                  disabled={testingId !== null}
                >
                  {testingId === p.id ? '测试中...' : '测试'}
                </button>
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
