import { GiPadlock } from 'react-icons/gi'
import { Flag, ChevronRight, AlertTriangle, Skull, MapPin } from 'lucide-react'

interface SceneState { when?: string[]; danger?: string; atmosphere?: string; description?: string }
interface NpcState { when?: string[]; personality?: string; initial_location?: string; alive?: boolean }
interface Scene { id: string; name?: string; title?: string; states?: SceneState[] }
interface NPC { id: string; name?: string; states?: NpcState[] }
interface Trigger { id: string; when?: string; set_flags?: string[]; clear_flags?: string[]; description?: string }

const DANGER_LABEL: Record<string, string> = { calm: '平静', uneasy: '不安', dangerous: '危险', deadly: '致命' }
const sceneName = (s: Scene) => s.name || s.title || s.id || '(未命名)'

function summarizeScene(st: SceneState): string {
  const parts: string[] = []
  if (st.danger) parts.push(`危险度→${DANGER_LABEL[st.danger] || st.danger}`)
  if (st.atmosphere) parts.push('氛围变化')
  if (st.description) parts.push('描述变化')
  return parts.join('、') || '状态变化'
}
function summarizeNpc(st: NpcState): { text: string; dead: boolean; moved: boolean } {
  if (st.alive === false) return { text: '死亡', dead: true, moved: false }
  const parts: string[] = []
  if (st.initial_location) parts.push(`转移至 ${st.initial_location}`)
  if (st.personality) parts.push('态度变化')
  return { text: parts.join('、') || '状态变化', dead: false, moved: !!st.initial_location }
}

/** 某些 flag 被置上后，会影响哪些场景/NPC（其变体 when 引用了这些 flag）。 */
function affectedBy(flags: string[], scenes: Scene[], npcs: NPC[]) {
  const set = new Set(flags)
  const items: { kind: 'scene' | 'npc'; name: string; summary: string; dead?: boolean; moved?: boolean }[] = []
  for (const s of scenes) {
    for (const st of s.states || []) {
      if ((st.when || []).some((w) => set.has(w))) {
        items.push({ kind: 'scene', name: sceneName(s), summary: summarizeScene(st) })
      }
    }
  }
  for (const n of npcs) {
    for (const st of n.states || []) {
      if ((st.when || []).some((w) => set.has(w))) {
        const r = summarizeNpc(st)
        items.push({ kind: 'npc', name: n.name || n.id, summary: r.text, dead: r.dead, moved: r.moved })
      }
    }
  }
  return items
}

const accent = 'var(--color-text-accent)'

export function ModuleTimeline({ scenes, npcs, triggers }: { scenes: Scene[]; npcs: NPC[]; triggers: Trigger[] }) {
  // 所有被某个变体引用的 flag
  const referenced = new Set<string>()
  for (const s of scenes) for (const st of s.states || []) for (const w of st.when || []) referenced.add(w)
  for (const n of npcs) for (const st of n.states || []) for (const w of st.when || []) referenced.add(w)
  // 所有被某个触发器置/清的 flag
  const provided = new Set<string>()
  for (const t of triggers) { for (const f of t.set_flags || []) provided.add(f); for (const f of t.clear_flags || []) provided.add(f) }

  const danglingVariants = [...referenced].filter((f) => !provided.has(f))   // 有变体引用、却没触发器置上
  const danglingTriggers = [...provided].filter((f) => !referenced.has(f))   // 触发器置了、却没变体消费

  if (triggers.length === 0 && referenced.size === 0) {
    return (
      <div className="card text-sm" style={{ color: 'var(--color-text-secondary)' }}>
        本模组未定义剧情推进（无触发器、无场景/NPC 变体）——它是一份静态模组，时间线为空。
      </div>
    )
  }

  return (
    <div className="space-y-4">
      <p className="text-xs" style={{ color: 'var(--color-text-secondary)' }}>
        时间线呈现剧情如何推进：每个触发器在条件达成时置/清标志，进而切换相关场景/NPC 的样貌——这正是关系图（空间结构）表达不了的时间维度。
      </p>

      {/* 触发器节点（按作者编排顺序） */}
      <div className="relative pl-6">
        <div className="absolute left-2 top-1 bottom-1 w-px" style={{ background: 'var(--color-border)' }} />
        {triggers.length === 0 && <p className="text-sm" style={{ color: 'var(--color-text-secondary)' }}>（未定义触发器，但存在引用了标志的变体——见下方提示）</p>}
        {triggers.map((t, i) => {
          const flags = [...(t.set_flags || []), ...(t.clear_flags || [])]
          const affected = affectedBy(flags, scenes, npcs)
          return (
            <div key={t.id || i} className="relative mb-4">
              <div className="absolute -left-[18px] top-1 rounded-full p-1" style={{ background: 'var(--color-bg-card)', border: `1px solid ${accent}` }}>
                <Flag size={11} style={{ color: accent }} />
              </div>
              <div className="card !mb-0">
                <div className="text-sm font-semibold mb-1">{t.when || '(未填触发条件)'}</div>
                <div className="flex flex-wrap items-center gap-1.5 mb-2">
                  {(t.set_flags || []).map((f) => (
                    <span key={f} className="badge inline-flex items-center gap-1" style={{ color: accent, borderColor: accent }}><Flag size={10} />{f}</span>
                  ))}
                  {(t.clear_flags || []).map((f) => (
                    <span key={f} className="badge inline-flex items-center gap-1" style={{ color: 'var(--color-text-secondary)', borderColor: 'var(--color-border)', textDecoration: 'line-through' }}><Flag size={10} />{f}</span>
                  ))}
                </div>
                {affected.length > 0 ? (
                  <div className="space-y-1">
                    {affected.map((a, k) => (
                      <div key={k} className="flex items-center gap-1.5 text-xs">
                        <ChevronRight size={12} style={{ color: 'var(--color-text-secondary)' }} />
                        {a.kind === 'npc'
                          ? (a.dead ? <Skull size={12} style={{ color: 'var(--color-danger)' }} /> : a.moved ? <MapPin size={12} style={{ color: accent }} /> : <span style={{ width: 12 }} />)
                          : <span style={{ width: 12 }} />}
                        <span style={{ color: 'var(--color-text-secondary)' }}>{a.kind === 'scene' ? '场景' : 'NPC'}</span>
                        <span className="font-medium">{a.name}</span>
                        <span style={{ color: a.dead ? 'var(--color-danger)' : 'var(--color-text-primary)' }}>{a.summary}</span>
                      </div>
                    ))}
                  </div>
                ) : (
                  <p className="text-xs" style={{ color: 'var(--color-text-secondary)', opacity: 0.6 }}>（暂无场景/NPC 变体消费这些标志）</p>
                )}
              </div>
            </div>
          )
        })}
      </div>

      {/* 一致性提示：标志名是否前后呼应 */}
      {(danglingVariants.length > 0 || danglingTriggers.length > 0) && (
        <div className="card text-xs" style={{ borderColor: '#b8860b' }}>
          <div className="flex items-center gap-1 font-semibold mb-1" style={{ color: '#b8860b' }}>
            <AlertTriangle size={13} /> 标志一致性提示
          </div>
          {danglingVariants.length > 0 && (
            <p style={{ color: 'var(--color-text-secondary)' }} className="flex items-start gap-1">
              <GiPadlock className="mt-0.5 flex-shrink-0" />
              <span>以下标志被场景/NPC 变体引用，却没有任何触发器会置上它（这些变体永不生效）：{danglingVariants.join('、')}</span>
            </p>
          )}
          {danglingTriggers.length > 0 && (
            <p style={{ color: 'var(--color-text-secondary)' }} className="mt-1">
              以下标志被触发器置/清，却没有任何场景/NPC 变体消费它（置了也没效果）：{danglingTriggers.join('、')}
            </p>
          )}
        </div>
      )}
    </div>
  )
}
