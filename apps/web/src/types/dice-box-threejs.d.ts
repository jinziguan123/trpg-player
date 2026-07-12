declare module '@3d-dice/dice-box-threejs' {
  export default class DiceBox {
    constructor(selector: string, config: Record<string, unknown>)
    initialize?: () => Promise<void>
    roll(notation: string): Promise<unknown>
    clearDice?: () => void
    dispose?: () => void
  }
}
