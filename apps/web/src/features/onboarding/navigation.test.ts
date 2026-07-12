import { describe, expect, it } from 'vitest'
import { getOnboardingReturnTo } from './navigation'

describe('getOnboardingReturnTo', () => {
  it('接受受支持的站内返回路径', () => {
    expect(getOnboardingReturnTo({ returnTo: '/onboarding' })).toBe('/onboarding')
  })

  it('拒绝绝对 URL 和未知站内路径', () => {
    expect(getOnboardingReturnTo({ returnTo: 'https://example.com' })).toBeNull()
    expect(getOnboardingReturnTo({ returnTo: '//example.com' })).toBeNull()
    expect(getOnboardingReturnTo({ returnTo: '/game' })).toBeNull()
    expect(getOnboardingReturnTo(null)).toBeNull()
  })
})
