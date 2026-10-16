import { useCallback, useEffect, useState } from 'react'
import { App, Button, Divider, Input, Select, Space } from 'antd'
import type { SelectProps } from 'antd'
import { PlusOutlined } from '@ant-design/icons'
import { api } from '../api'
import type { Project } from '../api'

/** 筛选用的特殊值：未归入任何项目 */
export const UNASSIGNED = 'none'

type Props = Omit<SelectProps<string | null>, 'options' | 'onChange'> & {
  value?: string | null
  onChange?: (value: string | null) => void
  /** 筛选模式：多一个「未归类」选项，值为 UNASSIGNED */
  withUnassigned?: boolean
  /** 下拉底部可以直接新建项目 */
  allowCreate?: boolean
  /** 没选时自动选上默认项目（建任务、挪任务用：任务必须属于某个项目） */
  pickDefault?: boolean
  /** 外部已经拿到列表时直接用，省一次请求 */
  projects?: Project[]
  onProjectsChange?: (projects: Project[]) => void
}

/** 选择项目。允许清空时，不选表示不归入任何项目（只有 Prompt 可以不归类）。 */
export default function ProjectSelect({
  value, onChange, withUnassigned = false, allowCreate = false, pickDefault = false, projects: given,
  onProjectsChange, ...rest
}: Props) {
  const { message } = App.useApp()
  const [own, setOwn] = useState<Project[]>([])
  const [newName, setNewName] = useState('')
  const [creating, setCreating] = useState(false)
  const projects = given ?? own

  const load = useCallback(async () => {
    const list = await api.projects()
    setOwn(list)
    onProjectsChange?.(list)
  }, [onProjectsChange])

  useEffect(() => {
    if (!given) void load().catch(() => undefined)
    // 只在首次挂载时拉一次；外部传了列表就不拉
  }, [])

  useEffect(() => {
    if (!pickDefault || value) return
    const fallback = projects.find((p) => p.is_default)
    if (fallback) onChange?.(fallback.id)
  }, [pickDefault, value, projects])

  const create = async () => {
    const name = newName.trim()
    if (!name) return
    setCreating(true)
    try {
      const created = await api.createProject({ name })
      setNewName('')
      await load()
      onChange?.(created.id)
      message.success(`已创建项目「${created.name}」`)
    } catch { /* 拦截器已提示 */ } finally {
      setCreating(false)
    }
  }

  const options = [
    ...(withUnassigned ? [{ value: UNASSIGNED, label: '（未归类）' }] : []),
    // 默认项目改过名的，标一下它是默认的
    ...projects.map((p) => ({
      value: p.id, label: p.is_default && p.name !== '默认项目' ? `${p.name}（默认）` : p.name,
    })),
  ]

  return (
    <Select<string | null>
      allowClear
      showSearch
      optionFilterProp="label"
      placeholder="选择项目"
      notFoundContent="还没有项目"
      {...rest}
      value={value || undefined}
      onChange={(v) => onChange?.(v ?? null)}
      options={options}
      dropdownRender={allowCreate
        ? (menu) => (
          <>
            {menu}
            <Divider style={{ margin: '8px 0' }} />
            <Space style={{ padding: '0 8px 4px' }}>
              <Input
                size="small" placeholder="新项目名称" value={newName} maxLength={128}
                onChange={(e) => setNewName(e.target.value)}
                onKeyDown={(e) => e.stopPropagation()}
                onPressEnter={() => void create()}
              />
              <Button size="small" type="text" icon={<PlusOutlined />} loading={creating}
                onClick={() => void create()}>新建</Button>
            </Space>
          </>
        )
        : undefined}
    />
  )
}
