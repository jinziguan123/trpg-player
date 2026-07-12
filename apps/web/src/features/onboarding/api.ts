import { api } from '@/api/client'

export interface AIStatus {
  configured: boolean
  name: string | null
}

export interface OnboardingStartResult {
  session_id: string
  status: string
  reused: boolean
}

export function checkAIStatus() {
  return api.get<AIStatus>('/settings/ai/status')
}

export function startOnboarding() {
  return api.post<OnboardingStartResult>('/onboarding/start')
}
