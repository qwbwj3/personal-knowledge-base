"""Revision-scoped source authority; labels are not authenticity certification."""
import re

POLICY_VERSION = 'source-trust-v1'
ADOPT_FIELD = '确认采用产品修订'
ROLE_FIELD = '资料用途'
ROLES = {'正式产品资料', '产品解读', '个人笔记', '社交文案', '示例资料'}

def assess(relative, text, kind, decision, digest):
    explicit = decision.get(ROLE_FIELD)
    if ROLE_FIELD in decision and (not isinstance(explicit, str) or explicit not in ROLES):
        raise ValueError('资料用途不受支持')
    # Only explicit whole-document self-descriptions; ordinary illustrative
    # paragraphs mentioning examples are not proof the entire source is fake.
    pattern = r'(?:本(?:文|资料|文件|条款)|此(?:文件|资料))[：:，,\s]*(?:为|是|仅供|属于)?[^\n。]{0,16}(?:虚构|测试件|演示稿|非真实|非正式)|(?:测试件|演示稿)[，,：:·•—\s]*(?:非真实|禁止业务使用)'
    risks = [{'kind':'self_described_non_authoritative', 'start':m.start(), 'end':m.end(), 'text':m.group(0)} for m in re.finditer(pattern,text) if not re.search(r'(?:不是|并非|并不是|不属于)[^\n。]{0,8}(?:测试件|演示稿|虚构)', m.group(0))]
    role = explicit or ('产品解读' if kind == '产品资料' and any(w in relative for w in ('摘要','解读','培训','问答','笔记')) else '正式产品资料' if kind == '产品资料' else '社交文案' if kind == '个人内容资料' else '个人笔记')
    if risks:
        role = '示例资料'
    adopted = decision.get(ADOPT_FIELD) is True
    return {'policy_version':POLICY_VERSION, 'policy_basis_sha256':digest,
            'material_role':role, 'authority_tier':'primary_product' if kind == '产品资料' and role == '正式产品资料' else 'secondary',
            'source_risk':risks, 'authority_basis':'person_label' if explicit else 'inferred_not_authenticated',
            'adoption_status':'explicit_revision' if adopted else 'initial_or_existing',
            'original_protection':'sha256_snapshot_not_os_write_lock'}
