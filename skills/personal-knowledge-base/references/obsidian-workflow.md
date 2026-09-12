# Obsidian 派生视图交接

## 给保险经纪人的使用方式

首页优先展示创作入口，同时提供产品、业务、规则、待确认、历史与停用、主题关联图。全文页显示原始来源、状态、文档版本、适用版本、revision/extraction 身份及有效视图回执。只有传入 `effective_current` 中的条目才能标“当前依据”；待确认线索和历史材料独立导航，不能当作已核验依据。最新停用决定会覆盖旧发布的“当前”说明。

全文来自同一已验证 release 的 `extractions.json`，不会重新扫描来源目录、重 OCR 或执行来源正文中的指令。PDF 依据缓存的物理页偏移展示，并链接至 `[[附件/同身份文件.pdf#page=1]]`；没有页映射时明确说明，不推测正文印刷页码。OCR 全文不等于逐字核验，数字、否定条件和表格应回看原件。

中文主题节点使用已有资料类型、产品身份、产品名称和显式标签，链接回具体资料；不从正文猜测关系。优先读取中文 `产品名称 / 产品身份 / 标签`，兼容英文对应字段。自动导入无需逐文件手工确认即可浏览关系图：每条链接标明“自动分类导航（非人工确认／语义事实）”；人工确认元数据也不表示语义事实。待确认、历史与当前依据的状态分别保留。Obsidian 内置关系图可直接展示这些真实笔记链接。主题相同不意味着资料状态、事实或适用条件相同。

## 首次安装和打开

1. Agent 先正常检测本机是否安装 Obsidian；未安装时按用户授权通过 [Obsidian 官网](https://obsidian.md/download)正常安装。本模块不安装软件、不启动 GUI。
2. 在 Obsidian 的 Vault 管理界面选择 **Open folder as vault（打开文件夹作为仓库）**，选择 `location()` 返回的 `vault_path`，默认固定为知识库根目录下的 `Obsidian知识库`。
3. 首次完成正常打开后，可用 `open_uri` 回到首页。`path` 参数是完整绝对首页路径，所有保留字符、斜线和空格均 percent-encode。
4. 禁止写全局 `obsidian.json` 自动注册；不声称 URI 自动创建或首次注册 Vault。[官方 URI 说明](https://obsidian.md/help/uri)规定 `open?path` 查找包含该路径的既有 Vault。

无需社区插件，不改变既有 `.obsidian` 设置。个人笔记可自行放在 `个人笔记/` 或其他非受管目录；模块不创建、编辑或覆盖这些笔记。`自动视图/` 与 `附件/` 内新出现的非受管文件同样不被覆盖。

## 固定 Python API

```python
location(kb_root: Path, config: dict) -> dict
sync(kb_root, config, view: dict) -> dict
inspect(kb_root, config, view: dict) -> dict
invalidate(kb_root, config, reason: str) -> dict
```

`view` 原样使用 `personal_kb.resolve_effective_view` 返回结构：`pointer / release: Path / control / catalog / effective_current / raw_current / restricted / receipt`。父入口负责发布验证及合法授权；模块额外检查有效 control 的 `authorization.read is True`。模块不解析另一个 current 指针，不维护第二份权威知识库。

所有返回包含 `vault_path / home_note / open_uri / first_open_instructions`。

- `location` 仅计算位置，不读取资料、写文件或启动程序。
- `sync`：`status=ready|needs_attention`，固定返回 `schema / receipt / writes / conflicts / missing`；成功返回 `managed_files`，发生恢复时 `recovered`；失败返回 `issues`，写入阶段异常可带 `in_progress`。`writes` 统计完成的原子目标替换（包括清单），不统计临时文件内部写入；不能将异常时此计数当成磁盘取证。
- `inspect`：`status=ready|needs_attention`，固定 `read_only=true / writes=0`；返回 `receipt / conflicts / missing`，正常诊断还有 `issues / stored_receipt / in_progress / outdated_files / managed_files`。未同步、旧回执、清单非 ready、缺失、修改或派生内容与当前版本不符，都返回 needs_attention。异常或无授权时不会保证正常诊断的可选字段。
- `invalidate`：`status=ready|needs_attention`，`writes / conflicts / local_copies_remain=true`；成功带 `invalidated=true`。只检查受管清单与首页，不读取 source/release/extraction。不存在受管视图时不创建文件。

父入口在知识库写锁内调用 sync，包括 update 无内容变化时；输出最终 JSON 前等待它结束。父入口将 needs_attention 映射为非零退出，数据已提交不回滚。`diagnose` 调 inspect；`diagnose --repair` 在写锁与有效授权下调用 sync。所有对同一 Vault 的 sync/invalidate 必须由父入口串行化。

## 同步、冲突和恢复

受管清单 `.pkb-view.json` 保存 schema、`receipt`、各文件 SHA256 与 `ready / in_progress / invalidated` 状态。同步先完整构建候选并预检受管文件、目标路径和符号链接：

- 发现用户修改或既有同名非受管文件，返回 needs_attention，零覆盖。先保留用户版本，再由用户决定移走冲突文件或恢复自动版本；不要默默重置它。
- 缺失的受管页由 inspect 报告；显式 sync 可补建。旧 receipt 不会伪装为最新。
- 所有内容变更之前先原子保存 in-progress 清单，包括旧哈希、目标哈希和中间首页哈希；首页提示同步未完成，并引导学员“让Agent检查／修复我的知识库”，不要求运行底层 Python API 名称。逐文件临时写入、fsync、原子 replace；首页最后恢复，最后才保存 ready。
- 中断后可接受旧哈希或该事务预期哈希，重新生成并完成；中断后用户另行编辑的文件仍会触发冲突。不要求删除临时文件；失败产生的 `.pkb-write-*.tmp` 可留存，正常同步不依赖 unlink。
- 废弃受管 Markdown 改为明确“已失效”的指路页，不保留旧当前说明。不删除旧附件，不改来源原件或 release 原件。Excel `.xlsx`、`.csv`、`.webp` 及核心支持的其他非活跃附件保留原扩展名与原始字节，可由本机对应应用打开，不承诺 Obsidian 内置预览 Excel。Markdown/HTML（含 `.htm`）等原件以 `.bin` 附件保存，避免变成可执行 HTML 或被当成当前 Markdown 导航；全文在安全转义后的资料页可读。
- 无变化同步为零写，个人笔记和 Obsidian 设置不变。inspect 全程只读，包含缓存与受管内容一致性检查，不自动修复。

单文件替换原子，但整个 Vault 不是一次性原子切换：中断期间用户可能在 Obsidian 缓存或已打开的资料页看到旧内容，首页和 inspect 明确提示不完整，必须恢复后才作为最新视图使用。

## 授权撤销

父入口 revoke 调 `invalidate`，首页改为“Agent 读取授权已撤销”，同步清单置 invalidated。若首页被用户编辑，保留编辑并报告冲突；调用方仍须独立完成授权撤销并向用户报告提示页未更新。

**旧本地页面、附件及个人笔记仍然存在，Obsidian 仍能查看。** 撤销 Agent 读取权不等于远程擦除，也不能阻断本地可见性。本模块不宣称删除、不扫描或擦除这些副本。授权恢复后，在同一知识库写锁内通过有效 view 重新 sync。

## 使用验证

在目标环境确认首次打开、资料页链接、原件页码、重复更新与个人笔记保护。代码回归不替代宿主GUI与用户真实资料的最终验收。发布包不包含实机测试记录或业务样本。

## 并发人工保存与撤销后恢复

预检后用户仍可能在Obsidian保存自动页，单纯“覆盖前再读一次”无法消除竞争。公开页面现在先将旧文件移入唯一的 `.pkb-preserved` 保留区，核对捕获字节，再以硬链接原子发布到不存在的目标；用户重新保存出同名文件会使发布拒绝，不覆盖它。旧打开句柄的迟到写入仍落在保留文件上，不丢弃；完成前和后续检查会检测保留稿变化，返回冲突及保留位置。记录先于移走原文件持久化，中断可恢复。自动备份暂不清理；文件系统须支持本地硬链接（常见APFS/NTFS），不支持时明确失败，不退回破坏性覆盖。个人笔记和.obsidian从未参与该协议。

预检已发现人工冲突时仍零写。并发冲突发生在写入阶段时会保留已完成页和in_progress状态，不能声称全视图完成。用户先把冲突保留稿移入个人笔记，再明确决定恢复自动页；不要求学员编辑清单。修改应优先放个人笔记，自动页只是生成物。

撤销已提交但首页失效提示失败时，重新“撤销”或“检查／修复”只执行最小控制维护，不读取原件、发布正文或旧索引，不重复写撤销时间；只读Obsidian状态也可说明未完成。授权已撤销仍是事实，call/update继续拒绝。首次撤销的视图未完成返回退出码3，不将所有步骤说成成功。
