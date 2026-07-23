import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { beforeAll, beforeEach, describe, expect, it, vi } from 'vitest'
import { api } from '@/api/client'
import { ModuleDetailPage } from './ModuleDetailPage'

vi.mock('@/api/client', () => ({
  api: { get: vi.fn(), post: vi.fn(), put: vi.fn() },
  getServerUrl: () => '',
}))

vi.mock('@/components/game/HexSandbox', () => ({
  HexSandbox: ({
    locations,
    selectedIds = [],
    onToggleScene,
  }: {
    locations: { id: string; name: string; map?: { biome?: string } | null }[]
    selectedIds?: readonly string[]
    onToggleScene?: (id: string) => void
  }) => (
    <div data-testid="hex-sandbox">
      {locations.map((location) => (
        <button key={location.id} onClick={() => onToggleScene?.(location.id)}>
          选择{location.name}：{location.map?.biome || 'plain'}
        </button>
      ))}
      <span data-testid="sandbox-selection">{selectedIds.join(',')}</span>
    </div>
  ),
}))

const mockGet = vi.mocked(api.get)
const mockPost = vi.mocked(api.post)
const mockPut = vi.mocked(api.put)

describe('模组详情图片', () => {
  beforeAll(() => {
    Object.defineProperty(HTMLElement.prototype, 'scrollIntoView', {
      configurable: true,
      value: vi.fn(),
    })
  })

  beforeEach(() => {
    vi.clearAllMocks()
    mockGet.mockResolvedValue({
      id: 'module-1',
      title: '常暗之箱',
      rule_system: 'coc',
      description: '测试模组',
      world_setting: {},
      scenes: [
        { id: 'scene-1', name: '六号车厢', image: '/api/images/scene.jpg', map: { q: 0, r: 0, biome: 'plain' } },
        { id: 'scene-2', name: '餐车', map: { q: 1, r: 0, biome: 'urban' } },
      ],
      npcs: [{ id: 'npc-1', name: '乘务员', portrait: '/api/images/npc.jpg' }],
      clues: [{ id: 'clue-1', name: '染血车票', image: '/api/images/clue.jpg' }],
      triggers: [],
      truth: '',
    })
    mockPut.mockResolvedValue({
      id: 'module-1',
      title: '常暗之箱',
      rule_system: 'coc',
      description: '测试模组',
      world_setting: {},
      scenes: [],
      npcs: [],
      clues: [],
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

  it('沙盘编辑态支持单节点和批量修改地貌', async () => {
    const user = userEvent.setup()
    render(
      <MemoryRouter initialEntries={['/modules/module-1']}>
        <Routes>
          <Route path="/modules/:id" element={<ModuleDetailPage />} />
        </Routes>
      </MemoryRouter>,
    )

    await user.click(await screen.findByRole('button', { name: '沙盘' }))
    await user.click(screen.getByRole('button', { name: '编辑' }))
    await user.click(screen.getByRole('button', { name: /选择六号车厢/ }))
    expect(screen.getByText('已选 1 个地点')).toBeInTheDocument()

    expect(screen.queryByRole('combobox', { name: '设置选中节点地貌' })).not.toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: /拖入沙盘或点击使用地貌：密林/ }))
    expect(screen.getByRole('button', { name: /选择六号车厢：forest/ })).toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: '全选地图节点' }))
    expect(screen.getByText('已选 2 个地点')).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: /拖入沙盘或点击使用地貌：水域/ }))
    expect(screen.getByRole('button', { name: /选择六号车厢：water/ })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /选择餐车：water/ })).toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: '保存' }))
    await waitFor(() => expect(mockPut).toHaveBeenCalledOnce())
    const payload = mockPut.mock.calls[0][1] as { scenes: { map?: { biome?: string } }[] }
    expect(payload.scenes.map((scene) => scene.map?.biome)).toEqual(['water', 'water'])
  })

  it('沙盘编辑取消会恢复原地貌且不会保存', async () => {
    const user = userEvent.setup()
    render(
      <MemoryRouter initialEntries={['/modules/module-1']}>
        <Routes>
          <Route path="/modules/:id" element={<ModuleDetailPage />} />
        </Routes>
      </MemoryRouter>,
    )

    await user.click(await screen.findByRole('button', { name: '沙盘' }))
    await user.click(screen.getByRole('button', { name: '编辑' }))
    await user.click(screen.getByRole('button', { name: /选择六号车厢/ }))
    await user.click(screen.getByRole('button', { name: /拖入沙盘或点击使用地貌：密林/ }))
    expect(screen.getByRole('button', { name: /选择六号车厢：forest/ })).toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: '取消' }))
    expect(screen.getByRole('button', { name: /选择六号车厢：plain/ })).toBeInTheDocument()
    expect(mockPut).not.toHaveBeenCalled()
  })

  it('场景卡地貌修改会同步保存到统一地图节点', async () => {
    const user = userEvent.setup()
    render(
      <MemoryRouter initialEntries={['/modules/module-1']}>
        <Routes>
          <Route path="/modules/:id" element={<ModuleDetailPage />} />
        </Routes>
      </MemoryRouter>,
    )

    await user.click(await screen.findByRole('button', { name: '编辑' }))
    screen.getByRole('combobox', { name: '地貌：六号车厢' }).focus()
    await user.keyboard('{Enter}')
    fireEvent.click(await screen.findByRole('option', { name: '密林' }))
    await user.click(screen.getByRole('button', { name: '保存' }))

    await waitFor(() => expect(mockPut).toHaveBeenCalledOnce())
    const payload = mockPut.mock.calls[0][1] as {
      scenes: { id: string; map?: { biome?: string } }[]
      map_nodes: { scene_id?: string | null; biome?: string }[]
    }
    expect(payload.scenes[0].map?.biome).toBe('forest')
    expect(payload.map_nodes.find((node) => node.scene_id === 'scene-1')?.biome).toBe('forest')
  })

  it('右侧地貌样例可直接替换选中节点并支持道路', async () => {
    const user = userEvent.setup()
    render(
      <MemoryRouter initialEntries={['/modules/module-1'] }>
        <Routes>
          <Route path="/modules/:id" element={<ModuleDetailPage />} />
        </Routes>
      </MemoryRouter>,
    )

    await user.click(await screen.findByRole('button', { name: '沙盘' }))
    await user.click(screen.getByRole('button', { name: '编辑' }))
    await user.click(screen.getByRole('button', { name: /选择六号车厢/ }))
    await user.click(screen.getByRole('button', { name: /拖入沙盘或点击使用地貌：道路/ }))

    expect(screen.getByRole('button', { name: /选择六号车厢：road/ })).toBeInTheDocument()
  })

  it('右侧连接编辑器可双向新增和删除连接', async () => {
    const user = userEvent.setup()
    render(
      <MemoryRouter initialEntries={['/modules/module-1'] }>
        <Routes>
          <Route path="/modules/:id" element={<ModuleDetailPage />} />
        </Routes>
      </MemoryRouter>,
    )

    await user.click(await screen.findByRole('button', { name: '沙盘' }))
    await user.click(screen.getByRole('button', { name: '编辑' }))
    await user.click(screen.getByRole('button', { name: /选择六号车厢/ }))
    screen.getByRole('combobox', { name: '连接目标' }).focus()
    await user.keyboard('{Enter}')
    fireEvent.click(await screen.findByRole('option', { name: '餐车' }))
    await user.click(screen.getByRole('button', { name: '新增连接' }))
    expect(screen.queryByText('暂无连接')).not.toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: '删除连接：餐车' }))
    expect(screen.getByText('暂无连接')).toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: '保存' }))
    await waitFor(() => expect(mockPut).toHaveBeenCalledOnce())
    const payload = mockPut.mock.calls[0][1] as { scenes: { connections?: string[] }[] }
    expect(payload.scenes[0].connections).toEqual([])
    expect(payload.scenes[1].connections).toEqual([])
  })
})
