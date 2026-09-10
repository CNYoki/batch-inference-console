import { Space, Tag } from 'antd'
import type { ModelOption, ModelOptions, PersonalReasoningCap, SystemSettings } from './api'

/** 参数表单要看的模型能力：决定哪些参数显示、推理档位有哪些 */
export type ModelCaps = Pick<
  ModelOption,
  | 'supports_temperature' | 'supports_system_prompt' | 'supports_json_mode'
  | 'reasoning_mode' | 'reasoning_effort_options' | 'reasoning_default_effort'
  | 'max_concurrency' | 'max_tokens_cap'
>

/**
 * 个人模型拿不到能力声明，采样参数按最宽松处理，由网关自己拒绝不支持的；
 * 推理部分则用后端按模型名规则算好的 cap，拿不到才退回网关默认配置。
 */
export function personalCaps(settings: SystemSettings | null, cap?: PersonalReasoningCap): ModelCaps {
  const fallbackMode = (settings?.user_gateway_reasoning_enabled ?? true) ? 'optional' : 'off'
  return {
    supports_temperature: true,
    supports_system_prompt: true,
    supports_json_mode: true,
    // 推理开关默认可切换、默认关闭；管理员可以整体关掉，也可以按模型名关掉
    reasoning_mode: cap?.reasoning_mode ?? fallbackMode,
    reasoning_effort_options:
      cap?.effort_options ?? settings?.user_gateway_reasoning_effort_options ?? [],
    reasoning_default_effort:
      cap?.default_effort ?? settings?.user_gateway_reasoning_default_effort ?? '',
    max_concurrency: settings?.user_gateway_max_concurrency ?? 0,
    max_tokens_cap: settings?.user_gateway_max_tokens_cap ?? 0,
  }
}

/** 公用模型用它自己的能力声明，个人模型用网关按模型名算出来的那份 */
export function capsFor(
  selection: Selection | null, options: ModelOptions | null, settings: SystemSettings | null,
): ModelCaps | undefined {
  if (!selection) return undefined
  if (selection.source === 'personal') {
    return personalCaps(settings, options?.personal_reasoning?.[selection.key])
  }
  return options?.shared.find((m) => m.id === selection.key)
}

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
