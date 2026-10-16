import { useCallback, useEffect, useMemo, useState } from 'react'
import { useNavigate, useSearchParams } from 'react-router-dom'
import {
  Alert, App, Button, Card, Divider, Drawer, Form, Input, Space, Table, Tag, Tooltip, Typography,
} from 'antd'
import type { ColumnsType } from 'antd/es/table'
import {
  CodeOutlined, CopyOutlined, DeleteOutlined, EditOutlined, FolderOutlined, PlusOutlined,
} from '@ant-design/icons'
import { api } from '../api'
import type { Project, SavedPrompt, SavedPromptInput, ScriptVariable } from '../api'
import ProjectSelect from '../components/ProjectSelect'
import PromptFields, { variableProblems } from '../components/PromptFields'
import { formatDateTime } from '../utils'

const EMPTY: SavedPromptInput = {
  name: '',
  description: '',
  system_prompt: '',
  prompt_template: '请对下面的内容做摘要：\n\n{{data}}',
  variables: [{ name: 'data', field: 'content' }],
}

export default function PromptsPage() {
  const { modal, message } = App.useApp()
  const navigate = useNavigate()
  const [form] = Form.useForm()
  const [rows, setRows] = useState<SavedPrompt[]>([])
  const [loading, setLoading] = useState(true)
  const [open, setOpen] = useState(false)
  const [editing, setEditing] = useState<SavedPrompt | null>(null)
  const [saving, setSaving] = useState(false)
  const [projects, setProjects] = useState<Project[]>([])
  // 按项目筛选放在网址里，项目页的「N 个 Prompt」能直接链过来；'none' 表示未归类
  const [searchParams, setSearchParams] = useSearchParams()
  const project = searchParams.get('project') ?? ''
  const projectName = useMemo(
    () => Object.fromEntries(projects.map((p) => [p.id, p.name])) as Record<string, string>,
    [projects],
  )

  const load = useCallback(async () => {
    setLoading(true)
    try {
      setRows(await api.prompts(project || undefined))
    } finally {
      setLoading(false)
    }
  }, [project])

  useEffect(() => { void load() }, [load])
  useEffect(() => { api.projects().then(setProjects).catch(() => undefined) }, [])

  /** record 为空是新建；copy=true 表示以它为底另存一份 */
  const openDrawer = (record?: SavedPrompt, copy = false) => {
    setEditing(record && !copy ? record : null)
    form.resetFields()
    form.setFieldsValue(record
      ? {
          ...record,
          name: copy ? `${record.name} 副本` : record.name,
          description: record.description ?? '',
          system_prompt: record.system_prompt ?? '',
          project_id: record.project_id ?? null,
        }
      // 正在看某个项目时，新建的 Prompt 默认归到这个项目
      : { ...EMPTY, project_id: project && project !== 'none' ? project : null })
    setOpen(true)
  }

  const save = async () => {
    const values = await form.validateFields() as SavedPromptInput
    const payload: SavedPromptInput = {
      ...values,
      variables: (values.variables || []).filter((v) => v?.name && v?.field),
      description: values.description?.trim() || null,
      system_prompt: values.system_prompt || null,
      project_id: values.project_id ?? null,
    }
    setSaving(true)
    try {
      if (editing) {
        await api.updatePrompt(editing.id, payload)
        message.success('已保存')
      } else {
        await api.createPrompt(payload)
        message.success('已创建')
      }
      setOpen(false)
      await load()
    } catch { /* 拦截器已提示 */ } finally {
      setSaving(false)
    }
  }

  const variables = (Form.useWatch('variables', form) as ScriptVariable[] | undefined) ?? []
  const template = Form.useWatch('prompt_template', form) as string | undefined
  const problems = open ? variableProblems(variables, template) : []

  const columns: ColumnsType<SavedPrompt> = [
    {
      title: '名称', width: 220,
      render: (_, r) => (
        <Space direction="vertical" size={0}>
          <Typography.Text strong>{r.name}</Typography.Text>
          {r.description && <Typography.Text type="secondary" style={{ fontSize: 12 }}>{r.description}</Typography.Text>}
        </Space>
      ),
    },
    {
      title: '项目', width: 140, ellipsis: true,
      render: (_, r) => r.project_id && projectName[r.project_id]
        ? <Tag icon={<FolderOutlined />}>{projectName[r.project_id]}</Tag>
        : <Typography.Text type="secondary">—</Typography.Text>,
    },
    {
      title: '数据变量', width: 260,
      render: (_, r) => r.variables.length
        ? (
          <Space size={[4, 4]} wrap>
            {r.variables.map((v) => (
              <Tag key={v.name} className="mono">{`{{${v.name}}}`} ← {v.field}</Tag>
            ))}
          </Space>
        )
        : <Typography.Text type="secondary">无</Typography.Text>,
    },
    {
      title: 'Prompt 模板', ellipsis: true,
      render: (_, r) => (
        <Tooltip title={<pre className="pre-wrap" style={{ margin: 0 }}>{r.prompt_template}</pre>}
          overlayStyle={{ maxWidth: 520 }}>
          <Typography.Text className="mono" ellipsis style={{ maxWidth: 360 }}>
            {r.system_prompt && <Tag color="blue">含系统提示</Tag>}
            {r.prompt_template}
          </Typography.Text>
        </Tooltip>
      ),
    },
    { title: '更新时间', width: 170, render: (_, r) => formatDateTime(r.updated_at) },
    {
      title: '操作', width: 250, fixed: 'right',
      render: (_, r) => (
        <Space size={4}>
          <Button size="small" type="primary" ghost icon={<CodeOutlined />}
            onClick={() => navigate(`/tools/split-script?prompt=${r.id}`)}>生成脚本</Button>
          <Button size="small" icon={<EditOutlined />} onClick={() => openDrawer(r)}>编辑</Button>
          <Tooltip title="复制一份">
            <Button size="small" icon={<CopyOutlined />} onClick={() => openDrawer(r, true)} />
          </Tooltip>
          <Button size="small" danger icon={<DeleteOutlined />} onClick={() => modal.confirm({
            title: `删除 Prompt「${r.name}」？`,
            content: '已经生成的脚本不受影响。',
            okText: '删除', okButtonProps: { danger: true }, cancelText: '取消',
            onOk: async () => { await api.deletePrompt(r.id); message.success('已删除'); await load() },
          })} />
        </Space>
      ),
    },
  ]

  return (
    <Card
      title="我的 Prompt"
      extra={<Button type="primary" icon={<PlusOutlined />} onClick={() => openDrawer()}>新建 Prompt</Button>}
    >
      <Typography.Paragraph type="secondary">
        把常用的 Prompt 连同数据变量存下来，生成预处理脚本时一键带入。
        变量写成 <span className="mono">{'{{变量名}}'}</span>，脚本运行时会替换成每行数据里对应字段的值。
      </Typography.Paragraph>
      <ProjectSelect
        withUnassigned placeholder="按项目筛选" style={{ width: 220, marginBottom: 16 }}
        projects={projects} value={project || null}
        onChange={(v) => setSearchParams((prev) => {
          const next = new URLSearchParams(prev)
          if (v) next.set('project', v)
          else next.delete('project')
          return next
        }, { replace: true })}
      />
      <Table rowKey="id" loading={loading} columns={columns} dataSource={rows} scroll={{ x: 1240 }}
        pagination={rows.length > 20 ? { pageSize: 20 } : false}
        locale={{ emptyText: '还没有保存任何 Prompt' }} />

      <Drawer
        title={editing ? `编辑：${editing.name}` : '新建 Prompt'}
        open={open} onClose={() => setOpen(false)} width={720} destroyOnClose={false}
        extra={<Space>
          <Button onClick={() => setOpen(false)}>取消</Button>
          <Button type="primary" loading={saving} onClick={() => void save()}>保存</Button>
        </Space>}
      >
        <Form form={form} layout="vertical">
          <Form.Item name="name" label="名称"
            rules={[{ required: true, whitespace: true, message: '请填写名称' }]}>
            <Input maxLength={128} placeholder="例：新闻摘要" />
          </Form.Item>
          <Form.Item name="description" label="说明（可选）">
            <Input maxLength={1024} placeholder="这个 Prompt 适用于什么数据、产出什么" />
          </Form.Item>
          <Form.Item name="project_id" label="所属项目（可选）">
            <ProjectSelect allowCreate placeholder="不归入项目" projects={projects} onProjectsChange={setProjects} />
          </Form.Item>

          <Divider orientation="left" plain>数据变量与 Prompt</Divider>
          <PromptFields templateRows={8} />
          {problems.length > 0 && (
            <Alert type="warning" showIcon message="变量与模板对不上"
              description={<ul style={{ paddingLeft: 18, margin: 0 }}>
                {problems.map((p) => <li key={p}>{p}</li>)}
              </ul>} />
          )}
        </Form>
      </Drawer>
    </Card>
  )
}
