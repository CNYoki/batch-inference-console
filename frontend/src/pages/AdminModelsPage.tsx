import { useCallback, useEffect, useState } from 'react'
import {
  Alert, App, Button, Card, Col, Collapse, Drawer, Form, Input, InputNumber, Row, Select,
  Space, Switch, Table, Tag, Tooltip, Typography,
} from 'antd'
import type { ColumnsType } from 'antd/es/table'
import { ApiOutlined, DeleteOutlined, EditOutlined, PlusOutlined, ThunderboltOutlined } from '@ant-design/icons'
import { api } from '../api'
import type { ModelConfig } from '../api'
import {
  CUSTOM_PRESET, EFFORT_LEVEL_OPTIONS, PRESET_SELECT_OPTIONS, REASONING_PRESETS, matchPreset,
} from '../reasoning'

type ProbeState = Record<string, { ok: boolean; text: string } | 'loading'>

const JSON_FIELDS = [
  'default_params', 'forced_params', 'reasoning_payload', 'reasoning_off_payload', 'extra_headers',
] as const

export default function AdminModelsPage() {
  const { modal, message } = App.useApp()
  const [form] = Form.useForm()
  const [rows, setRows] = useState<ModelConfig[]>([])
  const [loading, setLoading] = useState(true)
  const [open, setOpen] = useState(false)
  const [editing, setEditing] = useState<ModelConfig | null>(null)
  const [saving, setSaving] = useState(false)
  const [probe, setProbe] = useState<ProbeState>({})

  const load = useCallback(async () => {
    setLoading(true)
    try {
      setRows(await api.adminModels())
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => { void load() }, [load])

  const openDrawer = (record?: ModelConfig) => {
    setEditing(record ?? null)
    form.resetFields()
    if (record) {
      form.setFieldsValue({
        ...record,
        api_key: '', // 密钥不回显，留空表示不修改
        allowed_param_keys: record.allowed_param_keys ?? [],
        reasoning_effort_options: record.reasoning_effort_options ?? [],
        reasoning_preset: matchPreset(record.reasoning_payload, record.reasoning_effort_options),
        ...Object.fromEntries(JSON_FIELDS.map((k) => [k, JSON.stringify(record[k] ?? {}, null, 2)])),
      })
    } else {
      form.setFieldsValue({
        endpoint_path: '/chat/completions', enabled: true, admin_only: false,
        supports_temperature: true, supports_system_prompt: true, supports_json_mode: false,
        reasoning_mode: 'off', max_concurrency: 8, rpm_limit: 0, tpm_limit: 0,
        request_timeout: 300, max_retries: 3, max_tokens_cap: 0, sort_order: 0,
        default_params: '{\n  "temperature": 0.7\n}', forced_params: '{}',
        reasoning_payload: '{}', reasoning_off_payload: '{}', extra_headers: '{}',
        reasoning_preset: CUSTOM_PRESET, reasoning_effort_options: [], reasoning_default_effort: '',
      })
    }
    setOpen(true)
  }

  // 「默认档位」的候选就是上面填的档位名单，跟着一起变
  const effortOptions = Form.useWatch('reasoning_effort_options', form) as string[] | undefined

  /** 选中预设后，把请求体与档位一起填进表单；选「自定义」则不动已填的内容 */
  const applyPreset = (value: string) => {
    const preset = REASONING_PRESETS.find((p) => p.value === value)
    if (!preset) return
    form.setFieldsValue({
      reasoning_payload: JSON.stringify(preset.payload, null, 2),
      reasoning_off_payload: JSON.stringify(preset.offPayload, null, 2),
      reasoning_effort_options: [...preset.effortOptions],
      reasoning_default_effort: preset.defaultEffort,
    })
  }

  const save = async () => {
    const values = await form.validateFields()
    const payload: Record<string, unknown> = { ...values }

    for (const key of JSON_FIELDS) {
      try {
        payload[key] = JSON.parse((values[key] as string) || '{}')
      } catch {
        message.error(`${key} 不是合法 JSON`)
        return
      }
    }
    // 预设只是填表用的快捷方式，后端只认 payload 与档位列表
    delete payload.reasoning_preset
    // 编辑时留空表示保持原密钥不变
    if (editing && !values.api_key) delete payload.api_key

    setSaving(true)
    try {
      if (editing) {
        await api.updateModel(editing.id, payload)
        message.success('已保存')
      } else {
        await api.createModel(payload)
        message.success('已创建')
      }
      setOpen(false)
      await load()
    } catch { /* 拦截器已提示 */ } finally {
      setSaving(false)
    }
  }

  const runProbe = async (record: ModelConfig) => {
    setProbe((p) => ({ ...p, [record.id]: 'loading' }))
    try {
      const res = await api.probeModel(record.id)
      setProbe((p) => ({
        ...p,
        [record.id]: {
          ok: res.ok,
          text: res.ok
            ? `连通正常，延迟 ${res.latency_ms} ms`
            : `失败：${typeof res.detail === 'string' ? res.detail : JSON.stringify(res.detail)}`,
        },
      }))
    } catch {
      setProbe((p) => ({ ...p, [record.id]: { ok: false, text: '探测请求失败' } }))
    }
  }

  const columns: ColumnsType<ModelConfig> = [
    {
      title: '模型', width: 220,
      render: (_, r) => (
        <Space direction="vertical" size={0}>
          <Space size={6}>
            <Typography.Text strong>{r.display_name}</Typography.Text>
            {!r.enabled && <Tag>已禁用</Tag>}
            {r.admin_only && <Tag color="gold">仅管理员</Tag>}
          </Space>
          <Typography.Text type="secondary" className="mono">{r.name}</Typography.Text>
        </Space>
      ),
    },
    {
      title: '端点', width: 300, ellipsis: true,
      render: (_, r) => (
        <Space direction="vertical" size={0} style={{ maxWidth: 300 }}>
          <Typography.Text className="mono" ellipsis>{r.base_url}{r.endpoint_path}</Typography.Text>
          <Typography.Text type="secondary" className="mono">
            model={r.model_name}{r.api_key_masked ? ` · key=${r.api_key_masked}` : ' · 无密钥'}
          </Typography.Text>
        </Space>
      ),
    },
    {
      title: '并发 / 限流', width: 150,
      render: (_, r) => (
        <Space direction="vertical" size={0}>
          <span>并发 {r.max_concurrency}</span>
          <Typography.Text type="secondary">
            {r.rpm_limit ? `${r.rpm_limit} RPM` : '不限 RPM'}
            {r.tpm_limit ? ` · ${r.tpm_limit} TPM` : ''}
          </Typography.Text>
        </Space>
      ),
    },
    {
      title: '开关', width: 180,
      render: (_, r) => (
        <Space size={4} wrap>
          {r.supports_system_prompt && <Tag>系统提示</Tag>}
          {r.supports_json_mode && <Tag color="blue">JSON</Tag>}
          {r.reasoning_mode !== 'off' && (
            <Tag color="purple">推理{r.reasoning_mode === 'forced' ? '(强制)' : '(可选)'}</Tag>
          )}
          {!!r.reasoning_effort_options?.length && (
            <Tag color="purple">档位 {r.reasoning_effort_options.join('/')}</Tag>
          )}
          {!r.supports_temperature && <Tag color="default">无 temperature</Tag>}
        </Space>
      ),
    },
    {
      title: '连通性', width: 200,
      render: (_, r) => {
        const state = probe[r.id]
        return (
          <Space direction="vertical" size={2}>
            <Button size="small" icon={<ThunderboltOutlined />} loading={state === 'loading'}
              onClick={() => void runProbe(r)}>测试</Button>
            {state && state !== 'loading' && (
              <Tooltip title={state.text}>
                <Typography.Text type={state.ok ? 'success' : 'danger'}
                  style={{ fontSize: 12, maxWidth: 190, display: 'block' }} ellipsis>
                  {state.text}
                </Typography.Text>
              </Tooltip>
            )}
          </Space>
        )
      },
    },
    {
      title: '操作', width: 130, fixed: 'right',
      render: (_, r) => (
        <Space>
          <Button size="small" icon={<EditOutlined />} onClick={() => openDrawer(r)}>编辑</Button>
          <Button size="small" danger icon={<DeleteOutlined />} onClick={() => modal.confirm({
            title: `删除模型「${r.display_name}」？`,
            content: '已使用该模型的历史任务不会被删除，但将失去模型关联。',
            okText: '删除', okButtonProps: { danger: true }, cancelText: '取消',
            onOk: async () => { await api.deleteModel(r.id); message.success('已删除'); await load() },
          })} />
        </Space>
      ),
    },
  ]

  return (
    <Card
      title="模型配置"
      extra={<Button type="primary" icon={<PlusOutlined />} onClick={() => openDrawer()}>新增模型</Button>}
    >
      <Alert
        type="info" showIcon style={{ marginBottom: 16 }}
        message="所有模型都通过 OpenAI 兼容协议调用"
        description="base_url 填到 /v1 为止（例 http://127.0.0.1:8000/v1），vLLM、Ollama、SGLang 等网关均适用。"
      />
      <Table rowKey="id" loading={loading} columns={columns} dataSource={rows} scroll={{ x: 1200 }}
        pagination={false} />

      <Drawer
        title={editing ? `编辑：${editing.display_name}` : '新增模型'}
        open={open} onClose={() => setOpen(false)} width={720}
        extra={<Space>
          <Button onClick={() => setOpen(false)}>取消</Button>
          <Button type="primary" loading={saving} onClick={() => void save()}>保存</Button>
        </Space>}
      >
        <Form form={form} layout="vertical">
          <Row gutter={16}>
            <Col span={12}>
              <Form.Item name="name" label="唯一标识"
                rules={[{ required: true, message: '请填写唯一标识' }]}
                extra="创建后不可修改，用于内部识别">
                <Input placeholder="qwen3-32b" disabled={!!editing} className="mono" />
              </Form.Item>
            </Col>
            <Col span={12}>
              <Form.Item name="display_name" label="展示名称"
                rules={[{ required: true, message: '请填写展示名称' }]}>
                <Input placeholder="Qwen3 32B（内网）" />
              </Form.Item>
            </Col>
          </Row>
          <Form.Item name="description" label="描述"><Input placeholder="可留空" /></Form.Item>

          <Row gutter={16}>
            <Col span={14}>
              <Form.Item name="base_url" label="base_url"
                rules={[{ required: true, message: '请填写 base_url' }]}>
                <Input placeholder="http://127.0.0.1:8000/v1" className="mono" />
              </Form.Item>
            </Col>
            <Col span={10}>
              <Form.Item name="endpoint_path" label="端点路径">
                <Input className="mono" />
              </Form.Item>
            </Col>
          </Row>
          <Row gutter={16}>
            <Col span={12}>
              <Form.Item name="model_name" label="model 参数"
                rules={[{ required: true, message: '请填写下发给端点的 model 名' }]}>
                <Input placeholder="qwen3-32b" className="mono" />
              </Form.Item>
            </Col>
            <Col span={12}>
              <Form.Item name="api_key" label="API Key"
                extra={editing ? '留空表示不修改；填 - 一个空格再保存可清除' : undefined}>
                <Input.Password placeholder={editing ? '不修改请留空' : 'sk-…'} className="mono" />
              </Form.Item>
            </Col>
          </Row>

          <Row gutter={16}>
            <Col span={6}><Form.Item name="enabled" label="启用" valuePropName="checked"><Switch /></Form.Item></Col>
            <Col span={6}><Form.Item name="admin_only" label="仅管理员" valuePropName="checked"><Switch /></Form.Item></Col>
            <Col span={6}><Form.Item name="supports_system_prompt" label="系统提示词" valuePropName="checked"><Switch /></Form.Item></Col>
            <Col span={6}><Form.Item name="supports_temperature" label="temperature" valuePropName="checked"><Switch /></Form.Item></Col>
          </Row>
          <Row gutter={16}>
            <Col span={6}><Form.Item name="supports_json_mode" label="JSON 模式" valuePropName="checked"><Switch /></Form.Item></Col>
            <Col span={6}><Form.Item name="supports_tools" label="工具调用" valuePropName="checked"><Switch /></Form.Item></Col>
            <Col span={12}>
              <Form.Item name="reasoning_mode" label="深度推理开关">
                <Select options={[
                  { value: 'off', label: '不支持' },
                  { value: 'optional', label: '可选（用户在建任务时决定）' },
                  { value: 'forced', label: '强制开启' },
                ]} />
              </Form.Item>
            </Col>
          </Row>

          <Row gutter={16}>
            <Col span={6}><Form.Item name="max_concurrency" label="最大并发"><InputNumber min={1} max={512} style={{ width: '100%' }} /></Form.Item></Col>
            <Col span={6}><Form.Item name="rpm_limit" label="RPM 限制"><InputNumber min={0} style={{ width: '100%' }} /></Form.Item></Col>
            <Col span={6}><Form.Item name="tpm_limit" label="TPM 限制"><InputNumber min={0} style={{ width: '100%' }} /></Form.Item></Col>
            <Col span={6}><Form.Item name="max_tokens_cap" label="max_tokens 上限"><InputNumber min={0} style={{ width: '100%' }} /></Form.Item></Col>
          </Row>
          <Row gutter={16}>
            <Col span={8}><Form.Item name="request_timeout" label="单请求超时(秒)"><InputNumber min={5} max={3600} style={{ width: '100%' }} /></Form.Item></Col>
            <Col span={8}><Form.Item name="max_retries" label="失败重试次数"><InputNumber min={0} max={10} style={{ width: '100%' }} /></Form.Item></Col>
            <Col span={8}><Form.Item name="sort_order" label="排序权重"><InputNumber style={{ width: '100%' }} /></Form.Item></Col>
          </Row>

          <Collapse size="small" items={[{
            key: 'json',
            label: '高级：默认参数 / 强制参数 / 请求头',
            children: (
              <>
                <Form.Item name="default_params" label="默认参数"
                  extra="用户未指定时使用；用户可覆盖">
                  <Input.TextArea rows={4} className="mono" />
                </Form.Item>
                <Form.Item name="forced_params" label="强制参数"
                  extra="始终覆盖用户传入的同名参数">
                  <Input.TextArea rows={3} className="mono" />
                </Form.Item>
                <Form.Item name="allowed_param_keys" label="允许用户覆盖的参数白名单"
                  extra="留空表示不限制">
                  <Select mode="tags" placeholder="temperature, max_tokens …" />
                </Form.Item>
                <Form.Item name="reasoning_preset" label="推理请求体写法"
                  extra="选一个常见写法自动填好下面几项，也可以选「自定义」自己写">
                  <Select options={PRESET_SELECT_OPTIONS} onChange={applyPreset} />
                </Form.Item>
                <Form.Item name="reasoning_payload" label="开启推理时附加的请求体"
                  extra='例：{"chat_template_kwargs": {"enable_thinking": true}}；写 "$effort" 的位置会被换成用户选的档位'>
                  <Input.TextArea rows={3} className="mono" />
                </Form.Item>
                <Form.Item name="reasoning_off_payload" label="关闭推理时附加的请求体"
                  extra='推理「不支持」，或「可选」但用户没打开时附加，强制开启时不附加。例：{"chat_template_kwargs": {"enable_thinking": false}}'>
                  <Input.TextArea rows={3} className="mono" />
                </Form.Item>
                <Row gutter={16}>
                  <Col span={16}>
                    <Form.Item name="reasoning_effort_options" label="用户可选的推理档位"
                      extra="留空则不让用户选，请求体原样发出。列表里没有的档位可以直接输入">
                      <Select mode="tags" options={EFFORT_LEVEL_OPTIONS}
                        placeholder="low, medium, high" />
                    </Form.Item>
                  </Col>
                  <Col span={8}>
                    <Form.Item name="reasoning_default_effort" label="默认档位"
                      extra="留空则用名单第一项">
                      <Select allowClear placeholder="不指定"
                        options={(effortOptions ?? []).map((v: string) => ({ value: v, label: v }))} />
                    </Form.Item>
                  </Col>
                </Row>
                <Form.Item name="extra_headers" label="额外请求头">
                  <Input.TextArea rows={3} className="mono" />
                </Form.Item>
              </>
            ),
          }]} />
          <div style={{ marginTop: 16, opacity: 0.6 }}>
            <ApiOutlined /> 保存后可在列表点「测试」验证端点连通性。
          </div>
        </Form>
      </Drawer>
    </Card>
  )
}
