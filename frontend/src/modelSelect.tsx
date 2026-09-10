import { Space, Tag } from 'antd'
import type { ModelOptions } from './api'

/** 下拉框的值把来源编进去：shared:<配置id> / personal:<模型名> */
export type Selection = { source: 'shared' | 'personal'; key: string }

export function parseSelection(value?: string): Selection | null {
  if (!value) return null
  const idx = value.indexOf(':')
  if (idx < 0) return null
  const source = value.slice(0, idx)
  if (source !== 'shared' && source !== 'personal') return null
  return { source, key: value.slice(idx + 1) }
}

export type ModelGroup = {
  label: string
  options: Array<{ value: string; label: JSX.Element; name: string }>
}

/** 模型下拉框的分组：公用模型 + 个人网关模型；includePersonal=false 时只给公用 */
export function buildModelGroups(options: ModelOptions | null, includePersonal = true): ModelGroup[] {
  if (!options) return []
  const groups: ModelGroup[] = []

  if (options.shared.length) {
    groups.push({
      label: '公用模型',
      options: options.shared.map((m) => ({
        value: `shared:${m.id}`,
        name: `${m.display_name} ${m.name}`,
        label: (
          <Space size={6}>
            <Tag color="blue" style={{ marginInlineEnd: 0 }}>公用</Tag>
            <span>{m.display_name}</span>
          </Space>
        ),
      })),
    })
  }
  if (includePersonal && options.personal.length) {
    groups.push({
      label: `我的模型 · ${options.gateway_label}`,
      options: options.personal.map((n) => ({
        value: `personal:${n}`,
        name: n,
        label: <span className="mono">{n}</span>,
      })),
    })
  }
  return groups
}

/** 按 name（显示名 + 唯一标识）模糊搜索 */
export const filterModelOption = (input: string, option?: unknown) =>
  String((option as { name?: string })?.name ?? '').toLowerCase().includes(input.toLowerCase())
