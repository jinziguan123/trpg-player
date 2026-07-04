import { useCallback, useEffect, useState } from 'react'
import { GiBrain } from 'react-icons/gi'
import { api } from '../../api/client'

/** 上下文占用预估：分项 token、模型窗口占比与健康度。与后端 estimate_session_context 对齐。 */
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
}

const STATUS_COLOR: Record<ContextEstimate['status'], string> = {
  ok: 'var(--color-text-secondary)',
  warn: 'var(--color-accent)',
  critical: 'var(--color-danger)',
}

function fmt(n: number): string {
  return n >= 1000 ? `${(n / 1000).toFixed(1)}k` : String(n)
}

/**
 * 头部的上下文占用小徽标：显示「上下文 NN%」，颜色随健康度变化，悬停看分项明细。
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

  const pct = Math.round(est.usage_ratio * 100)
  const color = STATUS_COLOR[est.status]
  const b = est.breakdown
  const measured = est.source === 'measured'
  const headline = measured
    ? `上一回合实测输入 ${fmt(est.measured_input_tokens || 0)} token（服务端真实分词），加输出预留 ${fmt(est.output_reserve)} ≈ 窗口的 ${pct}%`
    : `本回合预估输入约 ${fmt(est.input_tokens)} token（启发式），加输出预留 ${fmt(est.output_reserve)} ≈ 窗口的 ${pct}%`
  const title = [
    `模型 ${est.model}（窗口 ${fmt(est.context_window)} token）`,
    headline,
    '',
    `分项估算（构成参考）：`,
    `· 系统提示/模组/记忆：${fmt(b.system)}`,
    `· 剧情摘要：${fmt(b.summary)}`,
    `· 近期逐条事件：${fmt(b.history)}`,
    '',
    `事件 ${est.events.total} 条：已折叠进摘要 ${est.events.summarized}，可逐条 ${est.events.verbatim_candidates}`,
    est.status === 'critical'
      ? '注意：逼近模型窗口上限，建议换更大窗口的模型或精简。'
      : est.status === 'warn'
        ? '上下文偏紧，注意后续增长。'
        : '上下文充裕。',
    measured ? '' : '（尚无实测：本回合结束后改用服务端真实用量。分项估算未计入按需检索的规则/原文摘录。）',
  ].filter((l) => l !== '').join('\n')

  return (
    <span
      className="text-xs px-2 py-0.5 rounded border inline-flex items-center gap-1 flex-shrink-0 whitespace-nowrap cursor-default"
      style={{ borderColor: 'var(--color-border)', color }}
      title={title}
    >
      <GiBrain size={13} /> 上下文 {measured ? '' : '约'}{pct}%
    </span>
  )
}
