export interface WeaponItem {
  name: string
  skill?: string
  success?: number
  dam?: string
  range?: string
  tho?: boolean
  round?: string
  num?: string
  err?: string
}

interface CharacterPayloadInput {
  name: string
  moduleId: string
  age: number
  baseAttributes: Record<string, number>
  skills: Record<string, number>
  backstory: string
  systemData: Record<string, unknown>
  equipmentText: string
  weapons: WeaponItem[]
}

function serializeWeapon(weapon: WeaponItem): Record<string, unknown> {
  const serialized: Record<string, unknown> = { name: weapon.name }
  if (weapon.skill !== undefined) serialized.skill = weapon.skill
  if (weapon.success !== undefined) serialized.success = weapon.success
  if (weapon.dam !== undefined) serialized.dam = weapon.dam
  if (weapon.range !== undefined) serialized.range = weapon.range
  if (weapon.tho !== undefined) serialized.tho = weapon.tho
  if (weapon.round !== undefined) serialized.round = weapon.round
  if (weapon.num !== undefined) serialized.num = weapon.num
  if (weapon.err !== undefined) serialized.err = weapon.err
  return serialized
}

export function buildCharacterPayload(input: CharacterPayloadInput) {
  const systemData = { ...input.systemData }
  const equipment = input.equipmentText
    .split(/[、,，]/)
    .map((item) => item.trim())
    .filter(Boolean)
  if (equipment.length > 0) systemData.equipment = equipment
  if (input.weapons.length > 0) {
    systemData.weapons = input.weapons.map(serializeWeapon)
  }

  return {
    name: input.name,
    module_id: input.moduleId,
    rule_system: 'coc',
    age: input.age,
    base_attributes: input.baseAttributes,
    skills: input.skills,
    backstory: input.backstory,
    system_data: systemData,
  }
}
