interface RadarChartProps {
  labels: string[]
  values: number[]
  maxValue?: number
  size?: number
}

export function RadarChart({ labels, values, maxValue = 100, size = 200 }: RadarChartProps) {
  const cx = size / 2
  const cy = size / 2
  const r = size * 0.38
  const n = labels.length
  const angleStep = (2 * Math.PI) / n
  const offset = -Math.PI / 2

  const getPoint = (index: number, radius: number) => ({
    x: cx + radius * Math.cos(offset + index * angleStep),
    y: cy + radius * Math.sin(offset + index * angleStep),
  })

  const gridLevels = [0.25, 0.5, 0.75, 1]
  // 标签画在 r+18 处，会超出 size 边界被裁切 → viewBox 四周留白，整体缩放以容下标签
  const pad = 22

  const dataPoints = values.map((v, i) => {
    const ratio = Math.min(v / maxValue, 1)
    return getPoint(i, r * ratio)
  })

  const dataPath = dataPoints.map((p, i) => `${i === 0 ? 'M' : 'L'}${p.x},${p.y}`).join(' ') + ' Z'

  return (
    <svg viewBox={`${-pad} ${-pad} ${size + pad * 2} ${size + pad * 2}`} width={size} height={size}>
      {gridLevels.map((level) => {
        const points = Array.from({ length: n }, (_, i) => getPoint(i, r * level))
        const path = points.map((p, i) => `${i === 0 ? 'M' : 'L'}${p.x},${p.y}`).join(' ') + ' Z'
        return (
          <path key={level} d={path} fill="none" stroke="var(--color-border)" strokeWidth="0.5" opacity={0.6} />
        )
      })}

      {Array.from({ length: n }, (_, i) => {
        const p = getPoint(i, r)
        return <line key={i} x1={cx} y1={cy} x2={p.x} y2={p.y} stroke="var(--color-border)" strokeWidth="0.5" opacity={0.4} />
      })}

      <path d={dataPath} fill="rgba(212, 162, 78, 0.16)" stroke="var(--color-accent)" strokeWidth="1.5" />

      {dataPoints.map((p, i) => (
        <circle key={i} cx={p.x} cy={p.y} r="2.5" fill="var(--color-accent)" />
      ))}

      {labels.map((label, i) => {
        const p = getPoint(i, r + 18)
        return (
          <text
            key={i}
            x={p.x}
            y={p.y}
            textAnchor="middle"
            dominantBaseline="middle"
            fontSize="10"
            fill="var(--color-text-secondary)"
            fontFamily="var(--font-body)"
          >
            {label}
          </text>
        )
      })}
    </svg>
  )
}
