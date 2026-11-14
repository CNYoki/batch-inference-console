import { useEffect, useMemo } from 'react'
import type { ReactNode } from 'react'
import { Alert, Col, Collapse, Form, Input, InputNumber, Row, Select, Slider, Space, Switch, Tag } from 'antd'
import type { FormInstance } from 'antd'
import type { ModelType } from '../api'
import type { ModelCaps } from '../modelSelect'
import { defaultEffort, sortEfforts } from '../reasoning'
import { formatNumber } from '../utils'

/**
 * 推理档位滑动条。表单里存的是档位字符串，滑块按名单下标定位，
 * step=null 让它只停在刻度上。
 */
function EffortSlider(
  { options, value, onChange }:
  { options: string[]; value?: string; onChange?: (v: string) => void },
) {
  if (options.length < 2) return <Tag color="purple">{value ?? options[0]}</Tag>
  const index = Math.max(0, options.indexOf(value ?? ''))
  return (
    <Slider
      min={0} max={options.length - 1} step={null} value={index}
      marks={Object.fromEntries(options.map(
        (o, i) => [i, <span style={{ fontSize: 12, whiteSpace: 'nowrap' }}>{o}</span>],
      ))}
      tooltip={{ open: false }}
      onChange={(v) => onChange?.(options[v as number])}
    />
  )
}

/** 任务上存的推理参数 → 表单值；附加参数在表单里是 JSON 文本 */
export function paramsToForm(params: Record<string, unknown>) {
  // dimensions / encoding_format 只属于 embedding，不留在 chat 的附加参数里，
  // 否则 embedding 任务换成 chat 模型时会被发出去；user 两边都能用
  const { dimensions, encoding_format, ...extra } = (params.extra ?? {}) as Record<string, unknown>
  return {
    dimensions: dimensions ?? undefined,
    encoding_format: encoding_format ?? undefined,
    user: extra.user ?? undefined,
    system_prompt: params.system_prompt ?? undefined,
    temperature: params.temperature ?? undefined,
    top_p: params.top_p ?? undefined,
    max_tokens: params.max_tokens ?? undefined,
    seed: params.seed ?? undefined,
    json_mode: !!params.json_mode,
    reasoning: !!params.reasoning,
    reasoning_effort: params.reasoning_effort ?? undefined,
    extra: Object.keys(extra).length ? JSON.stringify(extra, null, 2) : undefined,
  }
}

/** 表单值 → 提交给后端的推理参数。附加参数不是合法 JSON 时抛 SyntaxError */
export function formToParams(
  values: Record<string, unknown>, modelType: ModelType = 'chat',
): Record<string, unknown> {
  if (modelType === 'embedding') {
    return {
      system_prompt: null, temperature: null, top_p: null, max_tokens: null, seed: null,
      json_mode: false, reasoning: false, reasoning_effort: null,
      extra: Object.fromEntries(
        ['dimensions', 'encoding_format', 'user']
          .filter((k) => values[k] !== undefined && values[k] !== null && values[k] !== '')
          .map((k) => [k, values[k]]),
      ),
    }
  }
  const extra = typeof values.extra === 'string' ? values.extra.trim() : ''
  return {
    system_prompt: values.system_prompt || null,
    temperature: values.temperature ?? null,
    top_p: values.top_p ?? null,
    max_tokens: values.max_tokens ?? null,
    seed: values.seed ?? null,
    json_mode: !!values.json_mode,
    reasoning: !!values.reasoning,
    reasoning_effort: values.reasoning_effort || null,
    extra: extra ? JSON.parse(extra) : {},
  }
}

/**
 * 推理参数表单项，新建任务页和「更换模型 / 修改参数」弹窗共用。
 * 按模型能力决定显示哪些项；要放在所在页面的 <Form> 里。
 */
export default function JobParamsFields(
  { form, caps, rowExtra, advanced }:
  {
    form: FormInstance
    caps?: ModelCaps
    /** 放在 top_p / seed 那一行的第三格（新建任务页放并发数） */
    rowExtra?: ReactNode
    /** 高级选项里、附加参数之前的内容（新建任务页放优先级） */
    advanced?: ReactNode
  },
) {
  const reasoningOn = Form.useWatch('reasoning', form) as boolean | undefined

  const effortKey = (caps?.reasoning_effort_options ?? []).join('|')
  const effortOptions = useMemo(
    () => sortEfforts(effortKey ? effortKey.split('|') : []), [effortKey],
  )
  const effortFallback = defaultEffort(effortOptions, caps?.reasoning_default_effort)
  useEffect(() => {
    // 换模型后档位名单可能完全不同：原来的档位新模型也有就保留，没有就回到该模型的默认档
    const current = form.getFieldValue('reasoning_effort') as string | undefined
    const next = current && effortOptions.includes(current) ? current : effortFallback
    if (next !== current) form.setFieldValue('reasoning_effort', next)
  }, [form, effortOptions, effortFallback])

  if (caps?.model_type === 'embedding') {
    return (
      <>
        <Alert type="info" showIcon style={{ marginBottom: 16 }}
          message="Embedding 模型：数据每行需要 input 字段，对话参数不适用。" />
        <Row gutter={16}>
          <Col xs={24} sm={12}>
            <Form.Item name="dimensions" label="dimensions" extra="向量维度，留空用模型默认值">
              <InputNumber min={1} precision={0} style={{ width: '100%' }} placeholder="可选" />
            </Form.Item>
          </Col>
          <Col xs={24} sm={12}>
            <Form.Item name="encoding_format" label="encoding_format">
              <Select allowClear placeholder="float（默认）"
                options={[{ value: 'float', label: 'float' }, { value: 'base64', label: 'base64' }]} />
            </Form.Item>
          </Col>
          <Col xs={24} sm={12}>
            <Form.Item name="user" label="user" extra="终端用户标识，可选">
              <Input maxLength={256} placeholder="可选" />
            </Form.Item>
          </Col>
          {rowExtra && <Col xs={24} sm={12}>{rowExtra}</Col>}
        </Row>
        {advanced && (
          <Collapse size="small" items={[{ key: 'adv', label: '高级选项', children: advanced }]} />
        )}
      </>
    )
  }

  return (
    <>
      {caps?.supports_system_prompt !== false && (
        <Form.Item name="system_prompt" label="System Prompt（可选）"
          extra="会作为 system 消息注入到每一条请求；不覆盖数据中的 system 消息。">
          <Input.TextArea rows={3} maxLength={20000} showCount
            placeholder="例：你是一名严谨的数据标注员，只输出 JSON。" />
        </Form.Item>
      )}

      <Row gutter={16}>
        {caps?.supports_temperature !== false && (
          <Col xs={24} sm={12}>
            <Form.Item name="temperature" label="temperature">
              <Slider min={0} max={2} step={0.1} marks={{ 0: '0', 1: '1', 2: '2' }} />
            </Form.Item>
          </Col>
        )}
        <Col xs={24} sm={12}>
          <Form.Item name="max_tokens" label="max_tokens"
            extra={caps?.max_tokens_cap ? `上限 ${formatNumber(caps.max_tokens_cap)}` : undefined}>
            <InputNumber min={1} max={caps?.max_tokens_cap || 200000}
              style={{ width: '100%' }} placeholder="留空则使用模型默认值" />
          </Form.Item>
        </Col>
      </Row>

      <Row gutter={16}>
        <Col xs={12} sm={8}>
          <Form.Item name="top_p" label="top_p">
            <InputNumber min={0} max={1} step={0.05} style={{ width: '100%' }} placeholder="可选" />
          </Form.Item>
        </Col>
        <Col xs={12} sm={8}>
          <Form.Item name="seed" label="seed">
            <InputNumber style={{ width: '100%' }} placeholder="可选" />
          </Form.Item>
        </Col>
        {rowExtra && <Col xs={12} sm={8}>{rowExtra}</Col>}
      </Row>

      <Space size={24} wrap style={{ marginBottom: 16 }}>
        {caps?.supports_json_mode && (
          <Form.Item name="json_mode" label="JSON 输出模式" valuePropName="checked"
            style={{ marginBottom: 0 }}>
            <Switch />
          </Form.Item>
        )}
        {caps?.reasoning_mode === 'optional' && (
          <Form.Item name="reasoning" label="开启深度推理" valuePropName="checked"
            style={{ marginBottom: 0 }}>
            <Switch />
          </Form.Item>
        )}
        {caps?.reasoning_mode === 'forced' && <Tag color="purple">该模型始终开启深度推理</Tag>}
      </Space>

      {!!effortOptions.length && (caps?.reasoning_mode === 'forced' || reasoningOn) && (
        <Form.Item name="reasoning_effort" label="推理档位"
          style={{ maxWidth: 480, marginBottom: 16 }}>
          <EffortSlider options={effortOptions} />
        </Form.Item>
      )}

      <Collapse
        size="small"
        items={[{
          key: 'adv',
          label: '高级选项',
          children: (
            <>
              {advanced}
              <Form.Item name="extra" label="附加请求参数 (JSON)"
                extra="会合并进每条请求体，例如 {&quot;response_format&quot;: {&quot;type&quot;: &quot;json_object&quot;}}">
                <Input.TextArea rows={3} placeholder="{}" className="mono" />
              </Form.Item>
            </>
          ),
        }]}
      />
    </>
  )
}
