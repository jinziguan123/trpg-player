import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter, Route, Routes, useLocation } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { HomePage } from '@/pages/HomePage'
import { OnboardingPage } from './OnboardingPage'
import { checkAIStatus, startOnboarding } from './api'

vi.mock('./api', () => ({
  checkAIStatus: vi.fn(),
  startOnboarding: vi.fn(),
}))

const mockCheckAIStatus = vi.mocked(checkAIStatus)
const mockStartOnboarding = vi.mocked(startOnboarding)

function LocationProbe() {
  const location = useLocation()
  return <pre data-testid="location">{JSON.stringify(location)}</pre>
}

function renderFlow() {
  return render(
    <MemoryRouter initialEntries={['/onboarding']}>
      <Routes>
        <Route path="/onboarding" element={<OnboardingPage />} />
        <Route path="/settings" element={<LocationProbe />} />
        <Route path="/game/:sessionId" element={<LocationProbe />} />
      </Routes>
    </MemoryRouter>,
  )
}

describe('OnboardingPage', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('AI 已配置时创建会话并进入游戏', async () => {
    mockCheckAIStatus.mockResolvedValue({ configured: true, name: '测试模型' })
    mockStartOnboarding.mockResolvedValue({
      session_id: 'session-1',
      status: 'active',
      reused: false,
    })

    renderFlow()

    await waitFor(() => expect(screen.getByTestId('location')).toHaveTextContent('/game/session-1'))
    expect(screen.getByTestId('location')).toHaveTextContent('"isNew":true')
    expect(mockStartOnboarding).toHaveBeenCalledTimes(1)
  })

  it('AI 未配置时说明原因并携带返回意图进入设置', async () => {
    const user = userEvent.setup()
    mockCheckAIStatus.mockResolvedValue({ configured: false, name: null })

    renderFlow()

    expect(await screen.findByText('需要先配置 AI')).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: '配置 AI' }))

    expect(screen.getByTestId('location')).toHaveTextContent('/settings')
    expect(screen.getByTestId('location')).toHaveTextContent('"returnTo":"/onboarding"')
    expect(mockStartOnboarding).not.toHaveBeenCalled()
  })

  it('创建失败后保留可用的重试操作', async () => {
    const user = userEvent.setup()
    mockCheckAIStatus.mockResolvedValue({ configured: true, name: '测试模型' })
    mockStartOnboarding
      .mockRejectedValueOnce(new Error('创建失败'))
      .mockResolvedValueOnce({
        session_id: 'session-2',
        status: 'active',
        reused: false,
      })

    renderFlow()

    expect(await screen.findByText('未能启动新手团')).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: '重试' }))

    await waitFor(() => expect(screen.getByTestId('location')).toHaveTextContent('/game/session-2'))
    expect(mockStartOnboarding).toHaveBeenCalledTimes(2)
  })
})

it('首页提供唯一的新手团主入口', () => {
  render(
    <MemoryRouter>
      <HomePage />
    </MemoryRouter>,
  )

  const entries = screen.getAllByRole('link', { name: /体验新手团/ })
  expect(entries).toHaveLength(1)
  expect(entries[0]).toHaveAttribute('href', '/onboarding')
})
