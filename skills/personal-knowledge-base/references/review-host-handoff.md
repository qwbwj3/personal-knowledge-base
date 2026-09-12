# 当前创作Agent如何完成核对衔接

这份说明供Agent使用，学员不编辑计划或JSON。程序的全文覆盖检查不是自然语言真伪的万能判定器。

1. 保留原稿，以明确的产品意图调用review。先读JSON的status和review_validation，进程退出0不等于审核通过。
2. `review_contract`保留原稿、Unicode字符位置和快照；`review_plan`是实际使用的units/atoms/coverage_audit。不得删除已确定的数字、否定、条件或原文区域。若缺产品对象，先识别任务是否真有产品事实；不要为了消除错误随便选一个产品。
3. 程序能确定的supported/contradicted/missing_condition直接保留。只有needs_semantic_review允许补交语义判断；未具备证据时保留未决。个人亲历不能由产品条款代为证明。
4. 原文不够时按retrieval_scope.next_cursor取下一页；每个产品条目的review_source_chunks是本页完整块。根据document_id、revision_id、chunk_id和块内start/end/quote形成精确引用，不能用其他年度、待确认来源或个人文案为产品作证。
5. 复制`review_guidance.semantic_results_template`，在results中放入以下形状的实际判断，写入临时文件后以`--semantic-results-file`提交；query、目标、自定义计划如有必须与准备时相同。

```json
{
  "atom_id": "实际待语义原子项ID",
  "verdict": "supported",
  "reason": "说明原文如何支持本句以及不能扩张的条件",
  "citations": [{
    "document_id": "本轮来源ID",
    "revision_id": "本轮修订ID",
    "chunk_id": "本轮文本块ID",
    "start": 0,
    "end": 8,
    "quote": "这里须替换为精确原文"
  }]
}
```

上述ID、位置和文字都是占位符，不能直接提交。合法verdict为supported、contradicted或needs_semantic_review；支持/矛盾必须给出当前合格引用。返回invalid_semantic_result时按具体错误处理，不能改写输出或覆盖程序已判定的矛盾。快照变化必须重新核对。

对于建议、转场、虚构演绎、提问和个人表达，先阅读review_plan中的原始覆盖单元，结合任务区分它们与产品承诺。若当前协议无法收妥这种分类，明确指出该部分属于Agent人工核对、后端仍未决，不把整篇宣布通过；也不必把有明确模拟标签的创作稿扣住不交。稿件、来源和实际未决应同时交付，最终真实亲历及发布决定由用户确认。
