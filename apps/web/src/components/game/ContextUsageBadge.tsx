import { useCallback, useEffect, useState } from 'react'
import { GiBrain, GiTwoCoins } from 'react-icons/gi'
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
 * 头部两枚小徽标：
 * ①「上下文」——本回合送入 KP 的输入体量（占用，实测优先）。刻意**不显示占窗口百分比、不搞
 *   红黄溢出警戒**——上下文由组装预算主动裁剪、永远不会撑爆窗口，占比只会绕着远低于窗口的天花板
 *   来回抖，是误导性隐喻；且早期剧情被压成摘要时它会主动回落，故随局面**起伏而非累增**。
 * ②「累计」——本局累计 token 消耗（session_usage.total_tokens），含每一次 LLM 调用，
 *   **单调累增**，对应真实 API 花费的趋势。两者是「占用 vs 花费」两个维度，别混为一谈。
 *
 * 占用徽标：真正影响体验、也随局面单调变化的是「KP 还逐字记得多少剧情」，放进悬停详情；早期剧情
 * 大量压缩时徽标转强调色作轻提示（KP 对早期细节记得没那么细，并非危险）。
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

  const measured = est.source === 'measured'
  const tokens = measured ? (est.measured_input_tokens || 0) : est.input_tokens
  const su = est.session_usage
  const b = est.breakdown
  const ev = est.events
  const verbatim = Math.max(ev.total - ev.summarized, 0)
  // 早期剧情已过半被折叠进摘要 → 轻提示 KP 对早期细节记忆精度下降（非危险，仅信息）。
  const heavilyCompressed = ev.total > 0 && ev.summarized / ev.total >= 0.5
  const color = heavilyCompressed ? 'var(--color-accent)' : 'var(--color-text-secondary)'

  const memoryLine =
    ev.total === 0
      ? 'KP 记忆：开局，尚无历史事件'
      : ev.summarized === 0
        ? `KP 记忆：全部 ${ev.total} 段事件逐字在场，细节完整`
        : `KP 记忆：近 ${verbatim} 段逐字在场，早期 ${ev.summarized} 段已压缩为摘要`

  const title = [
    `模型 ${est.model}（窗口 ${fmt(est.context_window)} token）`,
    measured
      ? `本回合实测输入 ${fmt(tokens)} token（服务端真实分词）`
      : `本回合预估输入约 ${fmt(tokens)} token（启发式；本回合结束后转为实测）`,
    `组装预算上限 ${fmt(est.context_budget)}（按模型窗口自适应），另预留输出 ${fmt(est.output_reserve)}`,
    '',
    '构成（估算参考）：',
    `· 系统提示/模组/记忆：${fmt(b.system)}`,
    `· 剧情摘要：${fmt(b.summary)}`,
    `· 近期逐条事件：${fmt(b.history)}`,
    '',
    memoryLine,
    '说明：上下文由组装预算主动裁剪，不会溢出模型窗口——越往后越多早期剧情被压成摘要，'
      + '此处体量随之在预算内起伏（这是正常的，不代表快满）。',
    measured ? '' : '（尚无实测；分项估算未计入按需检索的规则/原文摘录，实测口径已含一切。）',
  ].filter((l) => l !== '').join('\n')

  const cumTitle = [
    `本局累计 token 消耗：${fmt(su.total_tokens)}`,
    `· 输入 ${fmt(su.prompt_tokens)} + 输出 ${fmt(su.completion_tokens)}`,
    `· 共 ${su.calls} 次 LLM 调用（含 planner/主叙事/校验/队友/子代理/战斗等）`,
    '',
    '随游戏推进单调累增，对应本局真实 API 花费的趋势——',
    '与左侧「上下文占用」是两码事：占用是每回合送入的体量，会随摘要压缩起伏。',
  ].join('\n')

  return (
    <>
      <span
        className="text-xs px-2 py-0.5 rounded border inline-flex items-center gap-1 flex-shrink-0 whitespace-nowrap cursor-default"
        style={{ borderColor: 'var(--color-border)', color }}
        title={title}
      >
        <GiBrain size={13} /> 上下文 {measured ? '' : '~'}{fmt(tokens)}
      </span>
      <span
        className="text-xs px-2 py-0.5 rounded border inline-flex items-center gap-1 flex-shrink-0 whitespace-nowrap cursor-default"
        style={{ borderColor: 'var(--color-border)', color: 'var(--color-text-secondary)' }}
        title={cumTitle}
      >
        <GiTwoCoins size={13} /> 累计 {fmt(su.total_tokens)}
      </span>
    </>
  )
}
