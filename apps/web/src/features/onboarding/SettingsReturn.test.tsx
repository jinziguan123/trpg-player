import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter, Route, Routes, useLocation } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { api } from '@/api/client'
import { SettingsPage } from '@/pages/SettingsPage'

vi.mock('@/api/client', () => ({
  api: {
    get: vi.fn(),
    post: vi.fn(),
    put: vi.fn(),
    delete: vi.fn(),
  },
}))

const mockGet = vi.mocked(api.get)
const mockPost = vi.mocked(api.post)

function LocationProbe() {
  return <span data-testid="pathname">{useLocation().pathname}</span>
}

function renderSettings() {
  return render(
    <MemoryRouter
      initialEntries={[{ pathname: '/settings', state: { returnTo: '/onboarding' } }]}
    >
      <Routes>
        <Route path="/settings" element={<SettingsPage />} />
        <Route path="/onboarding" element={<LocationProbe />} />
      </Routes>
    </MemoryRouter>,
  )
}

describe('设置页的新手团返回意图', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    mockGet.mockResolvedValue([
      {
        id: 'profile-1',
        name: '测试模型',
        protocol: 'openai',
        base_url: '',
        model_name: 'test-model',
        api_key: '****',
        is_active: true,
      },
    ])
  })

  it('连接测试成功后返回新手团', async () => {
    const user = userEvent.setup()
    mockPost.mockResolvedValue({ success: true, message: '连接成功', latency_ms: 12 })
    renderSettings()

    await user.click(await screen.findByRole('button', { name: '测试' }))

    await waitFor(() => expect(screen.getByTestId('pathname')).toHaveTextContent('/onboarding'))
  })

  it('连接测试失败时留在设置页', async () => {
    const user = userEvent.setup()
    mockPost.mockResolvedValue({ success: false, message: '连接失败', latency_ms: 12 })
    renderSettings()

    await user.click(await screen.findByRole('button', { name: '测试' }))

    expect(screen.queryByTestId('pathname')).not.toBeInTheDocument()
    expect(screen.getByRole('heading', { name: 'AI 配置' })).toBeInTheDocument()
  })
})
