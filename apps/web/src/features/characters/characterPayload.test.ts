import { describe, expect, it } from 'vitest'
import { buildCharacterPayload } from './characterPayload'

describe('buildCharacterPayload', () => {
  it('完整序列化装备和武器字段', () => {
    const payload = buildCharacterPayload({
      name: '许闻舟',
      moduleId: 'module-1',
      age: 29,
      baseAttributes: { STR: 50 },
      skills: { 侦查: 60 },
      backstory: '港口记者',
      systemData: { occupation: '记者' },
      equipmentText: '手电筒、 笔记本,相机',
      weapons: [{
        name: '左轮手枪',
        skill: '射击(手枪)',
        success: 45,
        dam: '1D10',
        range: '15m',
        tho: true,
        round: '1',
        num: '6',
        err: '100',
      }],
    })

    expect(payload.system_data.equipment).toEqual(['手电筒', '笔记本', '相机'])
    expect(payload.system_data.weapons).toEqual([{
      name: '左轮手枪',
      skill: '射击(手枪)',
      success: 45,
      dam: '1D10',
      range: '15m',
      tho: true,
      round: '1',
      num: '6',
      err: '100',
    }])
    expect(payload).toMatchObject({
      name: '许闻舟',
      module_id: 'module-1',
      rule_system: 'coc',
      age: 29,
      base_attributes: { STR: 50 },
      skills: { 侦查: 60 },
      backstory: '港口记者',
    })
  })

  it('不写入空装备和空武器数组', () => {
    const payload = buildCharacterPayload({
      name: '测试角色',
      moduleId: 'module-1',
      age: 25,
      baseAttributes: {},
      skills: {},
      backstory: '',
      systemData: {},
      equipmentText: '  ',
      weapons: [],
    })

    expect(payload.system_data).not.toHaveProperty('equipment')
    expect(payload.system_data).not.toHaveProperty('weapons')
  })
})
