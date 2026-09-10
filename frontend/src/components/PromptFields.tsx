import { useMemo } from 'react'
import { Button, Form, Input, Space } from 'antd'
import { DeleteOutlined, PlusOutlined } from '@ant-design/icons'
import type { ScriptVariable } from '../api'

// 与后端 codegen.VAR_PATTERN 保持一致
const VAR_REF = /\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}/g

/** 列出模板里引用到的变量名（去重、保持出现顺序） */
export function extractVariables(template: string | undefined): string[] {
  const seen: string[] = []
  for (const m of (template ?? '').matchAll(VAR_REF)) {
    if (!seen.includes(m[1])) seen.push(m[1])
  }
  return seen
}

/** 模板与变量对不上的地方，规则与后端 codegen.validate 的变量部分一致 */
export function variableProblems(variables: ScriptVariable[], template: string | undefined): string[] {
  const declared = new Set(variables.filter((v) => v?.name).map((v) => v.name))
  const used = new Set(extractVariables(template))
  const problems: string[] = []
  const missing = [...used].filter((n) => !declared.has(n))
  if (missing.length) problems.push(`Prompt 里用到了未定义的变量：${missing.join('、')}`)
  const unused = [...declared].filter((n) => !used.has(n))
  if (unused.length) problems.push(`定义了但没在 Prompt 里用到的变量：${unused.join('、')}`)
  return problems
}

/**
 * 「数据变量 + 系统提示词 + Prompt 模板」这组表单项。
 * 脚本生成页和「我的 Prompt」共用，字段名固定为 variables / system_prompt / prompt_template。
 */
export default function PromptFields({ templateRows = 5 }: { templateRows?: number }) {
  const form = Form.useFormInstance()
  const variables = (Form.useWatch('variables', form) as ScriptVariable[] | undefined) ?? []

  const varHint = useMemo(
    () => variables.filter((v) => v?.name).map((v) => `{{${v.name}}}`).join('  '),
    [variables],
  )

  return (
    <>
      <Form.List name="variables">
        {(fields, { add, remove }) => (
          <>
            {fields.map((field) => (
              <Space key={field.key} align="baseline" style={{ display: 'flex', marginBottom: 8 }}>
                <Form.Item {...field} name={[field.name, 'name']} style={{ marginBottom: 0 }}
                  rules={[{ required: true, message: '变量名' },
                          { pattern: /^[A-Za-z_][A-Za-z0-9_]*$/, message: '字母/数字/下划线，不能数字开头' }]}>
                  <Input addonBefore="变量" style={{ width: 200 }} className="mono" placeholder="data" />
                </Form.Item>
                <span style={{ opacity: 0.5 }}>←</span>
                <Form.Item {...field} name={[field.name, 'field']} style={{ marginBottom: 0 }}
                  rules={[{ required: true, message: '源字段名' }]}>
                  <Input addonBefore="字段" style={{ width: 240 }} className="mono" placeholder="content" />
                </Form.Item>
                <Button type="text" danger icon={<DeleteOutlined />} onClick={() => remove(field.name)} />
              </Space>
            ))}
            <Button type="dashed" icon={<PlusOutlined />} onClick={() => add({ name: '', field: '' })}
              style={{ marginBottom: 16 }}>
              添加变量
            </Button>
          </>
        )}
      </Form.List>

      <Form.Item name="system_prompt" label="系统提示词（可选）">
        <Input.TextArea rows={2} placeholder="例：你是一名严谨的数据标注员，只输出 JSON。" />
      </Form.Item>
      <Form.Item name="prompt_template" label="Prompt 模板"
        rules={[{ required: true, message: '请填写 Prompt' }]}
        extra={varHint ? <>可用变量：<span className="mono">{varHint}</span></> : '先在上面添加变量'}>
        <Input.TextArea rows={templateRows} className="mono" />
      </Form.Item>
    </>
  )
}
