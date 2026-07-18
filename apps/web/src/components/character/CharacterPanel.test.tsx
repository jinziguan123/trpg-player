import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { CharacterPanel } from './CharacterPanel'

const baseCharacter = {
  id: 'character-1',
  name: '山田健太',
  base_attributes: {},
  skills: {},
  system_data: {},
  backstory: '',
  status: 'active',
}

describe('CharacterPanel 状态显示', () => {
  it.each([
    ['ok', '正常'],
    ['dying', '濒死'],
    ['fled', '逃离'],
  ])('将 %s 显示为中文“%s”', (status, label) => {
    render(<CharacterPanel character={{ ...baseCharacter, status }} />)

    expect(screen.getByText(label)).toBeInTheDocument()
    expect(screen.queryByText(status)).not.toBeInTheDocument()
  })

  it('未知英文状态不直接暴露内部代码值', () => {
    render(<CharacterPanel character={{ ...baseCharacter, status: 'future_status' }} />)

    expect(screen.getByText('未知状态')).toBeInTheDocument()
    expect(screen.queryByText('future_status')).not.toBeInTheDocument()
  })
})
