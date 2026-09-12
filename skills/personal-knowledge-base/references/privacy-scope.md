# 隐私范围

首次配置集中确认一次，保存为用户可读的 scope JSON。重点询问：

1. **使用者与存储**：由哪个 Agent 使用；新 Obsidian Vault 放在哪里。
2. **允许读取**：一个精确资料文件夹。不要默认扫描主目录、其他项目或其他记忆系统。
3. **明确排除**：客户个人资料、账号与凭证、密钥、财务、医疗、人事敏感信息，以及用户点名目录。
4. **两类出口**：外部来源抓取固定关闭；另行确认是否允许把少量检索页面交给当前 Agent 模型。
5. **操作权限**：仅查询，或允许摄取与更新。删除、移动、扩大范围必须重新确认。
6. **预算与保留**：文件数、总大小、单文件大小、PDF 页数、单文件提取字符上限，以及测试 Vault 的保留方式。

当前结构：

```json
{
  "schema": "llm-wiki.privacy-scope.v2",
  "allowed_source_roots": ["/absolute/path/to/approved-materials"],
  "allowed_vaults": ["/absolute/path/to/new-vault"],
  "forbidden_roots": ["/absolute/path/to/excluded-data"],
  "source_fetch_egress_approved": false,
  "model_context_egress_approved": true,
  "mode": "query-and-ingest",
  "budgets": {
    "max_files": 20,
    "max_total_bytes": 52428800,
    "max_file_bytes": 20971520,
    "max_pdf_pages": 120,
    "max_extracted_chars_per_file": 250000
  },
  "retention": "用户确认的保留方式",
  "confirmation": "不含秘密的本轮授权记录"
}
```

路径在执行前会解析为真实绝对路径。符号链接不会跟随；禁止目录内的文件会跳过；资料根目录和 Vault
不能互相包含。`model_context_egress_approved: false` 会阻止查询包装器把页面正文交给当前模型。
