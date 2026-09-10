/**
 * 推理请求体的常见写法。不同网关开推理的字段完全不一样，
 * 与其让管理员每次手写 JSON，不如在这里列好可直接选用的模板。
 *
 * payload 里的 "$effort" 是档位占位符：后端在拼请求体时会把它换成用户
 * 选中的档位（纯数字会转成整数，budget_tokens 这类字段才拿得到 number）。
 */
export interface ReasoningPreset {
  value: string
  label: string
  hint: string
  payload: Record<string, unknown>
  /** 没开推理时附加的片段；空对象表示不开就等于关，不用额外带字段 */
  offPayload: Record<string, unknown>
  /** 建议给用户选的档位；空数组表示这种写法没有档位概念 */
  effortOptions: string[]
  /** 用户没选时用哪一档；空串表示取档位名单第一项 */
  defaultEffort: string
}

export const CUSTOM_PRESET = 'custom'

/**
 * reasoning_effort 类字段的通用档位，按强度从低到高排。
 * 各家支持的子集不同（none/minimal 只有部分实现认），
 * 所以档位输入框是可自由增删的 tags，这里只是备选项。
 */
export const EFFORT_LEVELS = ['none', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max']

export const EFFORT_LEVEL_OPTIONS = EFFORT_LEVELS.map((v) => ({ value: v, label: v }))

export const REASONING_PRESETS: ReasoningPreset[] = [
  {
    value: 'chat_template_kwargs',
    label: 'chat_template_kwargs.enable_thinking',
    hint: 'vLLM / SGLang 自建的 Qwen3、DeepSeek 等，开关走聊天模板参数',
    payload: { chat_template_kwargs: { enable_thinking: true } },
    // Qwen3 等模板默认就开着思考，不显式关掉会一直思考
    offPayload: { chat_template_kwargs: { enable_thinking: false } },
    effortOptions: [],
    defaultEffort: '',
  },
  {
    value: 'enable_thinking',
    label: 'enable_thinking（顶层）',
    hint: '百炼 / 部分网关把开关放在请求体顶层',
    payload: { enable_thinking: true },
    offPayload: { enable_thinking: false },
    effortOptions: [],
    defaultEffort: '',
  },
  {
    value: 'reasoning_effort',
    label: 'reasoning_effort',
    hint: 'OpenAI o 系、GPT-5 及大多数兼容实现',
    payload: { reasoning_effort: '$effort' },
    offPayload: {},
    effortOptions: [...EFFORT_LEVELS],
    defaultEffort: 'medium',
  },
  {
    value: 'reasoning_object',
    label: 'reasoning.effort',
    hint: 'OpenRouter 等把推理配置收在 reasoning 对象里',
    payload: { reasoning: { effort: '$effort' } },
    offPayload: {},
    effortOptions: [...EFFORT_LEVELS],
    defaultEffort: 'medium',
  },
  {
    value: 'thinking_budget',
    label: 'thinking.budget_tokens',
    hint: 'Anthropic 兼容层，档位直接就是思考预算 token 数',
    payload: { thinking: { type: 'enabled', budget_tokens: '$effort' } },
    offPayload: {},
    effortOptions: ['1024', '4096', '16384'],
    defaultEffort: '4096',
  },
  {
    value: 'thinking_budget_qwen',
    label: 'enable_thinking + thinking_budget',
    hint: 'Qwen3 系：开关与预算分开两个字段',
    payload: { enable_thinking: true, thinking_budget: '$effort' },
    offPayload: { enable_thinking: false },
    effortOptions: ['1024', '4096', '16384'],
    defaultEffort: '4096',
  },
]

const sameJson = (a: unknown, b: unknown) => JSON.stringify(a) === JSON.stringify(b)

/** 反查当前配置对应哪个预设，用于打开表单时回显下拉框 */
export function matchPreset(
  payload: Record<string, unknown> | undefined,
  effortOptions: string[] | undefined,
): string {
  const hit = REASONING_PRESETS.find(
    (p) => sameJson(p.payload, payload ?? {}) && sameJson(p.effortOptions, effortOptions ?? []),
  )
  return hit?.value ?? CUSTOM_PRESET
}

/**
 * 按强度从低到高排，滑动条才不会左右乱跳。
 * 全是已知档位就按 EFFORT_LEVELS 的顺序，全是数字（budget_tokens 那种）
 * 就按大小，其余情况保持管理员填的顺序不动。
 */
export function sortEfforts(options: string[]): string[] {
  if (options.every((o) => EFFORT_LEVELS.includes(o))) {
    return [...options].sort((a, b) => EFFORT_LEVELS.indexOf(a) - EFFORT_LEVELS.indexOf(b))
  }
  if (options.every((o) => /^\d+$/.test(o))) {
    return [...options].sort((a, b) => Number(a) - Number(b))
  }
  return options
}

/** 用户没选档位时落到哪一档；跟后端 resolve_reasoning_payload 保持一致 */
export function defaultEffort(options: string[], configured?: string): string | undefined {
  if (configured && options.includes(configured)) return configured
  return options[0]
}

export const PRESET_SELECT_OPTIONS = [
  ...REASONING_PRESETS.map((p) => ({ value: p.value, label: `${p.label} —— ${p.hint}` })),
  { value: CUSTOM_PRESET, label: '自定义（下面手写 JSON）' },
]
