import { useEffect, useState } from 'react';
import type { FormEvent } from 'react';
import { Activity, Clock3, RefreshCw } from 'lucide-react';
import { StageDetails } from './run-diagnostics';
import type { ModelUsage as Usage, Stage } from './run-diagnostics';

type Request = <T>(path: string, options?: RequestInit) => Promise<T>;
type Probe = { ok: boolean; checked_at: string; model?: string; latency_ms?: number; message?: string; usage?: Usage[] };
type Settings = {
  revision: string;
  yaml: string | null;
  ai: {
    base_url: string; model: string; effective_model: string; api_key_configured: boolean; protected_key: boolean;
    max_tokens: number; timeout_seconds: number; overrides: string[];
  };
  schedule: {
    fetch_interval_minutes: number; window_start: number; window_end: number;
    max_news_per_run: number; ai_interests: string; auto_summarize: boolean; summary_limit: number;
    max_candidates_per_run: number; max_news_age_hours: number; fetch_task_timeout: number; incremental_mode: boolean;
  };
  credentials: { webhook_configured: boolean; dingtalk_secret_configured: boolean; mysql_password_configured: boolean };
  database_restart_required: boolean;
  last_probe: Probe | null;
};
type Run = {
  started_at: string; mode: 'collect' | 'full'; status: string; message?: string;
  fetched?: number; inserted?: number; selected?: number; sent?: number; errors?: number;
  report_count?: number; model_usage?: Usage[];
  visible_links?: number;
  stages?: Stage[];
};
type Automation = {
  active: boolean; state: string; mode: 'collect' | 'full' | null;
  started_at: string | null; next_run_at: string | null; recent_runs: Run[];
  heartbeat_at?: string | null; requested_mode?: 'collect' | 'full' | null; stopping?: boolean;
};

const dates = new Intl.DateTimeFormat('zh-CN', {
  timeZone: 'Asia/Shanghai', month: '2-digit', day: '2-digit',
  hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false,
});
const date = (value?: string | null) => value ? dates.format(new Date(value)) : '—';
const modes = { collect: '仅采集', full: '完整流水线' };
const labels: Record<string, string> = {
  starting: '启动中', running: '执行中', waiting: '等待下次运行', stopped: '已停止', stalled: '心跳超时', slow: '本轮耗时异常',
  succeeded: '完成', failed: '失败', partial: '部分失败', skipped: '无待处理内容', busy: '已有任务执行',
};
const tabs = [
  { id: 'ai', label: 'AI 模型' }, { id: 'automation', label: '调度参数' },
  { id: 'credentials', label: '连接凭据' }, { id: 'yaml', label: '高级 YAML' },
] as const;
type Tab = typeof tabs[number]['id'];

function NumberField({ label, name, initial, min, max }: {
  label: string; name: string; initial: number; min: number; max: number;
}) {
  return <label>{label}<input name={name} type="number" required min={min} max={max} defaultValue={initial} /></label>;
}

function CredentialField({ label, name, configured }: { label: string; name: string; configured: boolean }) {
  return <label>{label}<input name={name} type="password" autoComplete="new-password" maxLength={4096}
    placeholder={configured ? '已配置 · 留空保留' : '未配置 · 输入新值'} /></label>;
}

function usageText(usage: Usage[] = []) {
  if (!usage.length) return '—';
  const input = usage.reduce((sum, row) => sum + (row.input_tokens ?? 0), 0);
  const output = usage.reduce((sum, row) => sum + (row.output_tokens ?? 0), 0);
  return input.toLocaleString() + ' / ' + output.toLocaleString();
}

export function SettingsPanel({ request }: { request: Request }) {
  const [tab, setTab] = useState<Tab>('ai');
  const [settings, setSettings] = useState<Settings>();
  const [saving, setSaving] = useState(false);
  const [testing, setTesting] = useState(false);
  const [notice, setNotice] = useState('');
  const [error, setError] = useState('');
  const [reload, setReload] = useState(0);
  const [formVersion, setFormVersion] = useState(0);
  useEffect(() => {
    const controller = new AbortController();
    request<Settings>('settings', { signal: controller.signal }).then(value => {
      if (!controller.signal.aborted) { setSettings(value); setFormVersion(version => version + 1); }
    }).catch(reason => {
      if (!controller.signal.aborted) setError(reason.message);
    });
    return () => controller.abort();
  }, [request, reload]);

  async function save(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!settings || saving) return;
    const form = new FormData(event.currentTarget);
    const numbers = new Set(['max_tokens','timeout_seconds','fetch_interval_minutes','window_start','window_end','max_news_per_run',
      'summary_limit','max_candidates_per_run','max_news_age_hours','fetch_task_timeout']);
    const values = Object.fromEntries([...form].map(([name, value]) => [name, numbers.has(name) ? Number(value) : String(value)]));
    if (tab === 'automation') Object.assign(values, {auto_summarize: form.has('auto_summarize'), incremental_mode: form.has('incremental_mode')});
    setSaving(true);
    setError('');
    setNotice('');
    try {
      const next = await request<Settings>(tab === 'yaml' ? 'settings' : 'settings/' + tab, {
        method: 'PUT', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ revision: settings.revision, ...values }),
      });
      setSettings(next);
      setFormVersion(value => value + 1);
      setNotice('配置已备份并保存。');
    } catch (reason) {
      setError((reason as Error).message);
    } finally {
      setSaving(false);
    }
  }

  async function testModel() {
    if (!settings || testing) return;
    const revision = settings.revision;
    setTesting(true);
    setError('');
    try {
      const last_probe = await request<Probe>('settings/ai/test', {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ revision }),
      });
      setSettings(current => current?.revision === revision ? { ...current, last_probe } : current);
    } catch (reason) {
      setError((reason as Error).message);
    } finally {
      setTesting(false);
    }
  }

  const ai = settings?.ai;
  const schedule = settings?.schedule;
  const probe = settings?.last_probe;
  return <div className="operations">
    <div className="page-title"><div><h1>系统设置</h1><p>管理后台配置，调度器从下一轮开始读取新设置。</p></div>
      <button type="button" className="button secondary" onClick={() => { setError(''); setReload(value => value + 1); }}><RefreshCw size={16} />重新读取</button>
    </div>
    <div className="settings-tabs" aria-label="设置分类">
      {tabs.map(item => <button type="button" className={item.id === tab ? 'selected' : ''} aria-pressed={item.id === tab}
        key={item.id} onClick={() => { setTab(item.id); setError(''); setNotice(''); }}>{item.label}</button>)}
    </div>
    {error && <p className="operation-alert error" role="alert">{error}</p>}
    {notice && <p className="operation-alert" role="status">{notice}</p>}
    {settings?.database_restart_required && <p className="operation-alert">MySQL 配置已变更，重启工作台后会切换连接池。</p>}
    {!settings && !error && <p role="status">正在读取配置…</p>}
    {settings && <form key={tab + formVersion} onSubmit={save} className="card operations-section">
      {tab === 'ai' && ai && <>
        <h2>模型连接</h2><p className="operation-note">使用 DeepSeek Chat Completions 接口，也可填写兼容服务的地址。</p>
        <div className="settings-grid">
          <label>Base URL<input name="base_url" defaultValue={ai.base_url} placeholder="使用 DeepSeek 时可留空" maxLength={2048} /></label>
          <label>模型名称<input name="model" defaultValue={ai.model} required maxLength={200} /></label>
          <CredentialField label="API Key" name="api_key" configured={ai.api_key_configured} />
          <NumberField label="请求超时（秒）" name="timeout_seconds" initial={ai.timeout_seconds} min={1} max={300} />
          <NumberField label="最大输出 Token" name="max_tokens" initial={ai.max_tokens} min={256} max={32768} />
        </div>
        {ai.overrides.length > 0 && <p className="operation-note">当前进程以环境变量 {ai.overrides.join('、')} 为准。</p>}
        {ai.protected_key && <p className="operation-note">模型密钥保存在当前 Windows 用户的加密保护区。</p>}
        {ai.model !== ai.effective_model && <p className="operation-note">请求模型标识为 <code>{ai.effective_model}</code>。</p>}
        <div className="operation-actions"><button className="button primary" disabled={saving} type="submit">保存模型配置</button>
          <button className="button secondary" type="button" disabled={testing || saving} onClick={testModel}>
            <Activity size={16} />{testing ? '正在请求模型…' : '测试已保存配置'}
          </button></div>
        {probe && <p className={'operation-alert' + (probe.ok ? '' : ' error')} role="status">
          {probe.ok ? '调用成功 · ' + probe.model + ' · ' + probe.latency_ms + ' ms · Token ' + usageText(probe.usage) : probe.message}
          {' · '}{date(probe.checked_at)}
        </p>}
      </>}
      {tab === 'automation' && schedule && <>
        <h2>采集与推送</h2><p className="operation-note">推送窗口按北京时间计算，结束时间填 24 表示当天结束。</p>
        <div className="settings-grid">
          <NumberField label="采集间隔（分钟）" name="fetch_interval_minutes" initial={schedule.fetch_interval_minutes} min={1} max={10080} />
          <NumberField label="推送开始（小时）" name="window_start" initial={schedule.window_start} min={0} max={23} />
          <NumberField label="推送结束（小时）" name="window_end" initial={schedule.window_end} min={0} max={24} />
          <NumberField label="每轮推送上限（0 为不限）" name="max_news_per_run" initial={schedule.max_news_per_run} min={0} max={500} />
          <NumberField label="每轮补生成摘要（篇）" name="summary_limit" initial={schedule.summary_limit} min={1} max={40} />
          <NumberField label="每轮候选新闻上限" name="max_candidates_per_run" initial={schedule.max_candidates_per_run} min={1} max={500} />
          <NumberField label="新闻新鲜度（小时）" name="max_news_age_hours" initial={schedule.max_news_age_hours} min={1} max={8760} />
          <NumberField label="采集总时限（秒）" name="fetch_task_timeout" initial={schedule.fetch_task_timeout} min={1} max={600} />
        </div>
        <label className="desk-confirm"><input name="auto_summarize" type="checkbox" defaultChecked={schedule.auto_summarize} />采集后自动生成单篇 AI 摘要</label>
        <label className="desk-confirm"><input name="incremental_mode" type="checkbox" defaultChecked={schedule.incremental_mode} />自动选稿时减少与已发内容相似的新闻</label>
        <label>AI 兴趣描述<textarea name="ai_interests" rows={5} maxLength={10000} defaultValue={schedule.ai_interests} /></label>
        <div className="operation-actions"><button type="submit" className="button primary" disabled={saving}>保存调度参数</button></div>
      </>}
      {tab === 'credentials' && <>
        <h2>钉钉与数据库</h2><p className="operation-note">可填写新凭据。留空会保留当前值。</p>
        <div className="settings-grid">
          <CredentialField label="钉钉 Webhook URL" name="webhook_url" configured={settings.credentials.webhook_configured} />
          <CredentialField label="钉钉签名密钥" name="dingtalk_secret" configured={settings.credentials.dingtalk_secret_configured} />
          <CredentialField label="MySQL 密码" name="mysql_password" configured={settings.credentials.mysql_password_configured} />
        </div>
        <div className="operation-actions"><button type="submit" className="button primary" disabled={saving}>保存凭据</button></div>
      </>}
      {tab === 'yaml' && <>
        <h2>完整配置</h2><p className="operation-note">可编辑 RSS、筛选词、来源权重、报告开关、调用预算和 MySQL 参数。请保留敏感字段占位符。</p>
        {settings.yaml !== null ? <>
          <label>YAML 配置<textarea className="yaml-editor" name="yaml" rows={20} spellCheck={false} defaultValue={settings.yaml} /></label>
          <div className="operation-actions"><button type="submit" className="button primary" disabled={saving}>校验并保存 YAML</button></div>
        </> : <p className="operation-alert">当前配置格式无法安全显示。可以在本地 config.yaml 中编辑。</p>}
      </>}
    </form>}
  </div>;
}

export function AutomationPanel({ request }: { request: Request }) {
  const [state, setState] = useState<Automation>();
  const [error, setError] = useState('');
  const [reload, setReload] = useState(0);
  const [commandPending, setCommandPending] = useState(false);
  useEffect(() => {
    const controller = new AbortController();
    let timer: number | undefined;
    async function read() {
      try {
        const value = await request<Automation>('automation', { signal: controller.signal });
        if (!controller.signal.aborted) { setState(value); setError(''); }
      } catch (reason) {
        if (!controller.signal.aborted) setError((reason as Error).message);
      } finally {
        if (!controller.signal.aborted) timer = window.setTimeout(read, 5000);
      }
    }
    void read();
    return () => { controller.abort(); window.clearTimeout(timer); };
  }, [request, reload]);
  async function command(action: 'collect' | 'full' | 'stop') {
    const confirmed = action === 'full' && window.confirm('开启完整自动化后，后台将在推送窗口内自动向当前钉钉机器人发送简报。确认开启？');
    if (action === 'full' && !confirmed) return;
    setCommandPending(true);
    try {
      await request('editorial/automation', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({action, confirmed})});
      setReload(value => value + 1);
    } catch (reason) { setError((reason as Error).message); }
    finally { setCommandPending(false); }
  }
  return <div className="operations">
    <div className="page-title"><div><h1>自动化运行</h1><p>查看后台调度和每轮采集、分析、推送的结果。</p></div>
      <button type="button" className="button secondary" onClick={() => setReload(value => value + 1)}><RefreshCw size={16} />刷新</button>
    </div>
    {error && <p className="operation-alert error" role="alert">{error}</p>}
    <section className="card operations-section">
      <div className="operations-heading"><h2>后台调度器</h2>
        {state && <span className={'badge ' + (state.active ? 'green' : 'neutral')}>{labels[state.state] || state.state}</span>}
      </div>
      <div className="automation-status">
        <div><Activity size={16} /><span>运行模式</span><strong>{state?.mode ? modes[state.mode] : '—'}</strong></div>
        <div><Clock3 size={16} /><span>启动时间</span><strong>{date(state?.started_at)}</strong></div>
        <div><Clock3 size={16} /><span>下一轮</span><strong>{date(state?.next_run_at)}</strong></div>
      </div>
      <div className="operation-actions">
        <button className="button secondary" disabled={commandPending || (state?.active && state.mode === 'collect' && !state.requested_mode)} onClick={() => command('collect')}>启用仅采集</button>
        <button className="button primary" disabled={commandPending || (state?.active && state.mode === 'full' && !state.requested_mode)} onClick={() => command('full')}>启用自动推送</button>
        <button className="button secondary" disabled={commandPending || !state?.active || state.stopping} onClick={() => command('stop')}>停止后台</button>
      </div>
      {state?.heartbeat_at && <p className="operation-note">最近心跳 {date(state.heartbeat_at)}</p>}
      {state?.requested_mode && <p className="operation-alert">本轮结束后切换到{modes[state.requested_mode]}。</p>}
      {state?.stopping && <p className="operation-alert">本轮结束后停止。</p>}
    </section>
    <section className="card operations-section"><h2>近期运行</h2>
      {state?.recent_runs.length ? <div className="operations-history">
        {state.recent_runs.map(run => <article key={run.started_at}>
          <header><strong>{labels[run.status] || run.status}</strong><span>{modes[run.mode]} · {date(run.started_at)}</span></header>
          <p>{run.message}</p>
          <div className="run-counts"><span>采集 {run.fetched ?? 0}</span><span>入库 {run.inserted ?? 0}</span>
            <span>分析输入 {run.selected ?? 0}</span><span>已确认批次 {run.sent ? 1 : 0}</span>
            {typeof run.visible_links === 'number' && <span>消息原文链接 {run.visible_links}</span>}
            <span>报告 {run.report_count ?? 0}</span><span>错误 {run.errors ?? 0}</span>
            <span>Token（输入 / 输出） {usageText(run.model_usage)}</span></div>
          <StageDetails stages={run.stages} usage={run.model_usage} />
        </article>)}
      </div> : <p className="operation-note">{state ? '调度器完成首轮后会显示执行结果。' : '正在读取运行状态…'}</p>}
    </section>
  </div>;
}
