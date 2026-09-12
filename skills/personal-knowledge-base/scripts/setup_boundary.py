"""Read-only first-run contract and structural source boundaries (no sample names)."""
from pathlib import Path


def setup_response(missing=None):
    fields = {
        'source_root': '请本人选择专用资料文件夹（不是Skill、安装目录或工程测试资料）。',
        'name': '请本人为知识库命名。',
        'confirmation': '请确认这些资料有权交给当前模型处理，并记录本人明确授权。',
        'model_context_egress_approved': '是否同意将回答所需的有限原文交给当前模型？',
    }
    return {
        'schema': 'personal-kb.setup.v1', 'status': 'needs_setup',
        'interaction_required': True, 'next_action': 'ask_user',
        'required_fields': [{'field': key, 'prompt': value} for key, value in fields.items()],
        'missing_fields': list(fields) if missing is None else missing,
        'message': '尚未完成首次配置。请与本人交互选择资料目录、知识库名称和授权；不得自动选择目录、扫描或用工程样例建库。无需本人编写JSON或代码。',
        'source_scanned': False, 'state_written': False,
    }


def boundary_reason(source: Path, skill_root: Path):
    """Resolve aliases; inspect directory markers only, never material contents."""
    source, skill_root = source.resolve(), skill_root.resolve()
    if source == skill_root or skill_root in source.parents or source in skill_root.parents:
        return '资料目录不能与Skill源码或安装目录重叠'
    for parent in (source, *source.parents):
        if (parent / 'SKILL.md').is_file():
            return 'Skill源码或安装目录及其子目录不能作为个人资料'
        # Works for clean exports as well as git clones; no hardcoded fixture filenames.
        engineering = (parent / 'engineering').is_dir()
        tests = any((parent / name).is_dir() for name in ('tests', 'test', 'qa', 'fixtures', 'demos'))
        project = (parent / '.git').exists() or (parent / 'pyproject.toml').is_file() or (parent / 'package.json').is_file() or (parent / 'skills').is_dir()
        if (engineering and tests) or (project and tests):
            return '含测试资料的工程仓库及其子目录不能作为个人资料'
    return None
