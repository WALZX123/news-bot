import { useEffect, useRef, useState } from 'react';

type Request = <T>(path: string, options?: RequestInit) => Promise<T>;
type Period = {report_type: string; period_start: string; period_end: string};
type Outcome = Period & {status: string; code?: string; available_news?: number; sampled_news?: number};
type ReportRow = Period & {status: string; error_code: string};
type ReportRetry = Outcome & {started_at?: string; duration_ms?: number; report_id?: number};
type ReportState = {items: ReportRow[]; job: ReportRetry};
export type Stage = {
  stage: string; status: string; duration_ms: number; code?: string;
  periods?: Outcome[]; failed_sources?: {source: string; code: string}[];
  fetched?: number; inserted?: number; selected?: number; linked?: number; sent?: number;
  generated?: number; attempted?: number; failed?: number;
};
export type ModelUsage = {
  operation?: string; input_tokens?: number; output_tokens?: number; latency_ms?: number; error_code?: string;
};

const stageNames: Record<string, string> = {
  prepare: '加载配置与连接', recovery: '检查批次状态', collection: '采集',
  ingestion: '校验与入库', selection: '选稿与评分', analysis: '简报生成',
  delivery: '钉钉投递', reports: '周期报告', summaries: '单篇摘要',
  scoring: '兴趣评分', report: '周期报告', article_summary: '单篇摘要',
};
const statuses: Record<string, string> = {
  running: '运行中', succeeded: '完成', partial: '部分失败', failed: '失败', waiting: '等待', reused: '复用已存结果',
  skipped: '跳过', insufficient: '资料不足', busy: '报告任务正在占用', unknown: '送达待核验', generating: '生成中',
};
const reportTypes: Record<string, string> = {weekly:'周报', monthly:'月报', quarterly:'季报', yearly:'年报'};
const phases: Record<string, string> = {read:'读取资料', budget:'整理样本', model:'模型生成或校验', persist:'保存结果'};
const reasons: Record<string, string> = {
  report_failed: '旧记录只保存了失败状态',
  model_budget: '达到本轮模型预算或时限', internal_error: '内部处理失败', database_error: '数据库操作失败',
  timeout: '超时', system_error: '本机 I/O 失败', invalid_model_response: '模型输出校验失败',
  invalid_json: '模型输出不是有效 JSON', schema_fields: '模型遗漏或增加了字段',
  schema_list: '模型列表的类型或条数不符合要求', schema_text: '模型文本类型或长度不符合要求',
  invalid_evidence: '引用未指向本批输入材料', incomplete_model_output: '模型输出未完成',
  model_timeout: '模型请求超时', model_network: '模型网络连接失败', model_quota: '模型额度不足',
  model_rate_limited: '模型接口限流', model_unauthorized: '模型密钥失效', model_forbidden: '模型服务拒绝访问',
  model_invalid_request: '请求参数被模型服务拒绝', model_provider_error: '模型服务返回错误',
  model_missing_key: '模型密钥未配置', model_not_found: '模型或接口地址不存在',
  rss_failed: '部分订阅采集失败', url_policy: '链接不符合采集规则', http_status: '站点返回非成功状态',
  content_type: '站点没有返回 RSS 或 Atom', invalid_feed: '订阅格式无法解析',
  invalid_article: '部分稿件字段无效', no_candidates: '当前没有合适的候选稿件',
  no_visible_links: '简报缺少可核对的原文链接', outside_push_window: '当前在推送窗口外',
  receipt_unknown: '等待人工核验回执', receipt_recovery: '回执保存失败，等待恢复',
  dingtalk_rejected: '钉钉拒绝这次投递', report_period_failed: '有报告周期生成失败',
  insufficient_material: '该周期的可用新闻不足', no_report_due: '暂无可生成的周期报告',
  report_claimed: '报告周期已被其他任务领取', period_not_failed: '该周期已有结果或状态已更新',
  invalid_period: '报告日期不符合已结束的周期', no_pending_summaries: '暂无待生成摘要',
  summary_failed: '部分摘要未通过校验',
};

function issue(code?: string) {
  if (!code) return '';
  const [prefix, detail] = code.split('.');
  return detail ? (phases[prefix] ? phases[prefix] + ' · ' : '') + (reasons[detail] ?? detail) : reasons[prefix] ?? prefix;
}

function periodLabel(period: Period) {
  return (reportTypes[period.report_type] ?? period.report_type) + ' ' +
    period.period_start + ' 至 ' + period.period_end + '（不含）';
}

function duration(milliseconds?: number) {
  if (milliseconds === undefined) return '—';
  return milliseconds < 1000 ? milliseconds + ' 毫秒' : (milliseconds / 1000).toFixed(1) + ' 秒';
}

const figures = {
  fetched:'取回', inserted:'入库', selected:'选中', linked:'简报原文', sent:'已推送', generated:'生成',
  attempted:'尝试', failed:'失败',
} as const;

export function StageDetails({stages, usage}: {stages?: Stage[]; usage?: ModelUsage[]}) {
  if (!stages?.length) return null;
  return <details className="run-diagnostics">
    <summary>环节明细</summary>
    <ol>
      {stages.map((step, index) => <li key={step.stage + index}>
        <strong>{stageNames[step.stage] ?? step.stage}</strong> · {statuses[step.status] ?? step.status} · {duration(step.duration_ms)}
        {step.code && <p>{issue(step.code)}</p>}
        {Object.entries(figures).some(([key]) => typeof step[key as keyof typeof figures] === 'number') && <p>
          {Object.entries(figures).flatMap(([key, label]) => {
            const value = step[key as keyof typeof figures];
            return value === undefined ? [] : [label + ' ' + value];
          }).join(' · ')}
        </p>}
        {!!step.failed_sources?.length && <ul>{step.failed_sources.map(({source, code}) => <li key={source}>{source} · {issue(code)}</li>)}</ul>}
        {!!step.periods?.length && <ul>{step.periods.map(period => <li key={period.report_type + period.period_start}>
          {periodLabel(period)} · {statuses[period.status] ?? period.status}{period.code ? ' · ' + issue(period.code) : ''}
        </li>)}</ul>}
      </li>)}
    </ol>
    {!!usage?.length && <details><summary>模型调用</summary><ul>
      {usage.map((call, index) => <li key={index}>
        {stageNames[call.operation ?? ''] ?? call.operation ?? '模型'} · {duration(call.latency_ms)}
        {call.error_code ? ' · ' + issue('model_' + call.error_code) : ' · Token ' + (call.input_tokens ?? '—') + ' / ' + (call.output_tokens ?? '—')}
      </li>)}
    </ul></details>}
  </details>;
}

export function ReportRetries({request, version, refresh, inspect}: {
  request: Request; version: number; refresh: () => void; inspect: (id: number) => void;
}) {
  const [state, setState] = useState<ReportState>();
  const [problem, setProblem] = useState('');
  const [revision, reload] = useState(0);
  const [submitting, setSubmitting] = useState(false);
  const completed = useRef<string | undefined>(undefined);
  useEffect(() => {
    const controller = new AbortController();
    let timer: number;
    async function load() {
      try {
        const next = await request<ReportState>('report-jobs', {signal: controller.signal});
        if (controller.signal.aborted) return;
        setState(next);
        if (next.job.status === 'running') timer = window.setTimeout(load, 1400);
      } catch (error) {
        if (!controller.signal.aborted) setProblem((error as Error).message);
      }
    }
    void load();
    return () => { controller.abort(); window.clearTimeout(timer); };
  }, [request, version, revision]);
  const job = state?.job;
  useEffect(() => {
    if (job?.status === 'succeeded' && job.started_at && job.started_at !== completed.current) {
      completed.current = job.started_at;
      refresh();
    }
  }, [job, refresh]);
  async function retry(row: ReportRow) {
    setProblem('');
    setSubmitting(true);
    try {
      const next = await request<ReportRetry>('report-jobs/retry', {
        method: 'POST', headers: {'Content-Type':'application/json'},
        body: JSON.stringify({report_type:row.report_type, period_start:row.period_start, period_end:row.period_end}),
      });
      setState(current => ({items: current?.items ?? [], job: next}));
      reload(value => value + 1);
    } catch (error) {
      setProblem((error as Error).message);
    } finally {
      setSubmitting(false);
    }
  }
  return <section className="card operations-section report-retries">
    <div className="operations-heading"><h2>报告失败与重跑</h2></div>
    {problem && <p role="alert" className="operation-alert error">{problem}
      <button className="button secondary" onClick={() => {setProblem(''); reload(value => value + 1);}}>刷新状态</button></p>}
    {job?.started_at && <p role="status">{periodLabel(job)} · {statuses[job.status] ?? job.status}
      {job.duration_ms !== undefined && ' · ' + duration(job.duration_ms)}
      {job.code && ' · ' + issue(job.code)}
      {job.report_id && <button className="text-button" onClick={() => inspect(job.report_id!)}>查看报告</button>}
    </p>}
    {state?.items.length ? <ul className="detail-list">{state.items.map(row => <li key={row.report_type + row.period_start}>
      <div className="operations-heading"><strong>{periodLabel(row)}</strong>
        {row.status === 'failed' && <button className="button secondary" disabled={submitting || job?.status === 'running'}
          onClick={() => retry(row)}>单独重跑</button>}</div>
      <p>{statuses[row.status] ?? row.status}{row.error_code ? ' · ' + issue(row.error_code) : ''}</p>
    </li>)}</ul> : <p className="operation-note">{state ? '暂无待处理的失败周期。' : '正在读取报告状态…'}</p>}
  </section>;
}
