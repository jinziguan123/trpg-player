import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { api } from '@/api/client'
import { ModuleDetailPage } from './ModuleDetailPage'

vi.mock('@/api/client', () => ({
  api: { get: vi.fn(), post: vi.fn(), put: vi.fn() },
  getServerUrl: () => '',
}))

vi.mock('@/components/game/HexSandbox', () => ({
  HexSandbox: () => <div data-testid="hex-sandbox" />,
}))

const mockGet = vi.mocked(api.get)
const mockPost = vi.mocked(api.post)

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

  it('确认 AI 补全后调用接口并重新加载模组', async () => {
    const user = userEvent.setup()
    mockPost.mockResolvedValue({ updated: true })
    render(
      <MemoryRouter initialEntries={['/modules/module-1']}>
        <Routes>
          <Route path="/modules/:id" element={<ModuleDetailPage />} />
        </Routes>
      </MemoryRouter>,
    )

    await user.click(await screen.findByRole('button', { name: '沙盘' }))
    await user.click(screen.getByRole('button', { name: 'AI 补全地貌与连接' }))
    expect(screen.getByText('已有连接不会被删除', { exact: false })).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: '开始补全' }))

    await waitFor(() => expect(mockPost).toHaveBeenCalledWith('/modules/module-1/map/enrich'))
    await waitFor(() => expect(mockGet).toHaveBeenCalledTimes(2))
  })
})
