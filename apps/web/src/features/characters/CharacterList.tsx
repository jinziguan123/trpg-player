import { useState } from 'react'
import { UserRound } from 'lucide-react'
import { ConfirmDialog } from '@/components/ui/confirm-dialog'
import type { Character } from './api'

const PAGE_SIZE = 8

const ATTRIBUTE_LABELS: Record<string, string> = {
  STR: '力量',
  CON: '体质',
  SIZ: '体型',
  DEX: '敏捷',
  APP: '外貌',
  INT: '智力',
  POW: '意志',
  EDU: '教育',
}

interface CharacterListProps {
  characters: Character[]
  selectedId: string | null
  onSelect: (character: Character) => void
  onEdit: (character: Character) => void
  onDelete: (characterId: string) => void | Promise<void>
}

export function CharacterList({
  characters,
  selectedId,
  onSelect,
  onEdit,
  onDelete,
}: CharacterListProps) {
  const [query, setQuery] = useState('')
  const [page, setPage] = useState(1)
  const normalizedQuery = query.trim().toLowerCase()
  const filtered = characters.filter((character) => {
    if (!normalizedQuery) return true
    const occupation = String(character.system_data?.occupation ?? '').toLowerCase()
    return character.name.toLowerCase().includes(normalizedQuery)
      || occupation.includes(normalizedQuery)
      || character.rule_system.toLowerCase().includes(normalizedQuery)
  })
  const totalPages = Math.max(1, Math.ceil(filtered.length / PAGE_SIZE))
  const currentPage = Math.min(page, totalPages)
  const pageItems = filtered.slice(
    (currentPage - 1) * PAGE_SIZE,
    currentPage * PAGE_SIZE,
  )

  return (
    <div>
      <div className="mb-3 flex items-center gap-2">
        <input
          value={query}
          onChange={(event) => {
            setQuery(event.target.value)
            setPage(1)
          }}
          placeholder="搜索角色名 / 职业 / 规则…"
          className="input flex-1"
        />
        <span
          className="whitespace-nowrap text-xs"
          style={{ color: 'var(--color-text-secondary)' }}
        >
          {filtered.length} 个角色
        </span>
      </div>

      <div className="space-y-3">
        {pageItems.length === 0 && (
          <p
            className="py-6 text-center text-sm"
            style={{ color: 'var(--color-text-secondary)' }}
          >
            {characters.length === 0
              ? '暂无角色，点右上角「创建角色」开始'
              : '没有匹配的角色'}
          </p>
        )}
        {pageItems.map((character) => {
          const hitPoints = (character.system_data?.hitPoints as {
            current: number
            max: number
          }) || { current: 0, max: 0 }
          const sanity = (character.system_data?.sanity as {
            current: number
            max: number
          }) || { current: 0, max: 0 }
          const occupation = String(character.system_data?.occupation ?? '')
          const isActive = selectedId === character.id
          return (
            <div
              key={character.id}
              className="card cursor-pointer transition-colors"
              style={{ borderColor: isActive ? 'var(--color-accent)' : undefined }}
              onClick={() => onSelect(character)}
              onKeyDown={(event) => {
                if (event.key === 'Enter') onSelect(character)
              }}
              role="button"
              tabIndex={0}
            >
              <div className="mb-2 flex items-center justify-between">
                <h3 className="card-title !mb-0 flex items-center gap-2">
                  <UserRound className="h-4 w-4 opacity-60" aria-hidden="true" />
                  {character.name}
                </h3>
                <div className="flex items-center gap-2">
                  {occupation && <span className="badge">{occupation}</span>}
                  <span className="badge">{character.rule_system.toUpperCase()}</span>
                  <button
                    onClick={(event) => {
                      event.stopPropagation()
                      onEdit(character)
                    }}
                    className="rounded px-1.5 py-0.5 text-xs transition-colors hover:bg-[var(--color-accent)] hover:text-[var(--color-on-accent)]"
                    style={{
                      color: 'var(--color-text-accent)',
                      border: '1px solid var(--color-border)',
                    }}
                  >
                    编辑
                  </button>
                  <ConfirmDialog
                    title="删除角色"
                    description={`确定要删除「${character.name}」吗？此操作不可恢复。`}
                    confirmLabel="删除"
                    onConfirm={() => onDelete(character.id)}
                  >
                    {(open) => (
                      <button
                        onClick={(event) => {
                          event.stopPropagation()
                          open()
                        }}
                        className="rounded px-1.5 py-0.5 text-xs transition-colors hover:bg-[var(--color-danger-deep)] hover:text-white"
                        style={{
                          color: 'var(--color-danger)',
                          border: '1px solid var(--color-danger)',
                        }}
                      >
                        删除
                      </button>
                    )}
                  </ConfirmDialog>
                </div>
              </div>
              <div
                className="flex flex-wrap gap-3 text-sm"
                style={{ color: 'var(--color-text-secondary)' }}
              >
                {Object.entries(character.base_attributes).map(([key, value]) => (
                  <span key={key}>
                    {ATTRIBUTE_LABELS[key] || key}{' '}
                    <strong className="font-mono">{value}</strong>
                  </span>
                ))}
              </div>
              {Boolean(hitPoints.max) && (
                <div
                  className="mt-2 flex gap-4 font-mono text-xs"
                  style={{ color: 'var(--color-text-secondary)' }}
                >
                  <span>HP {hitPoints.current}/{hitPoints.max}</span>
                  <span>SAN {sanity.current}/{sanity.max}</span>
                </div>
              )}
            </div>
          )
        })}
      </div>

      {totalPages > 1 && (
        <div className="mt-4 flex items-center justify-center gap-2 text-sm">
          <button
            onClick={() => setPage((current) => Math.max(1, current - 1))}
            disabled={currentPage <= 1}
            className="btn-secondary !px-2 !py-1 disabled:opacity-40"
          >
            上一页
          </button>
          <span style={{ color: 'var(--color-text-secondary)' }}>
            {currentPage} / {totalPages}
          </span>
          <button
            onClick={() => setPage((current) => Math.min(totalPages, current + 1))}
            disabled={currentPage >= totalPages}
            className="btn-secondary !px-2 !py-1 disabled:opacity-40"
          >
            下一页
          </button>
        </div>
      )}
    </div>
  )
}
