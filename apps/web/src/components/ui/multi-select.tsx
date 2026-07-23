import * as PopoverPrimitive from '@radix-ui/react-popover'
import { Check, ChevronDown } from 'lucide-react'
import { cn } from '@/lib/utils'

export interface MultiSelectOption {
  value: string
  label: string
}

interface MultiSelectProps {
  value: string[]
  options: MultiSelectOption[]
  onValueChange: (value: string[]) => void
  placeholder: string
  'aria-label': string
  className?: string
}

export function MultiSelect({
  value,
  options,
  onValueChange,
  placeholder,
  className,
  'aria-label': ariaLabel,
}: MultiSelectProps) {
  const selectedOptions = options.filter((option) => value.includes(option.value))
  const summary = selectedOptions.length === 0
    ? placeholder
    : selectedOptions.length <= 2
      ? selectedOptions.map((option) => option.label).join('、')
      : `已选择 ${selectedOptions.length} 名`

  const toggle = (optionValue: string) => {
    onValueChange(
      value.includes(optionValue)
        ? value.filter((item) => item !== optionValue)
        : [...value, optionValue],
    )
  }

  return (
    <PopoverPrimitive.Root>
      <PopoverPrimitive.Trigger asChild>
        <button
          type="button"
          aria-label={ariaLabel}
          className={cn(
            'flex h-9 min-w-0 items-center justify-between rounded-[3px] border px-3 py-2 text-left text-xs',
            'focus:outline-none focus-visible:ring-2 focus-visible:ring-[rgba(212,162,78,0.12)]',
            'disabled:cursor-not-allowed disabled:opacity-50',
            className,
          )}
          style={{
            borderColor: 'var(--color-border-strong)',
            background: 'var(--color-input-bg)',
            color: selectedOptions.length ? 'var(--color-text-primary)' : 'var(--color-text-secondary)',
            fontFamily: 'var(--font-ui)',
          }}
          disabled={options.length === 0}
        >
          <span className="min-w-0 flex-1 truncate">{options.length ? summary : '暂无可选 NPC'}</span>
          <ChevronDown size={12} className="ml-2 shrink-0 opacity-60" aria-hidden="true" />
        </button>
      </PopoverPrimitive.Trigger>
      <PopoverPrimitive.Portal>
        <PopoverPrimitive.Content
          align="start"
          sideOffset={4}
          className="z-[110] max-h-72 w-[var(--radix-popover-trigger-width)] min-w-48 overflow-y-auto rounded-md border p-1 shadow-lg"
          style={{
            background: 'var(--color-bg-secondary)',
            borderColor: 'var(--color-border-strong)',
            fontFamily: 'var(--font-ui)',
          }}
        >
          <div role="listbox" aria-label={ariaLabel} aria-multiselectable="true">
            {options.map((option) => {
              const selected = value.includes(option.value)
              return (
                <button
                  key={option.value}
                  type="button"
                  role="option"
                  aria-selected={selected}
                  onClick={() => toggle(option.value)}
                  className="flex w-full items-center gap-2 rounded-sm px-2 py-1.5 text-left text-xs outline-none hover:bg-[rgba(212,162,78,0.12)] focus-visible:bg-[rgba(212,162,78,0.12)]"
                  style={{ color: selected ? 'var(--color-text-accent)' : 'var(--color-text-primary)' }}
                >
                  <span
                    className="flex h-4 w-4 shrink-0 items-center justify-center rounded-[2px] border"
                    style={{
                      borderColor: selected ? 'var(--color-accent)' : 'var(--color-border-strong)',
                      background: selected ? 'var(--color-accent)' : 'transparent',
                      color: 'var(--color-on-accent)',
                    }}
                    aria-hidden="true"
                  >
                    {selected && <Check size={12} strokeWidth={2.5} />}
                  </span>
                  <span className="min-w-0 flex-1 truncate">{option.label}</span>
                </button>
              )
            })}
          </div>
          {value.length > 0 && (
            <div className="mt-1 border-t pt-1" style={{ borderColor: 'var(--color-border)' }}>
              <button
                type="button"
                onClick={() => onValueChange([])}
                className="w-full rounded-sm px-2 py-1.5 text-left text-xs hover:bg-[rgba(212,162,78,0.12)] focus-visible:outline-none focus-visible:bg-[rgba(212,162,78,0.12)]"
                style={{ color: 'var(--color-text-secondary)' }}
              >
                清空选择
              </button>
            </div>
          )}
        </PopoverPrimitive.Content>
      </PopoverPrimitive.Portal>
    </PopoverPrimitive.Root>
  )
}
