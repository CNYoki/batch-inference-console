import { useCallback, useEffect, useRef, useState } from 'react'
import { useNavigate, useSearchParams } from 'react-router-dom'
import {
  Alert, App, Button, Card, Col, Collapse, Divider, Form, Input, InputNumber, Modal, Row, Select,
  Space, Switch, Tag, Tooltip, Typography,
} from 'antd'
import { CopyOutlined, DownloadOutlined, SaveOutlined } from '@ant-design/icons'
import { api } from '../api'
import type { SavedPrompt, SavedPromptInput, ScriptConfig, ScriptPreview, ScriptVariable } from '../api'
import PromptFields from '../components/PromptFields'

const DEFAULT_CONFIG: ScriptConfig = {
  source_path: '/data/raw',
  source_format: 'csv',
  csv_delimiter: ',',
  encoding: 'utf-8',
  recursive: false,
  custom_id_mode: 'rownum',
  custom_id_field: null,
  custom_id_prefix: 'req-',
  variables: [{ name: 'data', field: 'content' }],
  system_prompt: null,
  prompt_template: '请对下面的内容做摘要：\n\n{{data}}',
  model: '',
  endpoint_path: '/v1/chat/completions',
  extra_body: {},
  output_dir: './batch_input',
  output_prefix: 'part',
  max_rows_per_file: 50000,
  max_file_size_mb: 100,
  max_prompt_chars: 0,
  max_line_bytes: 0,
  on_oversize: 'skip',
  skip_empty: true,
  check_duplicate_ids: true,
}

export default function ScriptGeneratorPage() {
  const { message } = App.useApp()
  const navigate = useNavigate()
  const [searchParams] = useSearchParams()
  const [form] = Form.useForm()
  const [saveForm] = Form.useForm<{ name: string }>()
  const [preview, setPreview] = useState<ScriptPreview | null>(null)
  const [loading, setLoading] = useState(false)
  const [prompts, setPrompts] = useState<SavedPrompt[]>([])
  const [activePromptId, setActivePromptId] = useState<string | null>(null)
  const [saveOpen, setSaveOpen] = useState(false)
  const [savingPrompt, setSavingPrompt] = useState(false)
  const timer = useRef<number | null>(null)

  const buildConfig = useCallback((values: Record<string, unknown>): ScriptConfig => {
    let extra: Record<string, unknown> = {}
    const raw = (values.extra_body_text as string) || ''
    if (raw.trim()) extra = JSON.parse(raw) as Record<string, unknown>
    return {
      ...DEFAULT_CONFIG,
      ...values,
      variables: ((values.variables as ScriptVariable[]) || []).filter((v) => v?.name && v?.field),
      extra_body: extra,
      custom_id_field: (values.custom_id_field as string) || null,
      system_prompt: (values.system_prompt as string) || null,
    } as ScriptConfig
  }, [])

  const refresh = useCallback(async () => {
    let values: Record<string, unknown>
    try {
      values = await form.validateFields()
    } catch {
      return // 表单还没填完，先不请求
    }
    setLoading(true)
    try {
      setPreview(await api.splitScript(buildConfig(values)))
    } catch (err) {
      if (err instanceof SyntaxError) message.error('「附加请求参数」不是合法 JSON')
    } finally {
      setLoading(false)
    }
  }, [form, buildConfig, message])

  // 表单一改就重新生成，但做个防抖，别每敲一个字就发一次请求
  const scheduleRefresh = useCallback(() => {
    if (timer.current) window.clearTimeout(timer.current)
    timer.current = window.setTimeout(() => void refresh(), 400)
  }, [refresh])

  const applyPrompt = useCallback((p: SavedPrompt) => {
    form.setFieldsValue({
      variables: p.variables.map((v) => ({ ...v })),
      system_prompt: p.system_prompt ?? '',
      prompt_template: p.prompt_template,
    })
    setActivePromptId(p.id)
    // setFieldsValue 不会触发 onValuesChange，得手动刷新预览
    scheduleRefresh()
  }, [form, scheduleRefresh])

  useEffect(() => {
    void refresh()
    // 从「我的 Prompt」点「生成脚本」过来时带着 ?prompt=<id>，加载完直接带入
    const wanted = searchParams.get('prompt')
    api.prompts().then((list) => {
      setPrompts(list)
      const hit = wanted ? list.find((p) => p.id === wanted) : undefined
      if (hit) applyPrompt(hit)
    }).catch(() => undefined)
    return () => { if (timer.current) window.clearTimeout(timer.current) }
    // 只在首次挂载时生成一次
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  const openSaveModal = () => {
    const active = prompts.find((p) => p.id === activePromptId)
    saveForm.setFieldsValue({ name: active?.name ?? '' })
    setSaveOpen(true)
  }

  const saveName = (Form.useWatch('name', saveForm) as string | undefined)?.trim() ?? ''
  const overwriting = prompts.find((p) => p.name === saveName)

  const savePrompt = async () => {
    const { name } = await saveForm.validateFields()
    const values = form.getFieldsValue() as Record<string, unknown>
    const payload: SavedPromptInput = {
      name: name.trim(),
      variables: ((values.variables as ScriptVariable[]) || []).filter((v) => v?.name && v?.field),
      system_prompt: (values.system_prompt as string) || null,
      prompt_template: (values.prompt_template as string) || '',
    }
    if (!payload.prompt_template.trim()) {
      message.error('Prompt 模板为空，先填好再保存')
      return
    }
    setSavingPrompt(true)
    try {
      // 同名就覆盖那一条，而不是报「重名」让用户再去改
      const saved = overwriting
        ? await api.updatePrompt(overwriting.id, payload)
        : await api.createPrompt(payload)
      setPrompts(await api.prompts())
      setActivePromptId(saved.id)
      setSaveOpen(false)
      message.success(overwriting ? `已更新「${saved.name}」` : `已保存为「${saved.name}」`)
    } catch { /* 拦截器已提示 */ } finally {
      setSavingPrompt(false)
    }
  }

  const copyScript = async () => {
    if (!preview) return
    try {
      await navigator.clipboard.writeText(preview.script)
      message.success('脚本已复制到剪贴板')
    } catch {
      message.error('浏览器拒绝了剪贴板访问，请手动全选复制')
    }
  }

  const downloadScript = () => {
    if (!preview) return
    // 直接用已经拿到的脚本内容生成下载，省一次请求
    const blob = new Blob([preview.script], { type: 'text/x-python;charset=utf-8' })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = preview.script_name
    a.click()
    URL.revokeObjectURL(url)
  }

  const sourceFormat = Form.useWatch('source_format', form) as string | undefined
  const customIdMode = Form.useWatch('custom_id_mode', form) as string | undefined

  return (
    <Row gutter={16}>
      <Col xs={24} xl={13}>
        <Card title="生成数据切分脚本">
          <Form
            form={form} layout="vertical"
            initialValues={{ ...DEFAULT_CONFIG, extra_body_text: '' }}
            onValuesChange={scheduleRefresh}
          >
            <Divider orientation="left" plain>输入</Divider>
            <Row gutter={16}>
              <Col span={16}>
                <Form.Item name="source_path" label="文件或目录路径（你本地的路径）"
                  rules={[{ required: true, message: '请填写路径' }]}
                  extra="填目录会处理里面所有匹配格式的文件">
                  <Input className="mono" placeholder="/data/raw 或 /data/raw/input.csv" />
                </Form.Item>
              </Col>
              <Col span={8}>
                <Form.Item name="source_format" label="文件格式">
                  <Select options={[
                    { value: 'csv', label: 'CSV' },
                    { value: 'jsonl', label: 'JSONL / NDJSON' },
                    { value: 'parquet', label: 'Parquet（需 pyarrow）' },
                  ]} />
                </Form.Item>
              </Col>
            </Row>
            <Space size={24} align="start" wrap>
              {sourceFormat === 'csv' && (
                <Form.Item name="csv_delimiter" label="分隔符">
                  <Input style={{ width: 90 }} className="mono" />
                </Form.Item>
              )}
              <Form.Item name="encoding" label="编码">
                <Input style={{ width: 130 }} className="mono" />
              </Form.Item>
              <Form.Item name="recursive" label="递归子目录" valuePropName="checked">
                <Switch />
              </Form.Item>
            </Space>

            <Divider orientation="left" plain>custom_id</Divider>
            <Space size={16} align="start" wrap>
              <Form.Item name="custom_id_mode" label="生成方式"
                extra="结果要靠它对齐回原始数据">
                <Select style={{ width: 190 }} options={[
                  { value: 'rownum', label: '行号' },
                  { value: 'field', label: '读取某个字段' },
                  { value: 'uuid', label: '生成 UUID' },
                ]} />
              </Form.Item>
              {customIdMode === 'field' && (
                <Form.Item name="custom_id_field" label="字段名"
                  rules={[{ required: true, message: '请填字段名' }]}>
                  <Input style={{ width: 180 }} className="mono" placeholder="id" />
                </Form.Item>
              )}
              <Form.Item name="custom_id_prefix" label="前缀（可选）">
                <Input style={{ width: 150 }} className="mono" placeholder="req-" />
              </Form.Item>
            </Space>

            <Divider orientation="left" plain>数据变量与 Prompt</Divider>
            <Space wrap style={{ marginBottom: 16 }}>
              <Select
                style={{ width: 260 }} placeholder="从我的 Prompt 导入" showSearch
                optionFilterProp="label"
                value={activePromptId ?? undefined}
                options={prompts.map((p) => ({ value: p.id, label: p.name }))}
                onChange={(id: string) => {
                  const hit = prompts.find((p) => p.id === id)
                  if (hit) applyPrompt(hit)
                }}
                notFoundContent={<span>还没有保存过 Prompt</span>}
              />
              <Tooltip title="把下面的变量、系统提示词和模板存进「我的 Prompt」">
                <Button icon={<SaveOutlined />} onClick={openSaveModal}>保存为我的 Prompt</Button>
              </Tooltip>
              <Button type="link" onClick={() => navigate('/tools/prompts')} style={{ paddingInline: 4 }}>
                管理
              </Button>
            </Space>
            <PromptFields />

            <Collapse size="small" items={[{
              key: 'more',
              label: '请求端点与切分参数',
              children: (
                <>
                  <Row gutter={16}>
                    <Col span={12}>
                      <Form.Item name="endpoint_path" label="请求端点"
                        extra="写进每行的 url 字段，与 OpenAI Batch 格式一致">
                        <Input className="mono" />
                      </Form.Item>
                    </Col>
                    <Col span={12}>
                      <Form.Item name="model" label="model 字段（可选）"
                        extra="留空则由平台的模型配置决定，一般不用填">
                        <Input className="mono" placeholder="留空即可" />
                      </Form.Item>
                    </Col>
                  </Row>
                  <Row gutter={16}>
                    <Col span={12}>
                      <Form.Item name="output_dir" label="输出目录">
                        <Input className="mono" />
                      </Form.Item>
                    </Col>
                    <Col span={12}>
                      <Form.Item name="output_prefix" label="输出文件名前缀">
                        <Input className="mono" />
                      </Form.Item>
                    </Col>
                  </Row>
                  <Space size={16} align="start" wrap>
                    <Form.Item name="max_rows_per_file" label="每个文件最大行数" extra="0 = 不限">
                      <InputNumber min={0} style={{ width: 150 }} />
                    </Form.Item>
                    <Form.Item name="max_file_size_mb" label="每个文件最大大小" extra="0 = 不限">
                      <InputNumber min={0} style={{ width: 150 }} addonAfter="MB" />
                    </Form.Item>
                    <Form.Item name="max_prompt_chars" label="单条 prompt 字符上限" extra="0 = 不限">
                      <InputNumber min={0} style={{ width: 170 }} />
                    </Form.Item>
                    <Form.Item name="max_line_bytes" label="单行字节上限" extra="0 = 不限">
                      <InputNumber min={0} style={{ width: 150 }} addonAfter="B" />
                    </Form.Item>
                  </Space>
                  <Space size={24} align="start" wrap>
                    <Form.Item name="on_oversize" label="超限时">
                      <Select style={{ width: 150 }} options={[
                        { value: 'skip', label: '跳过该行' },
                        { value: 'truncate', label: '截断 prompt' },
                      ]} />
                    </Form.Item>
                    <Form.Item name="skip_empty" label="跳过空 prompt" valuePropName="checked">
                      <Switch />
                    </Form.Item>
                    <Form.Item name="check_duplicate_ids" label="检查重复 custom_id" valuePropName="checked">
                      <Switch />
                    </Form.Item>
                  </Space>
                  <Form.Item name="extra_body_text" label="附加请求参数 (JSON)"
                    extra='合并进每行的 body，例如 {"temperature": 0.2}'>
                    <Input.TextArea rows={2} className="mono" placeholder="{}" />
                  </Form.Item>
                </>
              ),
            }]} />
          </Form>
        </Card>
      </Col>

      <Col xs={24} xl={11}>
        <Space direction="vertical" size={16} style={{ width: '100%' }}>
          {preview && preview.problems.length > 0 && (
            <Alert type="warning" showIcon message="配置有问题"
              description={<ul style={{ paddingLeft: 18, margin: 0 }}>
                {preview.problems.map((p) => <li key={p}>{p}</li>)}
              </ul>} />
          )}

          <Card title="输出样例" size="small"
            extra={preview?.detected_variables.length
              ? <Space size={4}>{preview.detected_variables.map((v) =>
                  <Tag key={v} className="mono">{`{{${v}}}`}</Tag>)}</Space>
              : null}>
            <pre className="mono pre-wrap" style={{ margin: 0, maxHeight: 220, overflow: 'auto' }}>
              {preview ? JSON.stringify(preview.sample_record, null, 2) : '…'}
            </pre>
          </Card>

          <Card
            title="生成的脚本" size="small" loading={loading && !preview}
            extra={
              <Space>
                <Tooltip title="复制全部内容">
                  <Button size="small" icon={<CopyOutlined />} onClick={() => void copyScript()}>复制</Button>
                </Tooltip>
                <Button size="small" type="primary" icon={<DownloadOutlined />} onClick={downloadScript}>
                  下载 .py
                </Button>
              </Space>
            }
          >
            <Typography.Paragraph type="secondary" style={{ fontSize: 12 }}>
              在你本地执行：<span className="mono">python {preview?.script_name ?? 'build_batch_input.py'}</span>；
              先加 <span className="mono">--dry-run</span> 只统计不写文件，
              或 <span className="mono">--limit 100</span> 拿前 100 行试跑。
            </Typography.Paragraph>
            <pre className="mono" style={{ margin: 0, maxHeight: 460, overflow: 'auto', fontSize: 11 }}>
              {preview?.script ?? ''}
            </pre>
          </Card>
        </Space>
      </Col>

      <Modal
        title="保存为我的 Prompt" open={saveOpen} onCancel={() => setSaveOpen(false)}
        onOk={() => void savePrompt()} confirmLoading={savingPrompt}
        okText={overwriting ? '覆盖保存' : '保存'} cancelText="取消"
      >
        <Form form={saveForm} layout="vertical" style={{ marginTop: 16 }}>
          <Form.Item name="name" label="名称"
            rules={[{ required: true, whitespace: true, message: '请填写名称' }]}
            extra={overwriting ? `将覆盖已有的「${overwriting.name}」` : '保存数据变量、系统提示词与 Prompt 模板'}>
            <Input maxLength={128} placeholder="例：新闻摘要" onPressEnter={() => void savePrompt()} />
          </Form.Item>
        </Form>
      </Modal>
    </Row>
  )
}
