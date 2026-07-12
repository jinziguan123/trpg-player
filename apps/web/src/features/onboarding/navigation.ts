const ALLOWED_RETURN_PATHS = new Set(['/onboarding'])

export function getOnboardingReturnTo(state: unknown): string | null {
  if (!state || typeof state !== 'object' || !('returnTo' in state)) return null
  const returnTo = (state as { returnTo?: unknown }).returnTo
  return typeof returnTo === 'string' && ALLOWED_RETURN_PATHS.has(returnTo)
    ? returnTo
    : null
}
