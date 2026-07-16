import { useCallback, useEffect, useState } from 'react'
import { GiTwoCoins } from 'react-icons/gi'
import { api } from '../../api/client'

/** 上下文占用预估：分项 token、组装预算与记忆压缩情况。与后端 estimate_session_context 对齐。 */
interface ContextEstimate {
  model: string
  context_window: number
  context_budget: number
  output_reserve: number
  input_tokens: number
  measured_input_tokens: number | null
  source: 'measured' | 'estimated'
  breakdown: { system: number; summary: number; history: number }
  events: { total: number; summarized: number; verbatim_candidates: number }
  usage_ratio: number
  status: 'ok' | 'warn' | 'critical'
  excludes_rag_excerpts: boolean
  session_usage: {
    prompt_tokens: number
    completion_tokens: number
    total_tokens: number
    calls: number
  }
}

function fmt(n: number): string {
  return n >= 1000 ? `${(n / 1000).toFixed(1)}k` : String(n)
}

/**
 * 头部单枚小徽标「累计上下文」——本局累计 token 消耗（session_usage.total_tokens），含每一次
 * LLM 调用，**单调累增**，对应本局真实 API 花费的趋势。
 *
 * （早先并列的「上下文占用」徽标已移除：占用由组装预算主动裁剪、永不撑爆窗口，占比只会绕着远低于
 * 窗口的天花板抖，对玩家无实际决策价值，反而易被误读为「快满了」。）
 *
 * `refreshKey` 变化（本局消息增减）且不在生成中时刷新；生成中不拉（上下文正在变）。
 */
export function ContextUsageBadge({
  sessionId, refreshKey, paused,
}: {
  sessionId: string
  refreshKey: number
  paused: boolean
}) {
  const [est, setEst] = useState<ContextEstimate | null>(null)

  const load = useCallback(() => {
    api.get<ContextEstimate>(`/sessions/${sessionId}/context-estimate`)
      .then(setEst)
      .catch(() => {})
  }, [sessionId])

  useEffect(() => {
    if (paused) return
    const t = setTimeout(load, 400)  // 防抖：一轮多条消息只拉一次
    return () => clearTimeout(t)
  }, [load, refreshKey, paused])

  if (!est) return null

  const su = est.session_usage

  const cumTitle = [
    `本局累计 token 消耗：${fmt(su.total_tokens)}`,
    `· 输入 ${fmt(su.prompt_tokens)} + 输出 ${fmt(su.completion_tokens)}`,
    `· 共 ${su.calls} 次 LLM 调用（含 planner/主叙事/校验/队友/子代理/战斗等）`,
    '',
    '随游戏推进单调累增，对应本局真实 API 花费的趋势。',
  ].join('\n')

  return (
    <span
      className="text-xs px-2 py-0.5 rounded border inline-flex items-center gap-1 flex-shrink-0 whitespace-nowrap cursor-default"
      style={{ borderColor: 'var(--color-border)', color: 'var(--color-text-secondary)' }}
      title={cumTitle}
    >
      <GiTwoCoins size={13} /> 累计上下文 {fmt(su.total_tokens)}
    </span>
  )
}
