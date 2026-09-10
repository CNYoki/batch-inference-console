import { useCallback, useEffect, useMemo, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import {
  Alert, App, Button, Card, Col, Collapse, Descriptions, Divider, Form, Input, InputNumber,
  Modal, Progress, Row, Select, Slider, Space, Spin, Steps, Switch, Tag, Typography, Upload,
} from 'antd'
import { InboxOutlined, KeyOutlined, ReloadOutlined } from '@ant-design/icons'
import type { UploadFile } from 'antd/es/upload/interface'
import { api } from '../api'
import type {
  DryRunResult, ModelOption, ModelOptions, MyUsage, PersonalReasoningCap, SystemSettings,
  UploadResult,
} from '../api'
import { formatBytes, formatNumber } from '../utils'
import { defaultEffort, sortEfforts } from '../reasoning'
import { buildModelGroups, filterModelOption, parseSelection } from '../modelSelect'

const SAMPLE = `{"custom_id": "req-1", "body": {"messages": [{"role": "user", "content": "把这句话翻译成英文：珞珈山下，清风徐来。"}]}}
{"custom_id": "req-2", "messages": [{"role": "user", "content": "总结这段文字……"}]}
{"custom_id": "req-3", "prompt": "写一首春天的诗"}`

type JobPayload = Parameters<typeof api.createJob>[0]

/** 试跑弹窗的状态机：跑 → 建任务，中间任何一步失败都停在 failed 让用户决定 */
type DryState = {
  phase: 'running' | 'submitting' | 'failed'
  job: JobPayload
  error?: string
  result?: DryRunResult
}

/** 后端 4xx 的 detail，拿不到就返回 undefined */
function errorDetail(err: unknown): string | undefined {
  const detail = (err as { response?: { data?: { detail?: unknown } } })?.response?.data?.detail
  return typeof detail === 'string' ? detail : undefined
}

/**
 * 个人模型拿不到能力声明，采样参数按最宽松处理，由网关自己拒绝不支持的；
 * 推理部分则用后端按模型名规则算好的 cap，拿不到才退回网关默认配置。
 */
function personalCaps(settings: SystemSettings | null, cap?: PersonalReasoningCap) {
  const fallbackMode = (settings?.user_gateway_reasoning_enabled ?? true) ? 'optional' : 'off'
  return {
    supports_temperature: true,
    supports_system_prompt: true,
    supports_json_mode: true,
    // 推理开关默认可切换、默认关闭；管理员可以整体关掉，也可以按模型名关掉
    reasoning_mode: (cap?.reasoning_mode ?? fallbackMode) as 'off' | 'optional',
    reasoning_effort_options:
      cap?.effort_options ?? settings?.user_gateway_reasoning_effort_options ?? [],
    reasoning_default_effort:
      cap?.default_effort ?? settings?.user_gateway_reasoning_default_effort ?? '',
    max_concurrency: settings?.user_gateway_max_concurrency ?? 0,
    max_tokens_cap: settings?.user_gateway_max_tokens_cap ?? 0,
  }
}

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

export default function NewJobPage() {
  const navigate = useNavigate()
  const { message } = App.useApp()
  const [form] = Form.useForm()

  const [options, setOptions] = useState<ModelOptions | null>(null)
  const [settings, setSettings] = useState<SystemSettings | null>(null)
  const [usage, setUsage] = useState<MyUsage | null>(null)
  const [fileList, setFileList] = useState<UploadFile[]>([])
  const [uploading, setUploading] = useState(false)
  const [percent, setPercent] = useState(0)
  const [upload, setUpload] = useState<UploadResult | null>(null)
  const [submitting, setSubmitting] = useState(false)
  const [dry, setDry] = useState<DryState | null>(null)

  // 本次会话里刚填的 token（尚未保存时，提交任务要一起带上）
  const [token, setToken] = useState('')
  const [tokenLoading, setTokenLoading] = useState(false)
  const [rememberToken, setRememberToken] = useState(true)
  const [editingToken, setEditingToken] = useState(false)

  const selectedValue = Form.useWatch('model', form) as string | undefined
  const selection = useMemo(() => parseSelection(selectedValue), [selectedValue])
  const reasoningOn = Form.useWatch('reasoning', form) as boolean | undefined

  const loadOptions = useCallback(async () => {
    const data = await api.modelOptions()
    setOptions(data)
    setEditingToken(!data.has_saved_token)
    return data
  }, [])

  useEffect(() => {
    loadOptions().catch(() => undefined)
    api.settings().then(setSettings).catch(() => undefined)
    api.usage().then(setUsage).catch(() => undefined)
  }, [loadOptions])

  const sharedModel: ModelOption | undefined = useMemo(
    () => (selection?.source === 'shared'
      ? options?.shared.find((m) => m.id === selection.key)
      : undefined),
    [options, selection],
  )
  // 公用模型用它自己的能力声明，个人模型用网关按模型名算出来的那份
  const caps = selection?.source === 'personal'
    ? personalCaps(settings, options?.personal_reasoning?.[selection.key])
    : sharedModel

  const effortOptions = useMemo(
    () => sortEfforts(caps?.reasoning_effort_options ?? []), [caps],
  )
  const effortFallback = defaultEffort(effortOptions, caps?.reasoning_default_effort)
  useEffect(() => {
    // 换模型后档位名单可能完全不同，统一回到该模型的默认档
    form.setFieldValue('reasoning_effort', effortFallback)
  }, [form, effortFallback])

  const fetchPersonalModels = async () => {
    if (!token.trim()) {
      message.warning('请先填写你的网关 token')
      return
    }
    setTokenLoading(true)
    try {
      const res = await api.personalModels(token.trim(), rememberToken)
      message.success(`拉取到 ${res.models.length} 个可用模型`)
      setOptions((prev) => prev && {
        ...prev, personal: res.models, personal_error: null,
        personal_reasoning: res.reasoning ?? {},
        has_saved_token: prev.has_saved_token || res.saved,
      })
      setEditingToken(false)
    } catch {
      // 具体原因（token 无效/网关不可达）由拦截器弹出
    } finally {
      setTokenLoading(false)
    }
  }

  const clearToken = async () => {
    await api.clearPersonalToken()
    setToken('')
    message.success('已清除保存的 token')
    await loadOptions()
  }

  const handleUpload = async (file: File) => {
    setUploading(true)
    setPercent(0)
    setUpload(null)
    try {
      const res = await api.uploadJsonl(file, setPercent)
      setUpload(res)
      api.usage().then(setUsage).catch(() => undefined)
      if (res.errors.length) {
        message.warning('文件存在格式问题，请修正后重新上传')
      } else {
        message.success(`解析成功，共 ${formatNumber(res.total_items)} 条`)
        if (!form.getFieldValue('name')) {
          form.setFieldValue('name', file.name.replace(/\.(jsonl|ndjson|json|txt)$/i, ''))
        }
      }
    } catch {
      setFileList([])
    } finally {
      setUploading(false)
    }
  }

  /** 真正建任务；试跑通过后自动走这里，试跑失败时用户点「仍要提交」也走这里 */
  const submitJob = async (job: JobPayload) => {
    setSubmitting(true)
    try {
      const created = await api.createJob(job)
      setDry(null)
      message.success('任务已提交，正在排队')
      navigate(`/jobs/${created.id}`)
    } catch {
      // 失败原因由拦截器提示，弹窗收掉让用户回去改
      setDry(null)
    } finally {
      setSubmitting(false)
    }
  }

  /**
   * 提交前先拿第一条真发一次。整批排完队再报错太亏，
   * 模型名、密钥、推理字段、max_tokens 这些坑在这一步就能暴露。
   */
  const onSubmit = async () => {
    const values = await form.validateFields()
    if (!upload || upload.errors.length) {
      message.error('请先上传一个格式正确的 JSONL 文件')
      return
    }
    const picked = parseSelection(values.model)
    if (!picked) {
      message.error('请选择一个模型')
      return
    }

    let extra: Record<string, unknown>
    try {
      extra = values.extra ? JSON.parse(values.extra) : {}
    } catch {
      message.error('「附加参数」不是合法 JSON')
      return
    }

    const job: JobPayload = {
      name: values.name,
      upload_id: upload.upload_id,
      model_source: picked.source,
      model_config_id: picked.source === 'shared' ? picked.key : null,
      personal_model: picked.source === 'personal' ? picked.key : null,
      // token 已保存过就不必再传；刚填的则随任务带上
      personal_token: picked.source === 'personal' && token.trim() ? token.trim() : null,
      remember_token: rememberToken,
      concurrency: values.concurrency ?? 0,
      priority: values.priority ?? 100,
      params: {
        system_prompt: values.system_prompt || null,
        temperature: values.temperature,
        top_p: values.top_p,
        max_tokens: values.max_tokens,
        seed: values.seed,
        json_mode: !!values.json_mode,
        reasoning: !!values.reasoning,
        reasoning_effort: values.reasoning_effort || null,
        extra,
      },
    }

    setDry({ phase: 'running', job })
    try {
      // 试跑用的就是这份 job 里的模型与参数，只是不带 name / 并发 / 优先级
      const res = await api.dryRunJob(job)
      if (!res.ok) {
        setDry({ phase: 'failed', job, error: res.error || '试跑失败', result: res })
        return
      }
      setDry({ phase: 'submitting', job, result: res })
      await submitJob(job)
    } catch (err) {
      setDry({
        phase: 'failed', job,
        error: errorDetail(err) || '试跑请求没能发出去，请检查网络或后端服务',
      })
    }
  }

  const modelGroups = useMemo(() => buildModelGroups(options), [options])

  const step = !upload || upload.errors.length ? 0 : selection ? 2 : 1

  return (
    <Space direction="vertical" size={16} style={{ width: '100%' }}>
      <Card>
        <Steps current={step} items={[{ title: '上传数据' }, { title: '选择模型' }, { title: '配置并提交' }]} />
      </Card>

      <Row gutter={16}>
        <Col xs={24} lg={10}>
          <Card title="1. 上传 JSONL 文件">
            <Upload.Dragger
              accept=".jsonl,.ndjson,.json,.txt"
              maxCount={1}
              fileList={fileList}
              disabled={uploading}
              beforeUpload={(file) => {
                setFileList([{ uid: file.name, name: file.name, status: 'done' }])
                void handleUpload(file)
                return false // 交给自定义逻辑上传，阻止 antd 默认行为
              }}
              onRemove={() => { setFileList([]); setUpload(null); return true }}
            >
              <p className="ant-upload-drag-icon"><InboxOutlined /></p>
              <p className="ant-upload-text">点击或拖拽文件到此处上传</p>
              <p className="ant-upload-hint">
                每行一个 JSON 对象；单文件最大 {settings?.max_upload_mb ?? 512} MB，
                最多 {formatNumber(settings?.max_items_per_job ?? 500000)} 条
              </p>
            </Upload.Dragger>

            {usage && !usage.storage.unlimited && (
              <div style={{ marginTop: 16 }}>
                <Space style={{ width: '100%', justifyContent: 'space-between' }}>
                  <Typography.Text type="secondary" style={{ fontSize: 12 }}>我的存储空间</Typography.Text>
                  <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                    {formatBytes(usage.storage.used)} / {formatBytes(usage.storage.limit)}
                  </Typography.Text>
                </Space>
                <Progress
                  percent={usage.storage.percent}
                  size="small"
                  status={usage.storage.percent >= 95 ? 'exception'
                    : usage.storage.percent >= 80 ? 'active' : 'normal'}
                />
                {usage.storage.percent >= 90 && (
                  <Typography.Text type="danger" style={{ fontSize: 12 }}>
                    空间即将用满，可以删除不再需要的历史任务来释放。
                  </Typography.Text>
                )}
              </div>
            )}

            {uploading && <Progress percent={percent} style={{ marginTop: 16 }} />}

            {upload && (
              <div style={{ marginTop: 16 }}>
                <Descriptions size="small" column={1} bordered>
                  <Descriptions.Item label="文件">{upload.filename}</Descriptions.Item>
                  <Descriptions.Item label="大小">{formatBytes(upload.size)}</Descriptions.Item>
                  <Descriptions.Item label="条目数">{formatNumber(upload.total_items)}</Descriptions.Item>
                </Descriptions>

                {upload.errors.length > 0 && (
                  <Alert
                    style={{ marginTop: 12 }} type="error" showIcon
                    message={`发现 ${upload.errors.length} 处格式问题`}
                    description={
                      <ul style={{ paddingLeft: 18, margin: 0 }}>
                        {upload.errors.slice(0, 8).map((e) => <li key={e}>{e}</li>)}
                      </ul>
                    }
                  />
                )}
                {upload.duplicate_custom_ids.length > 0 && (
                  <Alert
                    style={{ marginTop: 12 }} type="warning" showIcon
                    message="存在重复的 custom_id"
                    description={`请检查上传文件，例如：${upload.duplicate_custom_ids.slice(0, 5).join('、')}`}
                  />
                )}
                {upload.preview.length > 0 && upload.errors.length === 0 && (
                  <Collapse
                    style={{ marginTop: 12 }}
                    size="small"
                    items={[{
                      key: 'preview',
                      label: '预览解析结果（前 3 条）',
                      children: (
                        <pre className="mono pre-wrap" style={{ margin: 0, maxHeight: 220, overflow: 'auto' }}>
                          {upload.preview.map((p) => JSON.stringify(p, null, 2)).join('\n')}
                        </pre>
                      ),
                    }]}
                  />
                )}
              </div>
            )}

            <Divider plain>支持的格式</Divider>
            <pre className="mono pre-wrap" style={{ margin: 0, fontSize: 11, opacity: 0.75 }}>{SAMPLE}</pre>
          </Card>
        </Col>

        <Col xs={24} lg={14}>
          <Form form={form} layout="vertical" initialValues={{ priority: 100, concurrency: 0 }}>
            <Card title="2. 选择模型" style={{ marginBottom: 16 }}>
              {options?.gateway_enabled && (
                <>
                  {options.personal_error && (
                    <Alert
                      type="warning" showIcon style={{ marginBottom: 12 }}
                      message="个人模型列表拉取失败"
                      description={<>
                        {options.personal_error}
                        <br />请重新填写你的 token。
                      </>}
                    />
                  )}

                  {options.has_saved_token && !editingToken ? (
                    <Alert
                      type="success" showIcon style={{ marginBottom: 12 }}
                      message={`已保存你的 ${options.gateway_label} token，拉取到 ${options.personal.length} 个可用模型`}
                      action={
                        <Space>
                          <Button size="small" onClick={() => { setToken(''); setEditingToken(true) }}>
                            更换
                          </Button>
                          <Button size="small" danger onClick={() => void clearToken()}>清除</Button>
                        </Space>
                      }
                    />
                  ) : (
                    <div style={{ marginBottom: 12 }}>
                      <Typography.Text strong>{options.gateway_label} token</Typography.Text>
                      {/* <Typography.Paragraph type="secondary" style={{ marginBottom: 8, fontSize: 12 }}>
                        填写你本人的 token，系统会去 <span className="mono">{options.gateway_base_url}</span> 拉取
                        你有权限的模型。
                      </Typography.Paragraph> */}
                      <Space.Compact style={{ width: '100%' }}>
                        <Input.Password
                          prefix={<KeyOutlined />}
                          placeholder="sk-…"
                          value={token}
                          onChange={(e) => setToken(e.target.value)}
                          onPressEnter={() => void fetchPersonalModels()}
                          className="mono"
                        />
                        <Button type="primary" icon={<ReloadOutlined />} loading={tokenLoading}
                          onClick={() => void fetchPersonalModels()}>
                          拉取我的模型
                        </Button>
                      </Space.Compact>
                      <Space style={{ marginTop: 8 }}>
                        <Switch size="small" checked={rememberToken} onChange={setRememberToken} />
                        <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                          记住 token
                        </Typography.Text>
                      </Space>
                    </div>
                  )}
                </>
              )}

              <Form.Item name="model" label="模型" rules={[{ required: true, message: '请选择模型' }]}
                style={{ marginBottom: 0 }}>
                <Select
                  showSearch
                  placeholder={
                    modelGroups.length ? '选择可用模型' : '暂无可用模型'
                  }
                  options={modelGroups}
                  optionFilterProp="name"
                  filterOption={filterModelOption}
                />
              </Form.Item>

              {selection?.source === 'personal' && (
                <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                  该任务将以你的 token 调用 {options?.gateway_label}，并发上限
                  {settings?.user_gateway_max_concurrency ?? 4}。
                </Typography.Text>
              )}
              {sharedModel?.description && (
                <Alert type="info" showIcon style={{ marginTop: 12 }} message={sharedModel.description} />
              )}
            </Card>

            <Card title="3. 任务与推理参数">
              <Form.Item name="name" label="任务名称" rules={[{ required: true, message: '请填写任务名称' }]}>
                <Input placeholder="例：数据标注-chunk1" maxLength={255} />
              </Form.Item>

              {caps?.supports_system_prompt !== false && (
                <Form.Item name="system_prompt" label="System Prompt（可选）"
                  extra="会作为 system 消息注入到每一条请求；不覆盖数据中的 system 消息。">
                  <Input.TextArea rows={3} maxLength={20000} showCount
                    placeholder="例：你是一名严谨的数据标注员，只输出 JSON。" />
                </Form.Item>
              )}

              <Row gutter={16}>
                {caps?.supports_temperature !== false && (
                  <Col span={12}>
                    <Form.Item name="temperature" label="temperature">
                      <Slider min={0} max={2} step={0.1} marks={{ 0: '0', 1: '1', 2: '2' }} />
                    </Form.Item>
                  </Col>
                )}
                <Col span={12}>
                  <Form.Item name="max_tokens" label="max_tokens"
                    extra={caps?.max_tokens_cap ? `上限 ${formatNumber(caps.max_tokens_cap)}` : undefined}>
                    <InputNumber min={1} max={caps?.max_tokens_cap || 200000}
                      style={{ width: '100%' }} placeholder="留空则使用模型默认值" />
                  </Form.Item>
                </Col>
              </Row>

              <Row gutter={16}>
                <Col span={8}>
                  <Form.Item name="top_p" label="top_p">
                    <InputNumber min={0} max={1} step={0.05} style={{ width: '100%' }} placeholder="可选" />
                  </Form.Item>
                </Col>
                <Col span={8}>
                  <Form.Item name="seed" label="seed">
                    <InputNumber style={{ width: '100%' }} placeholder="可选" />
                  </Form.Item>
                </Col>
                <Col span={8}>
                  <Form.Item name="concurrency" label="并发数" extra="0 表示按模型配置">
                    <InputNumber min={0} max={sharedModel?.max_concurrency ?? 64}
                      style={{ width: '100%' }} />
                  </Form.Item>
                </Col>
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
                      <Form.Item name="priority" label="优先级" extra="数值越小优先级越大">
                        <InputNumber min={0} max={1000} style={{ width: 160 }} />
                      </Form.Item>
                      <Form.Item name="extra" label="附加请求参数 (JSON)"
                        extra="会合并进每条请求体，例如 {&quot;response_format&quot;: {&quot;type&quot;: &quot;json_object&quot;}}">
                        <Input.TextArea rows={3} placeholder="{}" className="mono" />
                      </Form.Item>
                    </>
                  ),
                }]}
              />

              <Divider />
              <Space>
                <Button type="primary" size="large" loading={submitting} onClick={() => void onSubmit()}
                  disabled={!upload || upload.errors.length > 0}>
                  提交任务
                </Button>
                <Button size="large" onClick={() => navigate('/jobs')}>取消</Button>
                {upload && upload.errors.length === 0 && (
                  <Typography.Text type="secondary">
                    将处理 {formatNumber(upload.total_items)} 条数据
                  </Typography.Text>
                )}
              </Space>
            </Card>
          </Form>
        </Col>
      </Row>

      <Modal
        open={!!dry}
        title="提交前试跑"
        width={720}
        maskClosable={false}
        // 跑的过程中不给关，免得任务提交到一半界面已经跳走
        closable={dry?.phase === 'failed'}
        onCancel={() => setDry(null)}
        footer={dry?.phase === 'failed' ? [
          <Button key="back" type="primary" onClick={() => setDry(null)}>返回修改</Button>,
          <Button key="force" danger loading={submitting}
            onClick={() => dry && void submitJob(dry.job)}>
            仍要提交
          </Button>,
        ] : null}
      >
        {dry?.phase !== 'failed' ? (
          <Space direction="vertical" size={12} style={{ width: '100%', padding: '8px 0' }}>
            <Space size={10}>
              <Spin size="small" />
              {dry?.phase === 'submitting'
                ? '试跑通过，正在提交任务…'
                : '正在用你选的模型真发第 1 条，通过了才会提交任务'}
            </Space>
            {dry?.result?.content && (
              <Alert
                type="success" showIcon
                message={`模型已回复（${dry.result.latency_ms} ms）`}
                description={
                  <div style={{ maxHeight: 160, overflow: 'auto', whiteSpace: 'pre-wrap' }}>
                    {dry.result.content}
                  </div>
                }
              />
            )}
          </Space>
        ) : (
          <Space direction="vertical" size={12} style={{ width: '100%' }}>
            <Alert type="error" showIcon message="试跑没通过，任务还没有提交"
              description={dry.error} />
            {dry.result && (
              <Descriptions size="small" column={2} bordered>
                <Descriptions.Item label="试的是哪条">
                  {dry.result.custom_id || `第 ${dry.result.item_index + 1} 条`}
                </Descriptions.Item>
                <Descriptions.Item label="HTTP 状态">
                  {dry.result.status_code ?? '未收到响应'}
                </Descriptions.Item>
              </Descriptions>
            )}
            {dry.result && (
              <Collapse size="small" items={[
                {
                  key: 'req',
                  label: '实际发出的请求体',
                  children: (
                    <pre className="mono" style={{ margin: 0, maxHeight: 240, overflow: 'auto' }}>
                      {JSON.stringify(dry.result.request_body, null, 2)}
                    </pre>
                  ),
                },
                ...(dry.result.response ? [{
                  key: 'resp',
                  label: '网关原始响应',
                  children: (
                    <pre className="mono" style={{ margin: 0, maxHeight: 240, overflow: 'auto' }}>
                      {JSON.stringify(dry.result.response, null, 2)}
                    </pre>
                  ),
                }] : []),
              ]} />
            )}
            <Typography.Text type="secondary">
              改完参数再点一次「提交任务」会重新试跑。如果只是网关抖了一下，也可以直接「仍要提交」。
            </Typography.Text>
          </Space>
        )}
      </Modal>
    </Space>
  )
}
