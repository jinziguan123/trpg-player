import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { api } from '@/api/client'
import { ModuleImage } from './ModuleImage'

vi.mock('@/api/client', () => ({
  api: { post: vi.fn() },
  getServerUrl: () => '',
}))

const mockPost = vi.mocked(api.post)

describe('ModuleImage', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('图片 404 时只重新生成一次，并切换到新 URL', async () => {
    const onRegenerated = vi.fn()
    mockPost.mockResolvedValue({ url: '/api/images/new.jpg' })
    render(
      <ModuleImage
        src="/api/images/deleted.jpg"
        moduleId="module-1"
        kind="scene"
        itemId="scene-1"
        field="image"
        alt="旧教堂"
        onRegenerated={onRegenerated}
      />,
    )

    const image = screen.getByRole('img', { name: '旧教堂' })
    expect(image).toHaveAttribute('src', expect.stringContaining('/api/images/deleted.jpg?verify='))
    fireEvent.error(image)

    await waitFor(() => expect(mockPost).toHaveBeenCalledWith(
      '/modules/module-1/images/regenerate',
      { kind: 'scene', item_id: 'scene-1', field: 'image' },
    ))
    await waitFor(() => expect(image).toHaveAttribute('src', '/api/images/new.jpg'))
    expect(onRegenerated).toHaveBeenCalledWith('/api/images/new.jpg')

    fireEvent.error(image)
    expect(mockPost).toHaveBeenCalledTimes(1)
    expect(await screen.findByText('图片暂不可用')).toBeInTheDocument()
  })

  it('没有图片 URL 时不渲染占位', () => {
    const { container } = render(
      <ModuleImage kind="clue" itemId="clue-1" field="image" alt="线索" />,
    )
    expect(container).toBeEmptyDOMElement()
  })
})
