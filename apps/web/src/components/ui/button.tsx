import * as React from 'react'
import { Slot } from '@radix-ui/react-slot'
import { cva, type VariantProps } from 'class-variance-authority'
import { cn } from '@/lib/utils'

const buttonVariants = cva(
  'inline-flex items-center justify-center gap-2 whitespace-nowrap rounded-[3px] text-sm font-semibold transition-colors focus-visible:outline-none disabled:pointer-events-none disabled:opacity-40 cursor-pointer',
  {
    variants: {
      variant: {
        default: 'btn-primary',
        secondary: 'btn-secondary',
        danger:
          'border text-sm px-3 py-1.5 rounded-[3px] hover:bg-[rgba(192,94,102,0.12)] transition-colors',
        ghost: 'hover:bg-[rgba(212,162,78,0.08)]',
      },
      size: {
        default: 'h-9 px-4 py-2',
        sm: 'h-7 px-2 py-1 text-xs',
        lg: 'h-10 px-6 py-2',
        icon: 'h-8 w-8',
      },
    },
    defaultVariants: {
      variant: 'default',
      size: 'default',
    },
  },
)

export interface ButtonProps
  extends React.ButtonHTMLAttributes<HTMLButtonElement>,
    VariantProps<typeof buttonVariants> {
  asChild?: boolean
}

const Button = React.forwardRef<HTMLButtonElement, ButtonProps>(
  ({ className, variant, size, asChild = false, style, ...props }, ref) => {
    const Comp = asChild ? Slot : 'button'
    const dangerStyle =
      variant === 'danger'
        ? { color: 'var(--color-danger)', borderColor: 'var(--color-danger)', ...style }
        : style
    return (
      <Comp
        className={cn(buttonVariants({ variant, size, className }))}
        ref={ref}
        style={dangerStyle}
        {...props}
      />
    )
  },
)
Button.displayName = 'Button'

export { Button, buttonVariants }
