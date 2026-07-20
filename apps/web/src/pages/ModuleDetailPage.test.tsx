import { render, screen } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { api } from '@/api/client'
import { ModuleDetailPage } from './ModuleDetailPage'

vi.mock('@/api/client', () => ({
  api: { get: vi.fn(), post: vi.fn(), put: vi.fn() },
  getServerUrl: () => '',
}))

const mockGet = vi.mocked(api.get)

describe('模组详情图片', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    mockGet.mockResolvedValue({
      id: 'module-1',
      title: '常暗之箱',
      rule_system: 'coc',
      description: '测试模组',
      world_setting: {},
      scenes: [{ id: 'scene-1', name: '六号车厢', image: '/api/images/scene.jpg' }],
      npcs: [{ id: 'npc-1', name: '乘务员', portrait: '/api/images/npc.jpg' }],
      clues: [{ id: 'clue-1', name: '染血车票', image: '/api/images/clue.jpg' }],
      triggers: [],
      truth: '',
    })
  })

  it('查看模组时展示场景、NPC 和线索图片', async () => {
    render(
      <MemoryRouter initialEntries={['/modules/module-1']}>
        <Routes>
          <Route path="/modules/:id" element={<ModuleDetailPage />} />
        </Routes>
      </MemoryRouter>,
    )

    expect(await screen.findByRole('img', { name: '六号车厢' })).toHaveAttribute(
      'src', expect.stringContaining('/api/images/scene.jpg?verify='),
    )
    expect(screen.getByRole('img', { name: '乘务员' })).toHaveAttribute(
      'src', expect.stringContaining('/api/images/npc.jpg?verify='),
    )
    expect(screen.getByRole('img', { name: '染血车票' })).toHaveAttribute(
      'src', expect.stringContaining('/api/images/clue.jpg?verify='),
    )
  })
})
