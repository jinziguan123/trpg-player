import { useEffect, useRef, useState } from 'react'
import { ImageOff, LoaderCircle } from 'lucide-react'
import { api, getServerUrl } from '@/api/client'

export type ModuleImageKind = 'scene' | 'npc' | 'clue'

interface ModuleImageProps {
  src?: string
  moduleId?: string
  kind: ModuleImageKind
  itemId: string
  field: 'image' | 'image_variant' | 'portrait' | 'encounter_image'
  alt: string
  aspectRatio?: string
  objectFit?: 'cover' | 'contain'
  className?: string
  onRegenerated?: (url: string) => void
  visualStateKey?: string
}

export interface RepairableImageOptions {
  src?: string
  moduleId?: string
  kind: ModuleImageKind
  itemId: string
  field: 'image' | 'image_variant' | 'portrait' | 'encounter_image'
  onRegenerated?: (url: string) => void
  visualStateKey?: string
}

function absoluteImageUrl(src: string): string {
  if (/^https?:\/\//i.test(src)) return src
  return `${getServerUrl()}${src}`
}

function verificationUrl(src: string): string {
  const url = absoluteImageUrl(src)
  const separator = url.includes('?') ? '&' : '?'
  return `${url}${separator}verify=${Date.now().toString(36)}`
}

export function ModuleImage({
  src,
  moduleId,
  kind,
  itemId,
  field,
  alt,
  aspectRatio = '16 / 9',
  objectFit = 'cover',
  className = '',
  onRegenerated,
  visualStateKey,
}: ModuleImageProps) {
  const image = useRepairableImage({ src, moduleId, kind, itemId, field, onRegenerated, visualStateKey })
  if (!src || !image.imageUrl) return null

  return (
    <div
      className={`relative overflow-hidden rounded-md ${className}`}
      style={{ aspectRatio, border: '1px solid var(--color-border)', background: 'var(--color-bg-tertiary)' }}
    >
      {image.status !== 'failed' && (
        <img
          src={image.imageUrl}
          alt={alt}
          className="block h-full w-full"
          style={{ objectFit, opacity: image.status === 'ready' ? 1 : 0.35 }}
          onLoad={image.onLoad}
          onError={image.onError}
        />
      )}
      {image.status === 'regenerating' && (
        <div className="absolute inset-0 flex items-center justify-center" aria-label="图片重新生成中">
          <LoaderCircle className="animate-spin" size={22} />
        </div>
      )}
      {image.status === 'failed' && (
        <div
          className="absolute inset-0 flex items-center justify-center gap-2 text-xs"
          style={{ color: 'var(--color-text-secondary)' }}
        >
          <ImageOff size={18} /> 图片暂不可用
        </div>
      )}
    </div>
  )
}

export function useRepairableImage({ src, moduleId, kind, itemId, field, onRegenerated, visualStateKey }: RepairableImageOptions) {
  const [imageUrl, setImageUrl] = useState(() => src ? verificationUrl(src) : '')
  const [status, setStatus] = useState<'loading' | 'ready' | 'regenerating' | 'failed'>('loading')
  const attemptedRef = useRef(false)

  useEffect(() => {
    setImageUrl(src ? verificationUrl(src) : '')
    setStatus('loading')
  }, [src])

  useEffect(() => {
    attemptedRef.current = false
  }, [moduleId, kind, itemId])

  const handleError = async () => {
    if (attemptedRef.current || !moduleId) {
      setStatus('failed')
      return
    }
    attemptedRef.current = true
    setStatus('regenerating')
    try {
      const result = await api.post<{ url: string }>(`/modules/${moduleId}/images/regenerate`, {
        kind,
        item_id: itemId,
        field,
        visual_state_key: visualStateKey,
      })
      setImageUrl(absoluteImageUrl(result.url))
      setStatus('loading')
      onRegenerated?.(result.url)
    } catch {
      setStatus('failed')
    }
  }

  return {
    imageUrl,
    status,
    onLoad: () => setStatus('ready'),
    onError: handleError,
  }
}
