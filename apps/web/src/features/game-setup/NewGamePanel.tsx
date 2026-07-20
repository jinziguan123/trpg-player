import { Search, Sparkles } from 'lucide-react'
import { SeatIcon } from '@/components/game/SeatIcon'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select'
import { MODULE_DIFFICULTIES } from '@/lib/module'
import type { GameSetupState } from './useGameSetup'

export function NewGamePanel({ setup }: { setup: GameSetupState }) {
  const {
    modules,
    filteredModules,
    filters,
    filterOptions,
    hasFilter,
    setFilter,
    resetFilters,
    moduleId,
    kpMode,
    setKpMode,
    selectedModule,
    range,
    minSeats,
    seats,
    seatHints,
    setSeatHint,
    generatingSeat,
    error,
    onSelectModule,
    changeSeatCount,
    assignSeat,
    seatOptions,
    generateForSeat,
    setSeatRole,
    allSeatsFilled,
    startGame,
  } = setup

  return (
    <div className="card mb-6">
      <h3 className="card-title">新游戏</h3>

      <div className="mb-3 space-y-2">
        <div className="relative">
          <Search
            size={14}
            className="absolute left-2 top-1/2 -translate-y-1/2"
            style={{ color: 'var(--color-text-secondary)' }}
          />
          <input
            value={filters.query}
            onChange={(event) => setFilter('query', event.target.value)}
            placeholder="搜索模组名、简介、标签、地区…"
            className="input w-full !pl-7"
          />
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <div
            className="flex items-center gap-1"
            title="按玩家人数上下限筛选：保留推荐人数区间与该范围有交集的模组"
          >
            <input
              type="number"
              min={1}
              value={filters.playerMin}
              onChange={(event) => setFilter('playerMin', event.target.value)}
              placeholder="人数≥"
              className="input !w-20"
            />
            <span style={{ color: 'var(--color-text-secondary)' }}>–</span>
            <input
              type="number"
              min={1}
              value={filters.playerMax}
              onChange={(event) => setFilter('playerMax', event.target.value)}
              placeholder="人数≤"
              className="input !w-20"
            />
          </div>
          <Select
            value={filters.era || '__all'}
            onValueChange={(value) => setFilter('era', value === '__all' ? '' : value)}
          >
            <SelectTrigger className="!w-28"><SelectValue placeholder="年代" /></SelectTrigger>
            <SelectContent>
              <SelectItem value="__all">年代 · 全部</SelectItem>
              {filterOptions.eras.map((era) => (
                <SelectItem key={era} value={era}>{era}</SelectItem>
              ))}
            </SelectContent>
          </Select>
          <Select
            value={filters.difficulty || '__all'}
            onValueChange={(value) => setFilter(
              'difficulty',
              value === '__all' ? '' : value,
            )}
          >
            <SelectTrigger className="!w-28"><SelectValue placeholder="难度" /></SelectTrigger>
            <SelectContent>
              <SelectItem value="__all">难度 · 全部</SelectItem>
              {MODULE_DIFFICULTIES.map((difficulty) => (
                <SelectItem key={difficulty} value={difficulty}>{difficulty}</SelectItem>
              ))}
            </SelectContent>
          </Select>
          <Select
            value={filters.region || '__all'}
            onValueChange={(value) => setFilter('region', value === '__all' ? '' : value)}
          >
            <SelectTrigger className="!w-28"><SelectValue placeholder="地区" /></SelectTrigger>
            <SelectContent>
              <SelectItem value="__all">地区 · 全部</SelectItem>
              {filterOptions.regions.map((region) => (
                <SelectItem key={region} value={region}>{region}</SelectItem>
              ))}
            </SelectContent>
          </Select>
          {hasFilter && (
            <button onClick={resetFilters} className="btn-secondary !px-2 !py-1 text-xs">
              清除筛选
            </button>
          )}
          <span className="ml-auto text-xs" style={{ color: 'var(--color-text-secondary)' }}>
            {filteredModules.length} / {modules.length} 个模组
          </span>
        </div>
      </div>

      <Select value={moduleId} onValueChange={onSelectModule}>
        <SelectTrigger className="mb-3 w-full">
          <SelectValue placeholder="— 选择模组 —" />
        </SelectTrigger>
        <SelectContent>
          {filteredModules.length === 0 ? (
            <div
              className="px-2 py-3 text-center text-sm"
              style={{ color: 'var(--color-text-secondary)' }}
            >
              无匹配模组
            </div>
          ) : filteredModules.map((module) => {
            const world = module.world_setting ?? {}
            const meta = [world.era, world.region, world.difficulty]
              .map((value) => String(value ?? ''))
              .filter(Boolean)
              .join(' · ')
            return (
              <SelectItem key={module.id} value={module.id}>
                {module.title}
                {meta ? (
                  <span style={{ color: 'var(--color-text-secondary)' }}>（{meta}）</span>
                ) : null}
              </SelectItem>
            )
          })}
        </SelectContent>
      </Select>

      {moduleId && (
        <>
          <div className="mb-3 flex flex-wrap items-center gap-2">
            <span className="text-sm font-medium">KP 模式</span>
            <button
              type="button"
              onClick={() => setKpMode('ai')}
              className="btn-secondary !px-2.5 !py-1 text-xs"
              style={kpMode === 'ai' ? { borderColor: 'var(--color-accent)', color: 'var(--color-text-accent)' } : undefined}
            >
              AI KP
            </button>
            <button
              type="button"
              onClick={() => setKpMode('human')}
              className="btn-secondary !px-2.5 !py-1 text-xs"
              style={kpMode === 'human' ? { borderColor: 'var(--color-accent)', color: 'var(--color-text-accent)' } : undefined}
            >
              真人 KP
            </button>
            <span className="text-xs" style={{ color: 'var(--color-text-secondary)' }}>
              {kpMode === 'human' ? '创建者只占 KP 席；玩家席等待其他真人用房间码加入。' : '由 AI 自动主持剧情。'}
            </span>
          </div>
          <div className="mb-1 flex items-center gap-2">
            <span className="text-sm font-medium">玩家人数</span>
            <button
              onClick={() => changeSeatCount(-1)}
              disabled={seats.length <= minSeats}
              className="btn-secondary !px-2 !py-0.5 disabled:opacity-40"
              aria-label="减少玩家人数"
            >
              −
            </button>
            <span
              className="w-6 text-center font-semibold"
              style={{ color: 'var(--color-text-accent)' }}
            >
              {seats.length}
            </span>
            <button
              onClick={() => changeSeatCount(1)}
              disabled={seats.length >= range.max}
              className="btn-secondary !px-2 !py-0.5 disabled:opacity-40"
              aria-label="增加玩家人数"
            >
              ＋
            </button>
          </div>
          <p className="mb-3 text-xs" style={{ color: 'var(--color-text-secondary)' }}>
            本模组推荐 {range.min}–{range.max} 人 · {kpMode === 'human' ? '真人 KP' : 'AI KP'} · {kpMode === 'human' ? '真人玩家席' : '你 1 人'} + AI 队友{' '}
            {Math.max(seats.length - 1, 0)} 人
            {range.min === 1 && range.max === 6 && !selectedModule?.world_setting?.player_count
              ? '（模组未标注人数，按默认范围）'
              : ''}
          </p>

          <div className="mb-3">
            {seats.map((seat, index) => {
              const emptyHuman = seat.role === 'human' && (index > 0 || kpMode === 'human')
              return (
                <div key={index} className="mb-2">
                  <div className="flex items-center gap-2">
                    <span
                      className="badge inline-flex whitespace-nowrap items-center gap-1"
                      style={index === 0 ? {
                        borderColor: 'var(--color-accent)',
                        color: 'var(--color-text-accent)',
                      } : undefined}
                    >
                      <SeatIcon kind={emptyHuman ? 'empty' : index === 0 ? 'me' : 'ai'} size={12} />
                      {kpMode === 'human' && index === 0
                        ? '真人玩家 1'
                        : index === 0 ? '你（真人）' : emptyHuman ? `真人 ${index + 1}` : `AI 队友 ${index}`}
                    </span>
                    {emptyHuman ? (
                      <span
                        className="flex-1 text-xs italic"
                        style={{ color: 'var(--color-text-secondary)' }}
                      >
                        {kpMode === 'human' && index === 0
                          ? '留空 · 创建者以 KP 身份进入，等待真人玩家加入认领'
                          : '留空 · 开局后分享房间码，等真人加入认领'}
                      </span>
                    ) : (
                      <Select value={seat.charId} onValueChange={(value) => assignSeat(index, value)}>
                        <SelectTrigger className="flex-1">
                          <SelectValue placeholder={index === 0 ? '选择你的角色' : '选择 AI 队友角色'} />
                        </SelectTrigger>
                        <SelectContent>
                          {seatOptions(index).map((character) => (
                            <SelectItem key={character.id} value={character.id}>
                              {character.name}
                            </SelectItem>
                          ))}
                        </SelectContent>
                      </Select>
                    )}
                    {index > 0 && (
                      <button
                        onClick={() => setSeatRole(index, emptyHuman ? 'ai' : 'human')}
                        className="btn-secondary whitespace-nowrap !px-2 !py-1 text-xs"
                        title="在「AI 队友」与「留空待真人加入」之间切换"
                      >
                        {emptyHuman ? '改为 AI' : '设为真人空席'}
                      </button>
                    )}
                    {!emptyHuman && (
                      <button
                        onClick={() => void generateForSeat(index)}
                        disabled={generatingSeat !== null}
                        className="btn-secondary inline-flex whitespace-nowrap items-center gap-1 !px-2 !py-1 text-xs"
                        title="让 AI 现场生成一张贴合模组的角色卡填入此席位"
                      >
                        {generatingSeat === index ? '生成中…' : (
                          <><Sparkles size={11} /> 生成</>
                        )}
                      </button>
                    )}
                  </div>
                  {!emptyHuman && (
                    <input
                      value={seatHints[index] ?? ''}
                      onChange={(event) => setSeatHint(index, event.target.value)}
                      placeholder="AI 生成提示（可选）：如 胆小的记者、退伍军医、通晓神秘学的教授"
                      className="input mt-1 w-full !py-0.5 text-xs"
                      style={{ color: 'var(--color-text-secondary)' }}
                    />
                  )}
                </div>
              )
            })}
          </div>

          {error && (
            <p className="mb-2 text-sm" style={{ color: 'var(--color-danger)' }}>{error}</p>
          )}
          <button
            onClick={() => void startGame()}
            disabled={!allSeatsFilled}
            className="btn-primary"
          >
            开始冒险（{seats.length} 名玩家）
          </button>
        </>
      )}
    </div>
  )
}
