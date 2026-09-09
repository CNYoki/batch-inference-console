"""辅助工具：为用户生成本地数据预处理脚本。"""
from __future__ import annotations

import json

from fastapi import APIRouter
from fastapi.responses import PlainTextResponse

from ..core.deps import CurrentUser
from ..schemas import ScriptConfig, ScriptPreview
from ..services import codegen

router = APIRouter(prefix="/tools", tags=["tools"])


def _build(config: ScriptConfig) -> ScriptPreview:
    record = codegen.preview_record(config)
    return ScriptPreview(
        script=codegen.generate(config),
        script_name=codegen.DEFAULT_SCRIPT_NAME,
        sample_record=record,
        sample_line=json.dumps(record, ensure_ascii=False),
        detected_variables=codegen.extract_variables(config.prompt_template),
        problems=codegen.validate(config),
    )


@router.post("/split-script", response_model=ScriptPreview)
async def generate_split_script(config: ScriptConfig, _: CurrentUser) -> ScriptPreview:
    """生成切分脚本，同时回传示例输出与配置检查结果。

    配置有问题也照样生成 —— 问题以列表形式返回，由前端提示，
    这样用户可以先看到脚本长什么样再决定要不要改。
    """
    return _build(config)


@router.post("/split-script/download", response_class=PlainTextResponse)
async def download_split_script(config: ScriptConfig, _: CurrentUser) -> PlainTextResponse:
    return PlainTextResponse(
        codegen.generate(config),
        media_type="text/x-python; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{codegen.DEFAULT_SCRIPT_NAME}"'},
    )
