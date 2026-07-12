import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { CharacterList } from './CharacterList'
import type { Character } from './api'

const characters: Character[] = Array.from({ length: 9 }, (_, index) => ({
  id: `character-${index + 1}`,
  name: index === 0 ? '林记者' : `调查员 ${index + 1}`,
  module_id: 'module-1',
  rule_system: index === 1 ? 'dnd' : 'coc',
  base_attributes: { STR: 50 },
  skills: {},
  system_data: { occupation: index === 0 ? '记者' : '侦探' },
  backstory: '',
  status: 'active',
}))

describe('CharacterList', () => {
  const onSelect = vi.fn()
  const onEdit = vi.fn()
  const onDelete = vi.fn()

  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('按姓名、职业和规则搜索', async () => {
    const user = userEvent.setup()
    render(
      <CharacterList
        characters={characters}
        selectedId={null}
        onSelect={onSelect}
        onEdit={onEdit}
        onDelete={onDelete}
      />,
    )

    const search = screen.getByPlaceholderText('搜索角色名 / 职业 / 规则…')
    await user.type(search, '记者')
    expect(screen.getByRole('heading', { name: '林记者' })).toBeInTheDocument()
    expect(screen.queryByRole('heading', { name: '调查员 2' })).not.toBeInTheDocument()

    await user.clear(search)
    await user.type(search, 'dnd')
    expect(screen.getByRole('heading', { name: '调查员 2' })).toBeInTheDocument()
    expect(screen.queryByRole('heading', { name: '林记者' })).not.toBeInTheDocument()
  })

  it('按八条一页分页并夹取边界', async () => {
    const user = userEvent.setup()
    render(
      <CharacterList
        characters={characters}
        selectedId={null}
        onSelect={onSelect}
        onEdit={onEdit}
        onDelete={onDelete}
      />,
    )

    expect(screen.queryByRole('heading', { name: '调查员 9' })).not.toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: '下一页' }))
    expect(screen.getByRole('heading', { name: '调查员 9' })).toBeInTheDocument()
    expect(screen.getByText('2 / 2')).toBeInTheDocument()
  })

  it('选择、编辑和删除命令互不冒泡', async () => {
    const user = userEvent.setup()
    render(
      <CharacterList
        characters={[characters[0]]}
        selectedId={null}
        onSelect={onSelect}
        onEdit={onEdit}
        onDelete={onDelete}
      />,
    )

    await user.click(screen.getByRole('heading', { name: '林记者' }))
    expect(onSelect).toHaveBeenCalledWith(characters[0])

    vi.clearAllMocks()
    await user.click(screen.getByRole('button', { name: '编辑' }))
    expect(onEdit).toHaveBeenCalledWith(characters[0])
    expect(onSelect).not.toHaveBeenCalled()

    await user.click(screen.getByRole('button', { name: '删除' }))
    expect(onSelect).not.toHaveBeenCalled()
    const deleteButtons = screen.getAllByRole('button', { name: '删除' })
    await user.click(deleteButtons[deleteButtons.length - 1])
    expect(onDelete).toHaveBeenCalledWith(characters[0].id)
  })
})
