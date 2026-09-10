import { useCallback, useEffect, useState } from 'react'
import {
  Alert, App, Badge, Button, Card, Col, Collapse, Descriptions, Divider, Form, Input, InputNumber,
  Progress, Row, Select, Space, Statistic, Switch, Table, Tag, Tooltip, Typography,
} from 'antd'
import type { ColumnsType } from 'antd/es/table'
import { PauseCircleOutlined, ReloadOutlined } from '@ant-design/icons'
import { api } from '../api'
import type {
  Dashboard, MailTestResult, ModelJobLimit, MyUsage, RetentionPreview, SystemSettings, WorkerInfo,
} from '../api'
import { usePolling } from '../hooks/usePolling'
import {
  EFFORT_LEVEL_OPTIONS, PRESET_SELECT_OPTIONS, REASONING_PRESETS, matchPreset,
} from '../reasoning'
import { STATUS_LABEL, formatBytes, formatNumber } from '../utils'

type UsageRow = {
  user_id: string; username: string; job_count: number; items: number
  prompt_tokens: number; completion_tokens: number; storage_bytes: number
}

/** 推理规则在表单里的形态：payload 是 JSON 文本，preset 只用于填表 */
type RuleForm = {
  pattern: string
  enabled?: boolean
  payload?: string
  off_payload?: string
  effort_options?: string[]
  default_effort?: string
  preset?: string
}

/** 表单里的 JSON 文本框：空串按 {} 处理，非法 JSON 抛出让上层提示 */
function parseJson(text: unknown): Record<string, unknown> {
  if (typeof text !== 'string' || !text.trim()) return {}
  return JSON.parse(text) as Record<string, unknown>
}

export default function AdminSystemPage() {
  const { message, modal } = App.useApp()
  const [form] = Form.useForm()
  const [dash, setDash] = useState<Dashboard | null>(null)
  const [usage, setUsage] = useState<UsageRow[]>([])
  const [saving, setSaving] = useState(false)
  const [settings, setSettings] = useState<SystemSettings | null>(null)
  const [retention, setRetention] = useState<RetentionPreview | null>(null)
  const [storage, setStorage] = useState<MyUsage | null>(null)
  const [mailTo, setMailTo] = useState('')
  const [mailTesting, setMailTesting] = useState(false)
  const [mailResult, setMailResult] = useState<MailTestResult | null>(null)
  const [purging, setPurging] = useState(false)
  const [pausingAll, setPausingAll] = useState(false)

  const load = useCallback(async () => {
    const [d, u] = await Promise.all([
      api.dashboard().catch(() => null),
      api.userUsage().catch(() => [] as UsageRow[]),
    ])
    if (d) setDash(d)
    setUsage(u)
    await api.retention().then(setRetention).catch(() => undefined)
    await api.usage().then(setStorage).catch(() => undefined)
  }, [])

  useEffect(() => {
    void load()
    api.settings().then((s) => {
      setSettings(s)
      form.setFieldsValue({
        ...s,
        smtp_password: '',
        user_gateway_reasoning_payload: JSON.stringify(s.user_gateway_reasoning_payload ?? {}, null, 2),
        user_gateway_reasoning_preset: matchPreset(
          s.user_gateway_reasoning_payload, s.user_gateway_reasoning_effort_options,
        ),
        user_gateway_reasoning_rules: (s.user_gateway_reasoning_rules ?? []).map((r) => ({
          ...r,
          payload: JSON.stringify(r.payload ?? {}, null, 2),
          off_payload: JSON.stringify(r.off_payload ?? {}, null, 2),
          preset: matchPreset(r.payload, r.effort_options),
        })),
      })
    }).catch(() => undefined)
  }, [load, form])

  usePolling(() => void load(), 5000)

  // 规则表格里每一行的原始值：payload 在表单里是文本，提交前才转成对象
  const rules = Form.useWatch('user_gateway_reasoning_rules', form) as RuleForm[] | undefined

  /** 某一行选中预设后，把这行的请求体与档位一起填好 */
  const applyRulePreset = (index: number, value: string) => {
    const preset = REASONING_PRESETS.find((p) => p.value === value)
    if (!preset) return
    const next = [...(form.getFieldValue('user_gateway_reasoning_rules') ?? [])]
    next[index] = {
      ...next[index],
      payload: JSON.stringify(preset.payload, null, 2),
      off_payload: JSON.stringify(preset.offPayload, null, 2),
      effort_options: [...preset.effortOptions],
      default_effort: preset.defaultEffort,
    }
    form.setFieldValue('user_gateway_reasoning_rules', next)
  }

  // 「默认档位」的候选就是上面填的档位名单
  const gatewayEfforts = Form.useWatch(
    'user_gateway_reasoning_effort_options', form,
  ) as string[] | undefined

  /** 选中预设后填好请求体与档位；「自定义」保持已填内容不动 */
  const applyPreset = (value: string) => {
    const preset = REASONING_PRESETS.find((p) => p.value === value)
    if (!preset) return
    form.setFieldsValue({
      user_gateway_reasoning_payload: JSON.stringify(preset.payload, null, 2),
      user_gateway_reasoning_effort_options: [...preset.effortOptions],
      user_gateway_reasoning_default_effort: preset.defaultEffort,
    })
  }

  const saveSettings = async () => {
    const values = await form.validateFields()
    setSaving(true)
    try {
      const s = await api.updateSettings({
        allow_new_jobs: values.allow_new_jobs,
        default_priority: values.default_priority,
        announcement: values.announcement ?? '',
        user_gateway_enabled: values.user_gateway_enabled,
        user_gateway_base_url: values.user_gateway_base_url,
        user_gateway_label: values.user_gateway_label,
        user_gateway_max_concurrency: values.user_gateway_max_concurrency,
        user_gateway_timeout: values.user_gateway_timeout,
        user_gateway_max_retries: values.user_gateway_max_retries,
        user_gateway_max_tokens_cap: values.user_gateway_max_tokens_cap,
        file_retention_days: values.file_retention_days,
        purge_input_files: values.purge_input_files,
        stale_upload_hours: values.stale_upload_hours,
        max_total_storage_gb: values.max_total_storage_gb,
        default_user_storage_gb: values.default_user_storage_gb,
        user_gateway_reasoning_enabled: values.user_gateway_reasoning_enabled,
        user_gateway_reasoning_payload: parseJson(values.user_gateway_reasoning_payload),
        user_gateway_reasoning_effort_options: values.user_gateway_reasoning_effort_options ?? [],
        user_gateway_reasoning_default_effort: values.user_gateway_reasoning_default_effort ?? '',
        // preset 只是填表用的，不提交；模型名留空的行直接丢掉
        user_gateway_reasoning_rules: (values.user_gateway_reasoning_rules ?? [])
          .filter((r: RuleForm) => r?.pattern?.trim())
          .map((r: RuleForm) => ({
            pattern: r.pattern.trim(),
            enabled: r.enabled !== false,
            payload: parseJson(r.payload),
            off_payload: parseJson(r.off_payload),
            effort_options: r.effort_options ?? [],
            default_effort: r.default_effort ?? '',
          })),
        model_job_limits: (values.model_job_limits ?? [])
          .filter((r: ModelJobLimit) => r?.pattern?.trim())
          .map((r: ModelJobLimit) => ({
            pattern: r.pattern.trim(),
            max_running_jobs: r.max_running_jobs ?? 0,
          })),
        smtp_enabled: values.smtp_enabled,
        smtp_host: values.smtp_host,
        smtp_port: values.smtp_port,
        smtp_username: values.smtp_username,
        // 只在用户真的输入了新密码时才提交，留空 = 保持原值
        ...(values.smtp_password ? { smtp_password: values.smtp_password } : {}),
        smtp_security: values.smtp_security,
        smtp_from: values.smtp_from,
        smtp_from_name: values.smtp_from_name,
        smtp_timeout: values.smtp_timeout,
        notify_on_success: values.notify_on_success,
        notify_on_failure: values.notify_on_failure,
        notify_on_canceled: values.notify_on_canceled,
        notify_min_items: values.notify_min_items,
        mail_html: values.mail_html,
        mail_subject_template: values.mail_subject_template,
        mail_body_template: values.mail_body_template,
      })
      setSettings(s)
      form.setFieldsValue({
        ...s,
        smtp_password: '',
        user_gateway_reasoning_payload: JSON.stringify(s.user_gateway_reasoning_payload ?? {}, null, 2),
        user_gateway_reasoning_preset: matchPreset(
          s.user_gateway_reasoning_payload, s.user_gateway_reasoning_effort_options,
        ),
        user_gateway_reasoning_rules: (s.user_gateway_reasoning_rules ?? []).map((r) => ({
          ...r,
          payload: JSON.stringify(r.payload ?? {}, null, 2),
          off_payload: JSON.stringify(r.off_payload ?? {}, null, 2),
          preset: matchPreset(r.payload, r.effort_options),
        })),
      })
      message.success('设置已保存')
    } catch (err) {
      if (err instanceof SyntaxError) {
        message.error('推理请求体不是合法 JSON，请检查默认配置与下面的模型规则')
      }
      // 其余错误由拦截器提示
    } finally {
      setSaving(false)
    }
  }

  /** 一键暂停全站排队中与运行中的任务，例如重建镜像前 */
  const pauseAll = () => {
    const running = dash?.jobs_by_status?.running ?? 0
    const queued = dash?.jobs_by_status?.queued ?? 0
    if (!running && !queued) {
      message.info('当前没有排队中或运行中的任务')
      return
    }
    modal.confirm({
      title: '暂停所有任务？',
      content: (
        <>
          将暂停全站 {formatNumber(running)} 个运行中、{formatNumber(queued)} 个排队中的任务（所有用户）。
          运行中的任务会在几秒内停下，已完成的条目不会丢，之后在任务列表里恢复即可接着跑。
          <br />
          之后新提交的任务不受影响；要一并拦住，请在下方「全局设置」里关闭「允许提交新任务」。
        </>
      ),
      okText: '全部暂停', okButtonProps: { danger: true }, cancelText: '取消',
      onOk: async () => {
        setPausingAll(true)
        try {
          const r = await api.pauseAllJobs()
          const parts = [`已暂停 ${r.paused} 个`]
          if (r.signaled) parts.push(`${r.signaled} 个正在收尾，几秒后变为已暂停`)
          if (r.failed) {
            parts.push(`${r.failed} 个失败，可以再点一次`)
            message.warning(parts.join('，'))
          } else {
            message.success(parts.join('，'))
          }
          await load()
        } catch {
          // 拦截器已提示
        } finally {
          setPausingAll(false)
        }
      },
    })
  }

  const workerColumns: ColumnsType<WorkerInfo> = [
    {
      title: 'Worker', dataIndex: 'id',
      render: (id: string, r) => (
        <Space>
          <Badge status={r.draining ? 'warning' : r.alive ? 'processing' : 'default'} />
          <span className="mono">{id}</span>
          {r.draining && r.alive && (
            <Tooltip title="收到退出信号后不再领新任务，正在把手上的任务跑完，跑完自动退出">
              <Tag color="orange">收尾中</Tag>
            </Tooltip>
          )}
          {!r.alive && (
            <Tooltip title="超过 45 秒没有上报心跳，进程可能已崩溃；它持有的任务会在租约到期后自动重排，这条记录 10 分钟后自动清除">
              <Tag color="red">心跳超时</Tag>
            </Tooltip>
          )}
        </Space>
      ),
    },
    {
      title: '数据目录', width: 220, ellipsis: true,
      render: (_, r) => <Typography.Text className="mono" ellipsis>{r.data_dir || '—'}</Typography.Text>,
    },
    {
      title: '在跑任务', width: 130,
      // 心跳超时的 jobs 是进程死前最后一次上报的快照，那些任务早已被回收重排，不能再显示
      render: (_, r) => !r.alive ? <Typography.Text type="secondary">—</Typography.Text> : (
        <>
          {r.jobs.length} / {r.capacity}
          {r.draining && r.jobs.length === 0 && (
            <Typography.Text type="secondary" style={{ fontSize: 12 }}> 即将退出</Typography.Text>
          )}
        </>
      ),
    },
    {
      title: '最近心跳', width: 180,
      render: (_, r) => r.last_seen
        ? new Date(r.last_seen * 1000).toLocaleString('zh-CN', { hour12: false })
        : '—',
    },
  ]

  const usageColumns: ColumnsType<UsageRow> = [
    { title: '用户', dataIndex: 'username' },
    { title: '任务数', dataIndex: 'job_count', width: 100 },
    { title: '已处理条目', dataIndex: 'items', width: 130, render: formatNumber },
    { title: '输入 tokens', dataIndex: 'prompt_tokens', width: 140, render: formatNumber },
    { title: '输出 tokens', dataIndex: 'completion_tokens', width: 140, render: formatNumber },
    {
      title: '任务占用', dataIndex: 'storage_bytes', width: 130,
      render: (v: number) => formatBytes(v || 0),
    },
  ]

  return (
    <Space direction="vertical" size={16} style={{ width: '100%' }}>
      {dash && dash.data_dir_conflict.length > 1 && (
        <Alert
          type="error" showIcon
          message="检测到多个部署共用同一个任务队列"
          description={
            <>
              在线 worker 报告了不同的数据目录，说明有不止一套部署连着同一个 Redis 队列和数据库：
              <ul style={{ paddingLeft: 18, marginBottom: 8 }}>
                {dash.data_dir_conflict.map((d) => <li key={d} className="mono">{d}</li>)}
              </ul>
              任务会被随机分给其中一个 worker，一旦分到看不见输入文件的那一方就会直接失败。
              请让它们共享同一份存储，或给不同部署配置不同的 <span className="mono">QUEUE_KEY_PREFIX</span>。
            </>
          }
        />
      )}
      {dash && !dash.queue.redis_ok && (
        <Alert type="error" showIcon message="Redis 不可用"
          description="任务无法入队或执行，请检查 REDIS_URL 配置与 Redis 服务状态。" />
      )}

      <Row gutter={16}>
        <Col xs={12} md={6}>
          <Card><Statistic title="排队中" value={dash?.queue.queued ?? 0} /></Card>
        </Col>
        <Col xs={12} md={6}>
          <Card><Statistic title="运行中" value={dash?.queue.running ?? 0} /></Card>
        </Col>
        <Col xs={12} md={6}>
          <Card><Statistic
            title="在线 Worker"
            value={dash?.workers.filter((w) => w.alive && !w.draining).length ?? 0}
            suffix={
              <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                / {dash?.workers.length ?? 0}
                {(dash?.workers.filter((w) => w.draining).length ?? 0) > 0 &&
                  `（${dash?.workers.filter((w) => w.draining).length} 个收尾中）`}
              </Typography.Text>
            }
          /></Card>
        </Col>
        <Col xs={12} md={6}>
          <Card><Statistic title="累计处理条目" value={dash?.total_items_processed ?? 0} /></Card>
        </Col>
      </Row>

      <Row gutter={16}>
        <Col xs={24} lg={14}>
          <Card title="Worker 状态"
            extra={
              <Space>
                <Button size="small" danger icon={<PauseCircleOutlined />} loading={pausingAll}
                  onClick={pauseAll}>
                  暂停所有任务
                </Button>
                <Button size="small" icon={<ReloadOutlined />} onClick={() => void load()}>刷新</Button>
              </Space>
            }>
            <Table rowKey="id" size="small" columns={workerColumns} dataSource={dash?.workers ?? []}
              pagination={false}
              locale={{ emptyText: '没有在线的 worker —— 请确认已启动 python -m app.worker.main' }} />
          </Card>
        </Col>
        <Col xs={24} lg={10}>
          <Card title="任务状态分布">
            <Descriptions column={1} size="small" bordered>
              {Object.entries(dash?.jobs_by_status ?? {}).map(([k, v]) => (
                <Descriptions.Item key={k} label={STATUS_LABEL[k as keyof typeof STATUS_LABEL] ?? k}>
                  {formatNumber(v)}
                </Descriptions.Item>
              ))}
              {!dash || Object.keys(dash.jobs_by_status).length === 0 ? (
                <Descriptions.Item label="暂无数据">—</Descriptions.Item>
              ) : null}
            </Descriptions>
          </Card>
        </Col>
      </Row>

      <Card title="个人网关（用户自带 token）">
        <Form form={form} layout="vertical" style={{ maxWidth: 1000 }}>
          <Typography.Paragraph type="secondary">
            开启后，用户可在「新建任务」页填写自己的网关 token，拉取本人有权限的模型并以自己的额度跑任务。
            管理员配置的模型会标注「公用」，两者可以混用。
          </Typography.Paragraph>
          <Space size={40} align="start" wrap>
            <Form.Item name="user_gateway_enabled" label="启用个人网关" valuePropName="checked">
              <Switch />
            </Form.Item>
            <Form.Item name="user_gateway_label" label="网关显示名" extra="展示给用户看的名字">
              <Input style={{ width: 180 }} placeholder="个人网关" />
            </Form.Item>
          </Space>
          <Form.Item
            name="user_gateway_base_url" label="网关地址"
            extra="OpenAI 兼容网关，填到 /v1 为止。模型列表取自 {地址}/models，按 token 权限返回"
            rules={[{ pattern: /^https?:\/\//, message: '必须以 http:// 或 https:// 开头' }]}
          >
            <Input className="mono" placeholder="https://gateway.example.com/v1" style={{ maxWidth: 460 }} />
          </Form.Item>
          <Space size={24} align="start" wrap>
            <Form.Item name="user_gateway_max_concurrency" label="并发上限"
              extra="个人 token 配额通常有限">
              <InputNumber min={1} max={128} style={{ width: 120 }} />
            </Form.Item>
            <Form.Item name="user_gateway_timeout" label="单请求超时(秒)">
              <InputNumber min={5} max={3600} style={{ width: 120 }} />
            </Form.Item>
            <Form.Item name="user_gateway_max_retries" label="重试次数">
              <InputNumber min={0} max={10} style={{ width: 120 }} />
            </Form.Item>
            <Form.Item name="user_gateway_max_tokens_cap" label="max_tokens 上限" extra="0 表示不限">
              <InputNumber min={0} style={{ width: 140 }} />
            </Form.Item>
          </Space>
          <Space size={40} align="start" wrap>
            <Form.Item name="user_gateway_reasoning_enabled" label="允许切换推理开关"
              valuePropName="checked" extra="开关在新建任务页默认关闭，由用户自行打开">
              <Switch />
            </Form.Item>
            <Form.Item name="user_gateway_reasoning_preset" label="推理请求体写法"
              extra="选一个常见写法自动填好右边两项">
              <Select options={PRESET_SELECT_OPTIONS} onChange={applyPreset} style={{ width: 320 }} />
            </Form.Item>
            <Form.Item name="user_gateway_reasoning_payload" label="开启推理时附加的请求参数"
              extra='字段因网关而异；写 "$effort" 的位置会被换成用户选的档位'>
              <Input.TextArea rows={3} className="mono" style={{ width: 380 }} />
            </Form.Item>
            <Form.Item name="user_gateway_reasoning_effort_options" label="用户可选的推理档位"
              extra="留空则不让用户选；列表外的档位可直接输入">
              <Select mode="tags" options={EFFORT_LEVEL_OPTIONS}
                placeholder="low, medium, high" style={{ width: 240 }} />
            </Form.Item>
            <Form.Item name="user_gateway_reasoning_default_effort" label="默认档位"
              extra="留空则用名单第一项">
              <Select allowClear placeholder="不指定" style={{ width: 140 }}
                options={(gatewayEfforts ?? []).map((v: string) => ({ value: v, label: v }))} />
            </Form.Item>
          </Space>

          <Divider orientation="left" plain>按模型名的推理配置</Divider>
          <Typography.Paragraph type="secondary">
            同一个网关上不同系列模型的开法不一样，可以按模型名单独配。
            模型名支持 <code>*</code> 通配（例 <code>qwen3-*</code>），
            <b>按顺序取第一条命中的</b>；都没命中就用上面那份默认配置。
          </Typography.Paragraph>
          <Form.List name="user_gateway_reasoning_rules">
            {(fields, { add, remove }) => (
              <>
                {fields.map(({ key, name, ...rest }) => (
                  <div key={key} style={{
                    border: '1px solid rgba(128,128,128,.25)', borderRadius: 8,
                    padding: '12px 16px 0', marginBottom: 12,
                  }}>
                    <Space size={24} align="start" wrap>
                      <Form.Item {...rest} name={[name, 'pattern']} label="模型名"
                        rules={[{ required: true, message: '填模型名或通配符' }]}>
                        <Input className="mono" placeholder="qwen3-*" style={{ width: 200 }} />
                      </Form.Item>
                      <Form.Item {...rest} name={[name, 'enabled']} label="支持推理"
                        valuePropName="checked"
                        extra="关掉表示这些模型不支持，用户看不到开关；关闭时的参数照样会附加">
                        <Switch />
                      </Form.Item>
                      <Form.Item {...rest} name={[name, 'preset']} label="请求体写法">
                        <Select options={PRESET_SELECT_OPTIONS} style={{ width: 300 }}
                          onChange={(v) => applyRulePreset(name, v)} />
                      </Form.Item>
                      <Form.Item label=" " colon={false}>
                        <Button danger onClick={() => remove(name)}>删除</Button>
                      </Form.Item>
                    </Space>
                    <Space size={24} align="start" wrap>
                      <Form.Item {...rest} name={[name, 'payload']} label="开启推理时附加的请求参数">
                        <Input.TextArea rows={3} className="mono" style={{ width: 380 }}
                          placeholder="{}" />
                      </Form.Item>
                      <Form.Item {...rest} name={[name, 'off_payload']} label="关闭推理时附加的请求参数"
                        extra="用户没打开推理时带上，只对这条规则命中的模型生效；没配规则的模型不带">
                        <Input.TextArea rows={3} className="mono" style={{ width: 380 }}
                          placeholder="{}" />
                      </Form.Item>
                      <Form.Item {...rest} name={[name, 'effort_options']} label="用户可选的推理档位">
                        <Select mode="tags" options={EFFORT_LEVEL_OPTIONS}
                          placeholder="low, medium, high" style={{ width: 240 }} />
                      </Form.Item>
                      <Form.Item {...rest} name={[name, 'default_effort']} label="默认档位">
                        <Select allowClear placeholder="不指定" style={{ width: 140 }}
                          options={(rules?.[name]?.effort_options ?? [])
                            .map((v) => ({ value: v, label: v }))} />
                      </Form.Item>
                    </Space>
                  </div>
                ))}
                <Button type="dashed" onClick={() => add({ enabled: true, payload: '{}', off_payload: '{}' })}
                  style={{ marginBottom: 16 }}>
                  + 添加模型规则
                </Button>
              </>
            )}
          </Form.List>

          <Button type="primary" loading={saving} onClick={() => void saveSettings()}>保存网关设置</Button>
        </Form>
      </Card>

      <Card title="单模型同时运行任务数">
        <Form form={form} layout="vertical" style={{ maxWidth: 720 }}>
          <Typography.Paragraph type="secondary">
            限制同一个模型同时在跑的任务数，所有 worker 合计。按真实模型名匹配：公用模型看配置里的「模型名」，
            个人网关模型看网关里的模型名，两边同名的算同一个模型。支持 <code>*</code> 通配，
            <b>按顺序取第一条命中的</b>，每个模型名单独计数；上限填 0 表示不限。
            超出上限的任务留在队列里等名额，排在后面的其他模型任务照常先跑。
          </Typography.Paragraph>
          <Form.List name="model_job_limits">
            {(fields, { add, remove }) => (
              <>
                {fields.map(({ key, name, ...rest }) => (
                  <Space key={key} size={16} align="start" wrap>
                    <Form.Item {...rest} name={[name, 'pattern']} label="模型名"
                      rules={[{ required: true, whitespace: true, message: '填模型名或通配符' }]}>
                      <Input className="mono" placeholder="qwen3.8-27b" style={{ width: 260 }} />
                    </Form.Item>
                    <Form.Item {...rest} name={[name, 'max_running_jobs']} label="最多同时运行"
                      rules={[{ required: true, message: '填上限' }]}>
                      <InputNumber min={0} max={1000} addonAfter="个任务" style={{ width: 170 }} />
                    </Form.Item>
                    <Form.Item label=" " colon={false}>
                      <Button danger onClick={() => remove(name)}>删除</Button>
                    </Form.Item>
                  </Space>
                ))}
                <div>
                  <Button type="dashed" onClick={() => add({ max_running_jobs: 4 })}
                    style={{ marginBottom: 16 }}>
                    + 添加模型
                  </Button>
                </div>
              </>
            )}
          </Form.List>
          <Button type="primary" loading={saving} onClick={() => void saveSettings()}>保存设置</Button>
        </Form>
      </Card>

      <Card title="全局设置">
        <Form form={form} layout="vertical" style={{ maxWidth: 640 }}>
          <Space size={40} align="start" wrap>
            <Form.Item name="allow_new_jobs" label="允许提交新任务" valuePropName="checked"
              extra="关闭后普通用户无法上传与提交，管理员不受限">
              <Switch />
            </Form.Item>
            <Form.Item name="default_priority" label="默认优先级" extra="数值越小越优先">
              <InputNumber min={0} max={1000} style={{ width: 140 }} />
            </Form.Item>
          </Space>
          <Form.Item name="announcement" label="站内公告" extra="填写后会显示在所有页面顶部；留空则隐藏">
            <Input.TextArea rows={2} maxLength={2000} showCount placeholder="例：今晚 22:00 推理集群维护，任务将暂停" />
          </Form.Item>
          <Space>
            <Button type="primary" loading={saving} onClick={() => void saveSettings()}>保存设置</Button>
            <Typography.Text type="secondary">
              上传上限 {settings?.max_upload_mb ?? '—'} MB · 单任务最多 {formatNumber(settings?.max_items_per_job ?? 0)} 条
            </Typography.Text>
          </Space>
        </Form>
      </Card>

      <Card title="存储空间">
        <Form form={form} layout="vertical" style={{ maxWidth: 720 }}>
          <Typography.Paragraph type="secondary">
            任务的输入与结果文件占用磁盘空间，超过全站上限或单用户上限时无法提交新任务。
          </Typography.Paragraph>
          <Space size={40} align="start" wrap>
            <Form.Item name="max_total_storage_gb" label="全站上限" extra="0 表示不限">
              <InputNumber min={0} style={{ width: 150 }} addonAfter="GB" />
            </Form.Item>
            <Form.Item name="default_user_storage_gb" label="单用户默认上限"
              extra="0 表示不限；可在用户管理里单独覆盖">
              <InputNumber min={0} style={{ width: 170 }} addonAfter="GB" />
            </Form.Item>
            <Form.Item name="stale_upload_hours" label="暂存上传保留"
              extra="0 表示不清理">
              <InputNumber min={0} max={8760} style={{ width: 150 }} addonAfter="小时" />
            </Form.Item>
          </Space>

          {storage?.total && (
            <Descriptions bordered size="small" column={{ xs: 1, sm: 2 }} style={{ marginBottom: 16 }}>
              <Descriptions.Item label="全站已用">
                {formatBytes(storage.total.used)}
                {!storage.total.unlimited && ` / ${formatBytes(storage.total.limit)}`}
              </Descriptions.Item>
              <Descriptions.Item label="其中暂存上传">{formatBytes(storage.total.pending)}</Descriptions.Item>
            </Descriptions>
          )}
          {storage?.total && !storage.total.unlimited && (
            <Progress
              percent={storage.total.percent} style={{ marginBottom: 16 }}
              status={storage.total.percent >= 90 ? 'exception' : 'normal'}
            />
          )}

          <Button type="primary" loading={saving} onClick={() => void saveSettings()}>保存存储设置</Button>
        </Form>
      </Card>

      <Card title="文件保留期">
        <Form form={form} layout="vertical" style={{ maxWidth: 720 }}>
          <Space size={40} align="start" wrap>
            <Form.Item name="file_retention_days" label="最长保留天数" extra="0 表示永久保留">
              <InputNumber min={0} max={3650} style={{ width: 160 }} addonAfter="天" />
            </Form.Item>
            <Form.Item name="purge_input_files" label="同时清除上传的原始文件" valuePropName="checked">
              <Switch />
            </Form.Item>
          </Space>

          {retention && (
            <Descriptions bordered size="small" column={{ xs: 1, sm: 2 }} style={{ marginBottom: 16 }}>
              <Descriptions.Item label="当前策略">
                {retention.retention_days > 0 ? `保留 ${retention.retention_days} 天` : '永久保留（未启用清理）'}
              </Descriptions.Item>
              <Descriptions.Item label="占用空间">
                {formatBytes(retention.tracked_bytes)}（{formatNumber(retention.tracked_jobs)} 个任务）
              </Descriptions.Item>
              <Descriptions.Item label="已达保留期">
                {retention.expiring_jobs > 0 ? (
                  <Typography.Text type="warning">
                    {formatNumber(retention.expiring_jobs)} 个任务 · 可释放 {formatBytes(retention.expiring_bytes)}
                  </Typography.Text>
                ) : '无'}
              </Descriptions.Item>
              <Descriptions.Item label="历史已清理">
                {formatNumber(retention.already_purged_jobs)} 个任务
              </Descriptions.Item>
            </Descriptions>
          )}

          <Space>
            <Button type="primary" loading={saving} onClick={() => void saveSettings()}>保存保留期设置</Button>
            <Button
              danger loading={purging}
              disabled={!retention?.expiring_jobs}
              onClick={() => modal.confirm({
                title: `立即清理 ${retention?.expiring_jobs} 个任务的文件？`,
                content: '这些任务的输入与结果文件会被永久删除，无法恢复。任务记录与统计会保留。',
                okText: '确认清理', okButtonProps: { danger: true }, cancelText: '取消',
                onOk: async () => {
                  setPurging(true)
                  try {
                    const r = await api.runRetention()
                    setRetention(r)
                    message.success('清理完成')
                  } finally {
                    setPurging(false)
                  }
                },
              })}
            >
              立即清理
            </Button>
          </Space>
        </Form>
      </Card>

      <Card title="邮件通知">
        <Form form={form} layout="vertical" style={{ maxWidth: 820 }}>

          <Space size={40} align="start" wrap>
            <Form.Item name="smtp_enabled" label="启用邮件通知" valuePropName="checked">
              <Switch />
            </Form.Item>
            <Form.Item name="smtp_security" label="加密方式"
              extra="587 一般用 STARTTLS，465 用 SSL">
              <Select style={{ width: 160 }} options={[
                { value: 'starttls', label: 'STARTTLS' },
                { value: 'ssl', label: 'SSL/TLS' },
                { value: 'none', label: '不加密（仅内网）' },
              ]} />
            </Form.Item>
            <Form.Item name="smtp_timeout" label="超时(秒)">
              <InputNumber min={3} max={300} style={{ width: 110 }} />
            </Form.Item>
          </Space>

          <Row gutter={16}>
            <Col span={14}>
              <Form.Item name="smtp_host" label="SMTP 服务器">
                <Input className="mono" placeholder="smtp.example.com" />
              </Form.Item>
            </Col>
            <Col span={10}>
              <Form.Item name="smtp_port" label="端口">
                <InputNumber min={1} max={65535} style={{ width: '100%' }} />
              </Form.Item>
            </Col>
          </Row>
          <Row gutter={16}>
            <Col span={12}>
              <Form.Item name="smtp_username" label="用户名"
                extra="留空表示服务器不需要认证">
                <Input className="mono" />
              </Form.Item>
            </Col>
            <Col span={12}>
              <Form.Item name="smtp_password" label="密码 / 授权码"
                extra={settings?.smtp_password_masked
                  ? `当前：${settings.smtp_password_masked}，留空表示不修改`
                  : '加密存储，不会回显'}>
                <Input.Password className="mono" placeholder={settings?.smtp_password_masked ? '不修改请留空' : ''} />
              </Form.Item>
            </Col>
          </Row>
          <Row gutter={16}>
            <Col span={12}>
              <Form.Item name="smtp_from" label="发件人地址">
                <Input className="mono" placeholder="noreply@example.com" />
              </Form.Item>
            </Col>
            <Col span={12}>
              <Form.Item name="smtp_from_name" label="发件人显示名">
                <Input placeholder="批量推理平台" />
              </Form.Item>
            </Col>
          </Row>

          <Divider orientation="left" plain>触发场景</Divider>
          <Space size={32} align="start" wrap>
            <Form.Item name="notify_on_success" label="任务完成" valuePropName="checked">
              <Switch />
            </Form.Item>
            <Form.Item name="notify_on_failure" label="异常终止" valuePropName="checked">
              <Switch />
            </Form.Item>
            <Form.Item name="notify_on_canceled" label="用户取消" valuePropName="checked">
              <Switch />
            </Form.Item>
            <Form.Item name="notify_min_items" label="条目数少于此值不打扰" extra="0 = 全部通知">
              <InputNumber min={0} style={{ width: 140 }} />
            </Form.Item>
            <Form.Item name="mail_html" label="HTML 邮件" valuePropName="checked"
              extra="关闭则发纯文本">
              <Switch />
            </Form.Item>
          </Space>

          <Divider orientation="left" plain>模板</Divider>
          <Typography.Paragraph type="secondary" style={{ fontSize: 12 }}>
            可用变量：
            {['job_name', 'status_label', 'model', 'total', 'completed', 'failed', 'duration',
              'tokens', 'username', 'job_url', 'error', 'error_block'].map((v) => (
              <Tag key={v} className="mono" style={{ marginInlineEnd: 4 }}>{`{{${v}}}`}</Tag>
            ))}
            <br />
            <span className="mono">{'{{error_block}}'}</span> 只在失败时展开成一行错误信息，成功时为空。
          </Typography.Paragraph>
          <Form.Item name="mail_subject_template" label="邮件标题">
            <Input className="mono" />
          </Form.Item>
          <Form.Item name="mail_body_template" label="邮件正文">
            <Input.TextArea rows={9} className="mono" />
          </Form.Item>

          <Space wrap>
            <Button type="primary" loading={saving} onClick={() => void saveSettings()}>保存邮件设置</Button>
            <Input
              style={{ width: 240 }} placeholder="收件人，用于测试"
              value={mailTo} onChange={(e) => setMailTo(e.target.value)}
            />
            <Button
              loading={mailTesting}
              disabled={!mailTo.includes('@')}
              onClick={async () => {
                setMailTesting(true)
                setMailResult(null)
                try {
                  const r = await api.testMail(mailTo)
                  setMailResult(r)
                  if (r.ok) message.success(r.detail)
                } finally {
                  setMailTesting(false)
                }
              }}
            >
              发送测试邮件
            </Button>
            <Typography.Text type="secondary" style={{ fontSize: 12 }}>
              请先保存设置，测试用的是已保存的配置
            </Typography.Text>
          </Space>

          {mailResult && (
            <Alert
              style={{ marginTop: 16 }}
              type={mailResult.ok ? 'success' : 'error'} showIcon
              message={mailResult.ok ? '发送成功' : '发送失败'}
              description={
                <>
                  <div>{mailResult.detail}</div>
                  {mailResult.subject && (
                    <Collapse ghost size="small" items={[{
                      key: 'p', label: '查看渲染后的邮件内容',
                      children: (
                        <>
                          <div className="mono" style={{ marginBottom: 8 }}>标题：{mailResult.subject}</div>
                          <pre className="mono pre-wrap" style={{ margin: 0, maxHeight: 260, overflow: 'auto' }}>
                            {mailResult.body}
                          </pre>
                        </>
                      ),
                    }]} />
                  )}
                </>
              }
            />
          )}
        </Form>
      </Card>

      <Card title="用量统计（按用户）"
        extra={<Typography.Text type="secondary" style={{ fontSize: 12 }}>
          「任务占用」只含已提交成任务的文件，不含尚未提交的暂存上传
        </Typography.Text>}>
        <Table rowKey="user_id" size="small" columns={usageColumns} dataSource={usage} pagination={false} />
      </Card>
    </Space>
  )
}
