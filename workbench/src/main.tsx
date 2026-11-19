import React, { lazy, Suspense, useEffect, useRef, useState } from 'react';
import { createRoot } from 'react-dom/client';
const Markdown = lazy(() => import('react-markdown'));
import {
  Activity, ArrowDownToLine, ArrowLeft, ArrowRight, ArrowUpRight, Check, ChevronDown, ChevronLeft, ChevronRight,
  CircleAlert, Clock3, Copy, Database, FileChartColumn, FilePenLine, House, Inbox, Layers3, ListFilter, Menu,
  Newspaper, Plus, RefreshCw, Rss, Search, Send, Settings2, X,
} from 'lucide-react';
import type { LucideIcon } from 'lucide-react';
import { AutomationPanel, SettingsPanel } from './operations';
import { CollectControl, PublishingDesk, ReadingSummary, RelatedStories } from './editorial';
import { ReportRetries } from './run-diagnostics';
import { NewsEditor, SourceEditor } from './management';
import './styles.css';

type Page = 'home' | 'news' | 'publish' | 'sources' | 'batches' | 'reports' | 'drafts' | 'automation' | 'settings';
type Article = { id: number; title: string; summary: string; source: string; url: string | null; processing_state: string; skip_reason: string; published_at: string | null; created_at: string; revision: string };
type NewsPage = { items: Article[]; total: number; page: number; size: number };
type Segment = { day: string; count: number };
type Summary = {
  counts: { total: number; pending: number; expired: number; batched: number; skipped: number; sent: number; reports: number; sources: number; last_ingested: string | null };
  recent: Article[]; series: Segment[]; archive: Segment[];
  configuration: { interval: number; start: number; end: number; interest: string };
};
type Source = { name: string; url: string | null; count: number; latest: string | null; checked: string | null; error: string | null;
  entries: number | null; returned: number | null; successes: number | null; failures: number | null; recent_count: number; recent_sent: number };
type Batch = { id: string; status: string; news_count: number; error_code: string; created_at: string; updated_at: string; attempts: number; failures: number };
type Preview = { status?: string; markdown?: string; visible_links?: number; analyzed?: number; truncated?: boolean; recorded?: boolean; message?: string };
type BatchDetail = Omit<Batch, 'attempts'> & {
  analysis: { headline?: string; _publication_mode?: string; _omitted_observations?: number;
    top8?: { title: string; summary: string; url?: string }[] };
  attempts: { status: string; error_code: string; started_at: string; finished_at: string | null }[];
  preview: Preview | null;
};
type Report = { id: number; title: string; report_type: string; period_start: string; period_end: string; news_count: number; created_at: string };
type ReportDetail = { id: number; title: string; content: {
  summary?: string; outlook?: string; trends?: string[]; domain_analysis?: Record<string,string>; data_summary?: string;
  top_events?: { event: string; significance: string; evolution?: string; url?: string; urls?: string[] }[];
  _coverage?: { available_news: number; sampled_news: number; note?: string };
} };
type Draft = { id: number; title: string; body: string; article_ids: number[]; updated_at: string; revision: number; archived_at?: string | null };

const navigation: { id: Page; label: string; icon: LucideIcon }[] = [
  { id: 'home', label: '工作台', icon: House },
  { id: 'news', label: '新闻库', icon: Newspaper },
  { id: 'publish', label: '发布台', icon: Send },
  { id: 'sources', label: '来源管理', icon: Rss },
  { id: 'drafts', label: '简报草稿', icon: FilePenLine },
  { id: 'batches', label: '推送批次', icon: Send },
  { id: 'reports', label: '周期报告', icon: FileChartColumn },
  { id: 'automation', label: '自动化运行', icon: Activity },
  { id: 'settings', label: '系统设置', icon: Settings2 },
];
const statusNames: Record<string, [string, string]> = {
  manual_ready: ['手动待发送', 'amber'], manual_send_failed: ['手动发送失败', 'red'],
  pending: ['待处理', 'blue'], batched: ['已组批', 'blue'], sent: ['所属批次已确认', 'green'],
  skipped: ['已筛除', 'neutral'], analyzing: ['分析中', 'blue'], analysis_failed: ['分析失败', 'red'],
  ready: ['待发送', 'blue'], sending: ['发送中', 'blue'], send_failed: ['发送失败', 'red'],
  unknown: ['待核验', 'amber'], canceled: ['已取消', 'neutral'], succeeded: ['成功', 'green'],
  failed: ['失败', 'red'], started: ['进行中', 'blue'], reconciled_sent: ['人工确认送达', 'green'],
  reconciled_not_sent: ['人工确认未送达', 'amber'],
};
const periodNames: Record<string, string> = { weekly: '周报', monthly: '月报', quarterly: '季报', yearly: '年报' };
const reasonNames: Record<string, string> = {
  invalid_legacy_record: '旧记录缺少可用字段', outside_news_age: '超出新闻时效',
  keywords: '关键词筛选', low_interest: '兴趣分不足', similar_sent_or_batch: '标题与已有报道相似',
};
const issueNames: Record<string, string> = {
  timeout: '请求超时', http_status: '来源返回非成功状态', unsafe_url: '链接不符合采集规则',
  content_type: '响应不是订阅格式', content_encoding: '响应压缩格式不受支持', xml_declaration: 'XML 含不受支持的声明',
  invalid_feed: '无法解析订阅', analysis_failed: '模型分析失败', interrupted_send: '发送中断',
  repacked_budget: '重新划分批次', manual_reconciliation: '人工核验',
};

function fmt(value: string | null | undefined, time = false) {
  if (!value) return '—';
  const date = new Date(value);
  if (!Number.isFinite(date.getTime())) return '—';
  return new Intl.DateTimeFormat('zh-CN', {
    timeZone: 'Asia/Shanghai', month: '2-digit', day: '2-digit',
    ...(time ? { hour: '2-digit', minute: '2-digit', hour12: false } : {}),
  }).format(date);
}
const digits = (number: number | undefined) => number == null ? '—' : number.toLocaleString('zh-CN');
const clock = (hour: number) => String(hour).padStart(2, '0') + ':00';
const statusFor = (status: string) => statusNames[status] || [status || '未归类', 'neutral'];
function external(url?: string | null) {
  if (!url) return undefined;
  try { const parsed = new URL(url); return ['http:', 'https:'].includes(parsed.protocol) ? parsed.href : undefined; }
  catch { return undefined; }
}

async function api<T>(path: string, options?: RequestInit): Promise<T> {
  const response = await fetch('/api/' + path, options);
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : '请求未完成，请检查输入或稍后重试。');
  return data as T;
}
function useData<T>(path: string | null, refresh: number) {
  const [value, setValue] = useState<T>();
  const [problem, setProblem] = useState('');
  const [busy, setBusy] = useState(true);
  const previousRefresh = useRef(refresh);
  useEffect(() => {
    const manualRefresh = previousRefresh.current !== refresh;
    previousRefresh.current = refresh;
    if (!path) {
      setBusy(false);
      return;
    }
    const abort = new AbortController();
    setBusy(true);
    setProblem('');
    const request = manualRefresh && (path === 'summary' || path === 'sources') ? path + '?refresh=1' : path;
    api<T>(request, { signal: abort.signal }).then(data => {
      if (!abort.signal.aborted) setValue(data);
    }).catch(error => {
      if (!abort.signal.aborted) setProblem(error.message);
    }).finally(() => {
      if (!abort.signal.aborted) setBusy(false);
    });
    return () => abort.abort();
  }, [path, refresh]);
  return { value, problem, busy };
}
const write = (method: string, body: object): RequestInit => ({
  method, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
});

function Badge({ status }: { status: string }) {
  const [name, color] = statusFor(status);
  return <span className={'badge ' + color}><i />{name}</span>;
}
function Empty({ icon: Icon = Inbox, title, text, action }: { icon?: LucideIcon; title: string; text?: string; action?: React.ReactNode }) {
  return <div className="empty"><div className="empty-icon"><Icon size={22} /></div><strong>{title}</strong>{text && <p>{text}</p>}{action}</div>;
}
function Failure({ message, retry }: { message: string; retry: () => void }) {
  return <div className="failure" role="alert"><CircleAlert size={18} /><span>{message}</span><button className="text-button" onClick={retry}>重试</button></div>;
}
function NewsRows({ items, open, selected, toggle }: { items: Article[]; open: (row: Article) => void; selected?: Map<number, Article>; toggle?: (row: Article) => void }) {
  return <ul className="news-rows">{items.map(row => <li key={row.id}>
    {toggle && <label className="row-check" aria-label={'选择 ' + row.title}><input type="checkbox" checked={selected?.has(row.id) || false}
      disabled={!selected?.has(row.id) && !canEditNews(row)} onChange={() => toggle(row)} /></label>}
    <button className="news-open" onClick={() => open(row)}><span className="news-title">{row.title}</span><span className="news-meta">{row.source}<i />{fmt(row.published_at || row.created_at, true)}</span></button>
    <Badge status={row.processing_state} />
    <button className="row-arrow" aria-label="查看新闻详情" onClick={() => open(row)}><ChevronRight size={17} /></button>
  </li>)}</ul>;
}

function Overview({ summary, busy, open, go, create }: { summary?: Summary; busy: boolean; open: (row: Article) => void; go: (page: Page) => void; create: () => void }) {
  const [preferredRange, setRange] = useState<string>();
  const range = preferredRange ?? (summary?.archive.length && summary.series.every(row => row.count === 0) ? 'archive' : 'week');
  const count = summary?.counts;
  const items = [
    { label: '新闻入库', value: count?.total, icon: Newspaper, tone: 'indigo', info: '新闻库累计', page: 'news' as const },
    { label: '新鲜待处理', value: count?.pending, icon: Inbox, tone: 'blue',
      info: '过期 ' + digits(count?.expired) + ' · 已组批 ' + digits(count?.batched), page: 'news' as const },
    { label: '已确认投递批次', value: count?.sent, icon: Send, tone: 'mint', info: '单位为推送批次', page: 'batches' as const },
    { label: '订阅来源', value: count?.sources, icon: Rss, tone: 'amber', info: '来自机器人配置', page: 'sources' as const },
  ];
  const activity = range === 'archive' ? summary?.archive : summary?.series;
  const largest = Math.max(4, ...((activity || []).map(item => item.count)));
  const peak = largest <= 4 ? 4 : Math.ceil(largest / 4) * 4;
  const total = activity?.reduce((n, row) => n + row.count, 0);
  return <>
    <section className="hero">
      <div>
        <div className="hero-label"><span className="hero-badge"><Activity size={13} /> 编辑工作区</span></div>
        <h1>新闻总览</h1>
        <p>查看采集、整理稿件，掌握每条新闻的进度。</p>
      </div>
      <div className="hero-actions"><button className="button primary" onClick={create}><Plus size={17} /> 新建简报</button></div>
    </section>
    <div className="stat-grid">{items.map(({ label, value, icon: Icon, tone, info, page }) => <button className="stat-card card" key={label} onClick={() => go(page)}>
      <div className="stat-top"><span className={'stat-icon ' + tone}><Icon size={19} /></span><ArrowUpRight size={15} className="stat-arrow" /></div>
      <div className="stat-label">{label}</div><strong>{digits(value)}</strong><span className="stat-info">{info}</span>
    </button>)}</div>
    <div className="overview-grid">
      <section className="card activity-card">
        <header className="card-heading"><div><h2>采集趋势</h2><p>按新闻入库时间统计</p></div>
          <label className="select-control compact"><select aria-label="采集趋势时间范围" value={range} onChange={e => setRange(e.target.value)}><option value="week">最近 7 天</option><option value="archive">最近一次采集周</option></select><ChevronDown size={14} /></label>
        </header>
        <div className="chart-total"><strong>{digits(total)}</strong><span>条新增</span></div>
        <div className="chart" aria-label="每日新闻入库数">
          <div className="chart-scale">{[peak, peak * .75, peak * .5, peak * .25, 0].map(i => <span key={i}>{i}</span>)}</div>
          <div className="chart-field">
            <div className="chart-grid">{[0, 1, 2, 3, 4].map(i => <i key={i} />)}</div>
            <div className="chart-bars">{(activity || []).map(({ day, count }) => <div className="chart-column" key={day}>
              <span className={'chart-bar' + (count === 0 ? ' zero' : '')} style={{ height: count ? count / peak * 100 + '%' : '3px' }} tabIndex={0} aria-label={day + ' ' + count + ' 条'}><em>{count} 条</em></span>
              <span className="chart-day">{day.slice(5).replace('-', '/')}</span>
            </div>)}</div>
          </div>
        </div>
        <div className="chart-note"><span><i />入库新闻</span><span>{total === 0 && !busy ? '这个时段没有新入库' : '北京时间'}</span></div>
      </section>
      <section className="card runtime-card">
        <header className="card-heading"><div><h2>机器人配置</h2><p>当前生效的采集与推送规则</p></div><span className="subtle-icon"><Clock3 size={17} /></span></header>
        <dl className="runtime-list">
          <div><dt><RefreshCw size={16} />采集间隔</dt><dd>{summary ? summary.configuration.interval + ' 分钟' : '—'}</dd></div>
          <div><dt><Send size={16} />钉钉推送窗口</dt><dd>{summary ? clock(summary.configuration.start) + ' – ' + clock(summary.configuration.end) : '—'}</dd></div>
          <div><dt><Database size={16} />最近一条入库</dt><dd>{fmt(count?.last_ingested, true)}</dd></div>
        </dl>
        <div className="interest"><span>关注方向</span><p>{summary?.configuration.interest || '暂未配置兴趣筛选'}</p></div>
      </section>
    </div>
    <section className="card recent">
      <header className="card-heading"><div><h2>最近入库</h2><p>按入库时间排列</p></div><button className="button quiet" onClick={() => go('news')}>查看新闻库 <ArrowRight size={15} /></button></header>
      {summary?.recent.length ? <NewsRows items={summary.recent} open={open} /> : <Empty title={busy ? '正在读取新闻库' : '暂无入库新闻'} />}
    </section>
  </>;
}

function PageTitle({ title, detail, actions }: { title: string; detail: string; actions?: React.ReactNode }) {
  return <header className="page-title"><div><h1>{title}</h1><p>{detail}</p></div>{actions}</header>;
}
const isArchived = (article: Article) => article.skip_reason?.startsWith('operator_archived:');
const canEditNews = (article: Article) => !isArchived(article) && ['pending','skipped'].includes(article.processing_state);

function NewsLibrary({ query, setQuery, source, setSource, selected, toggle, open, create, addNews, publish, version, retry }: {
  query: string; setQuery: (value: string) => void; source: string; setSource: (value: string) => void;
  selected: Map<number, Article>; toggle: (row: Article) => void;
  open: (row: Article) => void; create: () => void; addNews: () => void; publish: () => void; version: number; retry: () => void;
}) {
  const cleanQuery = query.trim();
  const [{ query: effectiveQuery, page }, setSearchPage] = useState({ query: cleanQuery, page: 1 });
  const [state, setState] = useState('all');
  useEffect(() => {
    const timer = setTimeout(() => {
      setSearchPage(current => current.query === cleanQuery ? current : { query: cleanQuery, page: 1 });
    }, 260);
    return () => clearTimeout(timer);
  }, [cleanQuery]);
  const setPage = (page: number) => setSearchPage({ query: effectiveQuery, page });
  const search = new URLSearchParams({ q: effectiveQuery, state, source, page: String(page) });
  const { value, busy: loading, problem } = useData<NewsPage>('news?' + search, version);
  const busy = loading || effectiveQuery !== cleanQuery;
  const sources = useData<Pick<Source, 'name'>[]>('sources?include_counts=false', version);
  const totalPages = Math.max(1, Math.ceil((value?.total || 0) / (value?.size || 12)));
  return <>
    <PageTitle title="新闻库" detail="筛选新闻、查看原文，把选中的稿件整理成简报。"
      actions={<div className="editorial-actions"><button className="button secondary" onClick={addNews}><Plus size={16} />录入新闻</button>
        <button className="button secondary" onClick={create}>建草稿</button>
        <button className="button primary" onClick={publish}><Send size={16} />到发布台{selected.size > 0 && ' · ' + selected.size}</button></div>} />
    {problem && <Failure message={problem} retry={retry} />}
    <section className="card listing">
      <div className="filters">
        <label className="filter-search"><Search size={17} /><input aria-label="筛选新闻" placeholder="搜索标题或摘要" maxLength={150} value={query} onChange={e => setQuery(e.target.value)} /></label>
        <label className="select-control"><ListFilter size={16} /><select aria-label="新闻状态" value={state} onChange={e => { setState(e.target.value); setPage(1); }}><option value="all">全部状态</option><option value="fresh_pending">新鲜待处理</option><option value="expired">已过期</option>{['pending','batched','sent','skipped'].map(s => <option key={s} value={s}>{statusFor(s)[0]}</option>)}<option value="archived">回收站</option></select><ChevronDown size={14} /></label>
        <label className="select-control"><Rss size={15} /><select aria-label="新闻来源" value={source} onChange={e => { setSource(e.target.value); setPage(1); }}><option value="">全部来源</option>
          {source && !sources.value?.some(item => item.name === source) && <option>{source}</option>}
          {sources.value?.map(s => <option key={s.name}>{s.name}</option>)}</select><ChevronDown size={14} /></label>
        <span className="filter-total">{busy ? '读取中…' : '共 ' + digits(value?.total) + ' 条'}</span>
      </div>
      {value?.items.length ? <NewsRows items={value.items} open={open} selected={selected} toggle={toggle} />
        : <Empty title={busy ? '正在读取新闻' : '没有符合条件的新闻'} text={busy ? undefined : '可以更换关键词或清除筛选条件。'}
          action={(query || state !== 'all' || source) ? <button className="button secondary" onClick={() => { setQuery(''); setState('all'); setSource(''); setPage(1); }}>清除筛选</button> : undefined} />}
      <footer className="pages"><span>{selected.size ? '已选 ' + selected.size + ' 条' : '每页 12 条'}</span><div><button className="icon-button" aria-label="上一页" disabled={page <= 1 || busy} onClick={() => setPage(page - 1)}><ChevronLeft size={17} /></button><span>{page} / {totalPages}</span><button className="icon-button" aria-label="下一页" disabled={page >= totalPages || busy} onClick={() => setPage(page + 1)}><ChevronRight size={17} /></button></div></footer>
    </section>
  </>;
}
function RecordTable({ children, headers }: { children: React.ReactNode; headers: string[] }) {
  return <section className="card table-wrap"><table><thead><tr>{headers.map(head => <th key={head}>{head}</th>)}</tr></thead><tbody>{children}</tbody></table></section>;
}

function sourceReliability(row: Source) {
  const attempts = (row.successes ?? 0) + (row.failures ?? 0);
  return attempts ? (row.successes ?? 0) / attempts : null;
}

function SourcesTable({rows, edit, browse}: {rows: Source[]; edit: (row: Source) => void; browse: (source: string) => void}) {
  const [search, setSearch] = useState('');
  const [status, setStatus] = useState('all');
  const [sort, setSort] = useState('default');
  const term = search.trim().toLowerCase();
  const filtered = rows.filter(row => {
    const matches = !term || row.name.toLowerCase().includes(term) || (row.url && new URL(row.url).hostname.includes(term));
    const state = row.error ? 'error' : row.checked ? 'success' : 'unchecked';
    return matches && (status === 'all' || status === state);
  });
  if (sort === 'volume') filtered.sort((a, b) => b.recent_count - a.recent_count);
  if (sort === 'reliability') filtered.sort((a, b) => (sourceReliability(a) ?? 2) - (sourceReliability(b) ?? 2));
  return <>
    <div className="card filters">
      <label className="filter-search"><Search size={17} /><input aria-label="搜索订阅源" placeholder="搜索来源名或域名" value={search} maxLength={150} onChange={event => setSearch(event.target.value)} /></label>
      <label className="select-control"><ListFilter size={16} /><select aria-label="订阅源状态" value={status} onChange={event => setStatus(event.target.value)}>
        <option value="all">全部状态</option><option value="success">最近成功</option><option value="error">最近失败</option><option value="unchecked">暂无记录</option>
      </select><ChevronDown size={14} /></label>
      <label className="select-control"><select aria-label="订阅源排序" value={sort} onChange={event => setSort(event.target.value)}>
        <option value="default">订阅顺序</option><option value="volume">近 7 天入库最多</option><option value="reliability">成功率从低到高</option>
      </select><ChevronDown size={14} /></label>
      <span className="filter-total">{digits(filtered.length)} / {digits(rows.length)} 路</span>
    </div>
    {filtered.length ? <RecordTable headers={['来源','最近采集','累计成功率','近 7 天入库','最近入库','操作']}>
      {filtered.map(row => {
        const reliability = sourceReliability(row);
        return <tr key={row.name}>
          <td><div className="source-name"><span className="stat-icon amber"><Rss size={16} /></span>
            <div><button className="record-link" onClick={() => browse(row.name)} title={'查看 ' + row.name + ' 的新闻'}>{row.name}</button>
              <small>{row.url ? new URL(row.url).hostname : '暂无链接'}</small></div></div></td>
          <td><span className={'badge ' + (row.error ? 'red' : row.checked ? 'green' : 'neutral')} title={row.error || undefined}><i />
            {row.error ? issueNames[row.error] || '采集异常' : !row.checked ? '暂无抓取记录' : row.returned === 0 ? '源可用 · 无合格条目' : '最近抓取成功'}</span>
            {row.checked && <div className="operation-note">{fmt(row.checked, true)} · 合格 {digits(row.returned ?? 0)}</div>}</td>
          <td>{reliability === null ? '—' : <>{new Intl.NumberFormat('zh-CN',{style:'percent',maximumFractionDigits:1}).format(reliability)}
            <div className="operation-note">{digits(row.successes ?? 0)} / {digits((row.successes ?? 0) + (row.failures ?? 0))} 次</div></>}</td>
          <td>{digits(row.recent_count)} 条<div className="operation-note">其中已推送 {digits(row.recent_sent)} · 总入库 {digits(row.count)}</div></td>
          <td>{fmt(row.latest, true)}</td>
          <td><div className="editorial-actions"><button className="button secondary" onClick={() => edit(row)}>编辑</button>
            {external(row.url) && <a className="icon-button" aria-label={'打开 ' + row.name + ' 的 RSS'} href={external(row.url)} target="_blank" rel="noreferrer"><ArrowUpRight size={17} /></a>}</div></td>
        </tr>;
      })}
    </RecordTable> : <section className="card"><Empty title="没有匹配的订阅源" /></section>}
  </>;
}

function DataPage({ page, version, draftState, retry, inspect, go, create, browseSource }: {
  page: Exclude<Page,'home'|'news'|'publish'|'automation'|'settings'>; version: number; draftState: ReturnType<typeof useData<Draft[]>>;
  retry: () => void; inspect: (key: string | number) => void; go: (page: Page) => void; create: () => void;
  browseSource: (source: string) => void;
}) {
  const state = useData<Source[] | Batch[] | Report[]>(page === 'drafts' ? null : page, version);
  const [sourceEdit,setSourceEdit] = useState<Source | 'new' | null>(null);
  const [draftTrash,setDraftTrash] = useState(false);
  const [operatorError,setOperatorError] = useState('');
  const trashed = useData<Draft[]>(page === 'drafts' && draftTrash ? 'drafts?archived=true' : null, version);
  const { value, problem, busy } = page === 'drafts' ? draftTrash ? trashed : draftState : state;
  async function restoreDraft(row: Draft) {
    try { await api('drafts/' + row.id + '/restore',write('POST',{revision:row.revision})); retry(); setOperatorError(''); }
    catch (reason) { setOperatorError((reason as Error).message); }
  }
  const data = value || [];
  const details = {
    sources: ['来源管理', '筛选订阅、查看采集和推送表现，点击来源名查看该来源的新闻。'],
    batches: ['推送批次', '跟踪每一批新闻的分析、发送与核验状态。'],
    reports: ['周期报告', '查看机器人生成的周报、月报、季报和年报。'],
    drafts: ['简报草稿', '把选中的新闻整理成可编辑、可复制的简报。'],
  }[page];
  return <>
    <PageTitle title={details[0]} detail={details[1]} actions={page === 'drafts' ? <div className="editorial-actions">
      <button className="button secondary" onClick={() => setDraftTrash(value => !value)}>{draftTrash ? '返回草稿' : '回收站'}</button>
      <button className="button primary" onClick={create}><Plus size={17} />新建简报</button>
    </div> : page === 'sources' ? <button className="button primary" onClick={() => setSourceEdit('new')}><Plus size={17} />添加来源</button> : undefined} />
    {sourceEdit && <SourceEditor key={sourceEdit === 'new' ? 'new' : sourceEdit.name}
      item={sourceEdit === 'new' ? undefined : sourceEdit} request={api}
      completed={() => { setSourceEdit(null); retry(); }} cancel={() => setSourceEdit(null)} />}
    {operatorError && <Failure message={operatorError} retry={retry} />}
    {problem && <Failure message={problem} retry={retry} />}
    {page === 'batches' && <PreviewPanel />}
    {page === 'reports' && <ReportRetries request={api} version={version} refresh={retry} inspect={inspect} />}
    {!data.length ? <section className="card"><Empty icon={navigation.find(n => n.id === page)?.icon}
      title={busy ? '正在读取数据' : page === 'drafts' && draftTrash ? '回收站为空' : '暂无' + details[0].replace('管理','')}
      text={busy || (page === 'drafts' && draftTrash) ? undefined : {sources:'添加 RSS 订阅后会显示在这里。',batches:'机器人完成一轮简报处理后，批次会显示在这里。',reports:'有报告生成后，可以在这里查看周期、正文与资料覆盖。',drafts:'从新闻库勾选新闻，或从空白草稿开始。'}[page]}
      action={!busy && page === 'drafts' && !draftTrash ? <button className="button secondary" onClick={() => go('news')}>前往新闻库 <ArrowRight size={16} /></button> : undefined} /></section>
    : page === 'sources' ? <SourcesTable rows={data as Source[]} edit={setSourceEdit} browse={browseSource} />
    : page === 'batches' ? <RecordTable headers={['批次','状态','关联新闻','尝试次数','最后更新','']}>{(data as Batch[]).map(row => <tr key={row.id}>
      <td><button className="record-link" onClick={() => inspect(row.id)}>{row.id.slice(0,8).toUpperCase()}</button></td>
      <td><Badge status={row.status} />{(row.error_code === 'analysis_failed_3' || (row.status === 'send_failed' && row.failures >= 3 && row.error_code !== 'manual_reconciliation')) && <div className="operation-note">达到重试上限，待核对</div>}</td>
      <td>{digits(row.news_count)}</td><td>{digits(row.attempts)}</td><td>{fmt(row.updated_at, true)}</td>
      <td><button className="icon-button" onClick={() => inspect(row.id)} aria-label="查看批次"><ChevronRight size={17} /></button></td>
    </tr>)}</RecordTable>
    : page === 'reports' ? <RecordTable headers={['标题','类型','报告周期','资料数','']}>{(data as Report[]).map(row => <tr key={row.id}>
      <td><button className="record-link" onClick={() => inspect(row.id)}>{row.title}</button></td><td>{periodNames[row.report_type] || row.report_type}</td>
      <td>{row.period_start} 至 {row.period_end}</td><td>{digits(row.news_count)}</td>
      <td><button className="icon-button" onClick={() => inspect(row.id)} aria-label="查看报告"><ChevronRight size={17} /></button></td>
    </tr>)}</RecordTable>
    : <RecordTable headers={['标题','关联新闻','最后编辑','']}>{(data as Draft[]).map(row => <tr key={row.id}>
      <td><button className="record-link" onClick={() => inspect(row.id)}>{row.title}</button></td><td>{row.article_ids.length} 条</td><td>{fmt(row.updated_at,true)}</td>
      <td>{draftTrash ? <button className="button secondary" onClick={() => restoreDraft(row)}>恢复</button>
        : <button className="icon-button" onClick={() => inspect(row.id)} aria-label="编辑草稿"><ChevronRight size={17} /></button>}</td>
    </tr>)}</RecordTable>}
  </>;
}
function PreviewContent({ preview }: { preview: Preview }) {
  return <><p className="operation-note">原文链接 {digits(preview.visible_links ?? 0)} 个{preview.truncated && ' · 已按投递长度截断'}
      {preview.recorded === false && ' · 历史分析重建预览'}</p>
    <div className="markdown-preview"><Suspense fallback={<p>正在加载预览…</p>}>
      <Markdown skipHtml components={{ img: () => null, a: ({href, children}) =>
        external(href) ? <a href={external(href)} target="_blank" rel="noreferrer">{children}</a> : <span>{children}</span>
      }}>{preview.markdown || ''}</Markdown></Suspense></div></>;
}

function PreviewPanel() {
  const [version, setVersion] = useState(0);
  const [starting, setStarting] = useState(false);
  const [error, setError] = useState('');
  const preview = useData<Preview>('preview', version);
  useEffect(() => {
    if (preview.value?.status !== 'running') return;
    const timer = setTimeout(() => setVersion(current => current + 1), 1400);
    return () => clearTimeout(timer);
  }, [preview.value]);

  async function prepare() {
    setStarting(true);
    setError('');
    try {
      await api('preview', write('POST', {}));
      setVersion(current => current + 1);
    } catch (reason) {
      setError((reason as Error).message);
    } finally {
      setStarting(false);
    }
  }

  return <section className="card operations-section">
    <div className="operations-heading"><h2>推送预览</h2><button className="button secondary" onClick={prepare}
      disabled={starting || preview.value?.status === 'running'}>生成预览</button></div>
    <p className="operation-note">使用新闻库里符合当前规则的待处理新闻，按正式钉钉格式生成预览。</p>
    {(error || preview.problem) && <p className="operation-alert error" role="alert">{error || preview.problem}</p>}
    {preview.value?.status === 'running' && <p role="status">模型正在整理新闻…</p>}
    {preview.value?.message && <p role="status">{preview.value.message}</p>}
    {preview.value?.markdown && <PreviewContent preview={preview.value} />}
  </section>;
}

function Drawer({ title, close, children, wide }: { title: string; close: () => void; children: React.ReactNode; wide?: boolean }) {
  const ref = useRef<HTMLDialogElement>(null);
  useEffect(() => { ref.current?.showModal(); }, []);
  return <dialog ref={ref} className={'drawer ' + (wide ? 'wide' : '')} aria-label={title} onCancel={event => { event.preventDefault(); close(); }}
    onClick={event => { if (event.target === event.currentTarget) close(); }}>
    <div className="drawer-inner"><header><h2>{title}</h2><button className="icon-button" onClick={close} aria-label="关闭详情"><X size={20} /></button></header>{children}</div>
  </dialog>;
}
function DraftEditor({ initial, saved, notify, archive }: { initial: Draft; saved: (draft: Draft) => void; notify: (message: string) => void; archive: () => void }) {
  const [title, setTitle] = useState(initial.title);
  const [body, setBody] = useState(initial.body);
  const [view, setView] = useState<'edit'|'preview'>('edit');
  const [busy, setBusy] = useState(false);
  const markdown = '# ' + title.trim() + '\n\n' + body;
  async function save() {
    if (!title.trim()) return notify('请输入简报标题。');
    setBusy(true);
    try {
      saved(await api<Draft>('drafts/' + initial.id, write('PUT', { title, body, revision: initial.revision })));
      notify('草稿已保存');
    } catch (error) { notify((error as Error).message); }
    finally { setBusy(false); }
  }
  async function copy() {
    try { await navigator.clipboard.writeText(markdown); notify('已复制 Markdown'); }
    catch { notify('剪贴板不可用，可以下载 Markdown 文件。'); }
  }
  function download() {
    const url = URL.createObjectURL(new Blob([markdown], { type: 'text/markdown;charset=utf-8' }));
    const link = document.createElement('a');
    link.href = url;
    link.download = (title.trim().replace(/[<>:"/\\|?*]/g, '_') || '新闻简报') + '.md';
    link.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }
  return <div className="drawer-body editor">
    <label>简报标题<input value={title} maxLength={160} onChange={e => setTitle(e.target.value)} /></label>
    <div className="editor-switch"><button className={view==='edit'?'active':''} onClick={() => setView('edit')}>编辑正文</button><button className={view==='preview'?'active':''} onClick={() => setView('preview')}>Markdown 预览</button><span>{initial.article_ids.length} 条关联新闻</span></div>
    {view === 'edit' ? <textarea aria-label="简报正文" value={body} maxLength={25000} placeholder="在这里整理简报正文…" onChange={e => setBody(e.target.value)} /> :
      <div className="markdown-preview"><Suspense fallback={<p>正在加载预览…</p>}><Markdown skipHtml components={{
        img: () => null,
        a: ({href, children}) => external(href) ? <a href={external(href)} target="_blank" rel="noreferrer">{children}</a> : <span>{children}</span>,
      }}>{markdown}</Markdown></Suspense></div>}
    <div className="drawer-actions">{!initial.archived_at && <button className="button primary" disabled={busy} onClick={save}><Check size={16} />{busy ? '保存中…' : '保存草稿'}</button>}<button className="button secondary" onClick={copy}><Copy size={16} />复制</button><button className="button secondary" onClick={download}><ArrowDownToLine size={16} />下载</button>
      {!initial.archived_at && <button className="button secondary" disabled={busy} onClick={archive}>移至回收站</button>}</div>
  </div>;
}

function App() {
  const readPage = (): Page => navigation.some(item => item.id === location.hash.slice(1)) ? location.hash.slice(1) as Page : 'home';
  const [page, setPage] = useState(readPage);
  const [query, setQuery] = useState('');
  const [newsSource, setNewsSource] = useState('');
  const [topSearch, setTopSearch] = useState('');
  const [version, setVersion] = useState(0);
  const [selected, setSelected] = useState(() => {
    try {
      const saved = JSON.parse(localStorage.getItem('news-editorial-selection') || '[]') as Article[];
      return new Map(saved.slice(0, 20).map(row => [row.id, row]));
    } catch { return new Map<number, Article>(); }
  });
  const [panel, setPanel] = useState<null | { type:'article'; value: Article } | { type:'news-edit'; value?: Article } | { type:'draft'; value: Draft } | { type:'batch'; value: BatchDetail } | { type:'report'; value: ReportDetail }>(null);
  const [message, setMessage] = useState('');
  const [working, setWorking] = useState(false);
  const [mobileNav, setMobileNav] = useState(false);
  const searchInput = useRef<HTMLInputElement>(null);
  const summary = useData<Summary>(page === 'home' ? 'summary' : null, version);
  const health = useData<{ok: boolean}>(page === 'home' ? null : 'health', version);
  const connection = page === 'home' ? summary : health;
  const draftState = useData<Draft[]>('drafts', version);
  const draftList = draftState.value;
  const refresh = () => setVersion(v => v + 1);
  const openArticle = (value: Article) => setPanel({ type: 'article', value });
  const current = navigation.find(item => item.id === page)!;
  const CurrentIcon = current.icon;
  useEffect(() => {
    const change = () => setPage(readPage());
    const command = (e: KeyboardEvent) => {
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'k') { e.preventDefault(); searchInput.current?.focus(); }
    };
    addEventListener('hashchange', change);
    addEventListener('keydown', command);
    return () => { removeEventListener('hashchange', change); removeEventListener('keydown', command); };
  }, []);
  useEffect(() => {
    if (!message) return;
    const timer = setTimeout(() => setMessage(''), 4200);
    return () => clearTimeout(timer);
  }, [message]);
  function go(next: Page) {
    setPage(next);
    location.hash = next;
    setMobileNav(false);
  }
  function browseSource(source: string) {
    setQuery('');
    setTopSearch('');
    setNewsSource(source);
    go('news');
  }
  useEffect(() => {
    try { localStorage.setItem('news-editorial-selection', JSON.stringify([...selected.values()])); } catch { /* optional browser storage */ }
  }, [selected]);
  function toggle(row: Article) {
    if (!selected.has(row.id) && !canEditNews(row)) {
      setMessage('这篇新闻已组批或投递，可在推送批次中查看。');
      return;
    }
    if (!selected.has(row.id) && selected.size >= 20) {
      setMessage('一份简报最多选择 20 条新闻。');
      return;
    }
    setSelected(current => {
      const next = new Map(current);
      if (next.has(row.id)) next.delete(row.id);
      else if (next.size < 20) next.set(row.id, row);
      return next;
    });
  }
  async function archiveArticle(row: Article, restore = false) {
    if (!restore && !window.confirm('将这条新闻移至回收站？')) return;
    try {
      await api('manage/news/' + row.id + (restore ? '/restore' : ''),write(restore ? 'POST' : 'DELETE',{revision:row.revision}));
      setSelected(current => { const next = new Map(current); next.delete(row.id); return next; });
      setPanel(null); setMessage(restore ? '新闻已恢复' : '新闻已移至回收站'); refresh();
    } catch (error) { setMessage((error as Error).message); }
  }
  async function archiveDraft(row: Draft) {
    if (!window.confirm('将这份简报移至回收站？')) return;
    try {
      await api('drafts/' + row.id,write('DELETE',{revision:row.revision}));
      setPanel(null); setMessage('简报已移至回收站'); refresh();
    } catch (error) { setMessage((error as Error).message); }
  }
  async function create() {
    if (working) return;
    setWorking(true);
    try {
      const value = await api<Draft>('drafts', write('POST', { article_ids: Array.from(selected.keys()) }));
      setPanel({type:'draft',value});
      setSelected(new Map());
      refresh();
      go('drafts');
    } catch (error) { setMessage((error as Error).message); }
    finally { setWorking(false); }
  }
  async function inspect(id: string | number) {
    try {
      if (page === 'drafts') {
        const value = await api<Draft>('drafts/' + id);
        setPanel({type:'draft',value});
      } else if (page === 'batches') setPanel({type:'batch',value:await api<BatchDetail>('batches/' + id)});
      else if (page === 'reports') setPanel({type:'report',value:await api<ReportDetail>('reports/' + id)});
    } catch (error) { setMessage((error as Error).message); }
  }
  async function retryManual(batch: BatchDetail) {
    if (working || !window.confirm('确认按当前批次保存的内容再次发送到原钉钉接收方？')) return;
    setWorking(true);
    try {
      const result = await api<{status: string}>('editorial/send', write('POST', {id: batch.id, confirmed: true}));
      setMessage(result.status === 'sent' ? '钉钉已确认收到。' : result.status === 'unknown' ? '送达状态未知，请先核对。' : '本次发送被拒绝。');
      await inspect(batch.id);
      refresh();
    } catch (error) { setMessage((error as Error).message); }
    finally { setWorking(false); }
  }
  return <>
    <header className="chrome">
      <div className="chrome-tabs">
        <div className="brand"><span className="brand-icon"><Layers3 size={19} /></span><strong>新闻工作台</strong></div>
        <button className="open-tab" onClick={() => go('home')} title="返回总览"><CurrentIcon size={16} /><span>{current.label}</span></button>
        <button className="tab-plus icon-button" title="新建简报" aria-label="新建简报" onClick={create}><Plus size={18} /></button>
        <span className="chrome-mode"><span />本地工作区</span>
      </div>
      <div className="toolbar">
        <button className="mobile-menu icon-button" aria-label="切换菜单" onClick={() => setMobileNav(!mobileNav)}><Menu size={20} /></button>
        <button className="icon-button toolbar-home" aria-label="返回总览" title="返回总览" onClick={() => go('home')}><ArrowLeft size={19} /></button>
        <button className={'icon-button ' + (connection.busy ? 'spin' : '')} aria-label="刷新数据" title="刷新数据" onClick={refresh}><RefreshCw size={17} /></button>
        <form className="omnibox" onSubmit={event => { event.preventDefault(); setQuery(topSearch.trim()); setNewsSource(''); go('news'); }}>
          <Search size={17} /><input ref={searchInput} aria-label="全局搜索新闻" maxLength={150} placeholder="搜索新闻标题或摘要" value={topSearch} onChange={e => setTopSearch(e.target.value)} /><kbd>Ctrl K</kbd>
        </form>
        <span className={'connection ' + (connection.problem ? 'offline' : connection.value ? 'connected' : '')}><i />{connection.problem ? '连接异常' : connection.value ? '数据库已连接' : '连接中'}</span>
      </div>
    </header>
    <div className="shell">
      <aside className={'sidebar' + (mobileNav ? ' expanded' : '')}>
        <div className="side-label">工作区</div>
        <nav aria-label="工作区导航">{navigation.map(({id,label,icon:Icon}) => <button key={id} className={id===page?'active':''} aria-current={id===page?'page':undefined} onClick={() => go(id)}>
          <Icon size={18} /><span>{label}</span>{id === 'drafts' && !!draftList?.length && <small>{draftList.length}</small>}
          {id === 'publish' && selected.size > 0 && <small>{selected.size}</small>}
        </button>)}</nav>
        <div className="sidebar-foot"><div className="side-monitor"><Database size={16} /><div><strong>新闻机器人</strong><span>本地数据</span></div></div><span className="local-dot" /></div>
      </aside>
      <main className="workspace">
        <div className="content">
          <div className="breadcrumb"><span>工作区</span><ChevronRight size={13} /><span>{current.label}</span></div>
          {(page === 'home' || page === 'automation') && <CollectControl request={api} completed={refresh} />}
          {page==='home' ? <>{summary.problem && <Failure message={summary.problem} retry={refresh} />}<Overview summary={summary.value} busy={summary.busy} open={openArticle} go={go} create={create} /></>
            : page==='news' ? <NewsLibrary query={query} setQuery={setQuery} source={newsSource} setSource={setNewsSource} selected={selected} toggle={toggle} open={openArticle} create={create} addNews={() => setPanel({type:'news-edit'})} publish={() => go('publish')} version={version} retry={refresh} />
              : page==='publish' ? <PublishingDesk request={api} items={[...selected.values()]}
                  setItems={items => setSelected(new Map(items.flatMap(item => {
                    const row = selected.get(item.id);
                    return row ? [[row.id, row] as const] : [];
                  })))} browse={() => go('news')} onSent={refresh}
                  render={(markdown, visible_links) => <PreviewContent preview={{markdown, visible_links}} />} />
              : page==='automation' ? <AutomationPanel request={api} />
              : page==='settings' ? <SettingsPanel request={api} />
              : <DataPage key={page} page={page} version={version} draftState={draftState} retry={refresh} inspect={inspect} go={go} create={create} browseSource={browseSource} />}
        </div>
        <footer className="workspace-footer"><span>新闻工作台</span><span>来源数据按北京时间展示</span></footer>
      </main>
    </div>
    {!!message && !panel && <div className="toast" role="status">{message}<button className="icon-button" aria-label="关闭提示" onClick={() => setMessage('')}><X size={16} /></button></div>}
    {panel && <Drawer title={{ article:'新闻详情', 'news-edit': panel.type === 'news-edit' && panel.value ? '编辑新闻' : '录入新闻', draft:'简报草稿', batch:'推送批次详情', report:'周期报告详情' }[panel.type]} close={() => setPanel(null)} wide={panel.type==='draft'}>
      {!!message && <div className="drawer-message" role="status">{message}</div>}
      {panel.type==='article' && <div className="drawer-body">
        <div className="item-meta"><span>{panel.value.source}</span><span>{fmt(panel.value.published_at || panel.value.created_at,true)}</span>
          {isArchived(panel.value) ? <span className="badge neutral">回收站</span> : <Badge status={panel.value.processing_state} />}</div>
        <h3>{panel.value.title}</h3>
        <ReadingSummary key={panel.value.id} id={panel.value.id} request={api} />
        <h4>订阅源摘录</h4><p className="article-summary">{panel.value.summary || '来源未提供摘录。'}</p>
        {panel.value.skip_reason && !isArchived(panel.value) && <p className="result-note">筛除原因　{reasonNames[panel.value.skip_reason] || panel.value.skip_reason}</p>}
        <div className="drawer-actions">{external(panel.value.url) && <a className="button primary" href={external(panel.value.url)} target="_blank" rel="noreferrer">查看原文 <ArrowUpRight size={16} /></a>}
          <button className="button secondary" disabled={!selected.has(panel.value.id) && !canEditNews(panel.value)}
            onClick={() => toggle(panel.value)}>{selected.has(panel.value.id) ? '移出选稿' : '加入选稿'}</button>
          {selected.size > 0 && <button className="button primary" onClick={() => { setPanel(null); go('publish'); }}>到发布台 · {selected.size}</button>}
          {canEditNews(panel.value) && <><button className="button secondary" onClick={() => setPanel({type:'news-edit',value:panel.value})}>编辑新闻</button>
            <button className="button secondary" onClick={() => archiveArticle(panel.value)}>移至回收站</button></>}
          {isArchived(panel.value) && <button className="button secondary" onClick={() => archiveArticle(panel.value,true)}>恢复新闻</button>}</div>
        {!isArchived(panel.value) && <RelatedStories key={panel.value.id} id={panel.value.id} request={api}
          open={openArticle} formatTime={value => fmt(value, true)} />}
      </div>}
      {panel.type==='news-edit' && <NewsEditor item={panel.value} request={api} completed={() => {
        if (panel.value) setSelected(current => { const next = new Map(current); next.delete(panel.value!.id); return next; });
        setPanel(null); refresh(); setMessage('新闻已保存');
      }} />}
      {panel.type==='draft' && <DraftEditor key={panel.value.id} initial={panel.value} saved={value => { setPanel({type:'draft',value}); refresh(); }}
        notify={setMessage} archive={() => archiveDraft(panel.value)} />}
      {panel.type==='batch' && <div className="drawer-body"><div className="item-meta"><Badge status={panel.value.status} /><span>{panel.value.news_count} 条分析输入</span></div>
        <h3>{panel.value.analysis.headline || '批次 ' + panel.value.id.slice(0,8)}</h3>
        <p className="result-note">批次编号　{panel.value.id}</p>
        {panel.value.error_code && <p className="result-note">状态说明　{issueNames[panel.value.error_code] || panel.value.error_code}</p>}
        {!!panel.value.analysis._omitted_observations && <p className="result-note">
          因缺少有效来源，省略了 {panel.value.analysis._omitted_observations} 条附加观察。</p>}
        {panel.value.preview && <><h4>钉钉消息内容</h4><PreviewContent preview={panel.value.preview} /></>}
        {panel.value.analysis._publication_mode === 'manual' && ['manual_ready','manual_send_failed'].includes(panel.value.status) &&
          <button className="button secondary" disabled={working} onClick={() => retryManual(panel.value)}>核对后发送此批次</button>}
        <h4>发送尝试</h4>{panel.value.attempts.length ? <ul className="detail-list">{panel.value.attempts.map((attempt,i) => <li key={i}><Badge status={attempt.status} /> <span>{fmt(attempt.started_at,true)}</span>{attempt.error_code && <p>{issueNames[attempt.error_code] || attempt.error_code}</p>}</li>)}</ul> : <p className="muted">这个批次暂无发送尝试。</p>}
      </div>}
      {panel.type==='report' && <div className="drawer-body"><h3>{panel.value.title}</h3>
        {panel.value.content._coverage && <div className="result-note">留存资料 {panel.value.content._coverage.available_news} 条 · 分析样本 {panel.value.content._coverage.sampled_news} 条
          {panel.value.content._coverage.note && <p>{panel.value.content._coverage.note}</p>}
        </div>}
        <p className="article-summary">{panel.value.content.summary || '没有保存正文摘要。'}</p>
        <ul className="detail-list">{panel.value.content.top_events?.map((item,i) => {
          const links = [...new Set(item.urls?.length ? item.urls : [item.url])].flatMap(url => external(url) || []);
          return <li key={i}><strong>{item.event}</strong><p>{item.significance}</p>{item.evolution && <p>{item.evolution}</p>}
            <div className="editorial-actions">{links.map((url, index) => <a key={url} href={url} target="_blank" rel="noreferrer">
              {links.length === 1 ? '引用原文' : '原文 ' + (index + 1)} <ArrowUpRight size={13} /></a>)}</div></li>;
        })}</ul>
        {!!Object.keys(panel.value.content.domain_analysis || {}).length && <><h4>领域分析</h4><ul className="detail-list">{Object.entries(panel.value.content.domain_analysis || {}).map(([name, content]) => <li key={name}><strong>{name}</strong><p>{content}</p></li>)}</ul></>}
        {!!panel.value.content.trends?.length && <><h4>趋势观察</h4><ul className="detail-list">{panel.value.content.trends.map((trend,i) => <li key={i}>{trend}</li>)}</ul></>}
        {panel.value.content.data_summary && <><h4>数据摘要</h4><p className="article-summary">{panel.value.content.data_summary}</p></>}
        {panel.value.content.outlook && <><h4>后续关注</h4><p>{panel.value.content.outlook}</p></>}
      </div>}
    </Drawer>}
  </>;
}

createRoot(document.getElementById('root')!).render(<React.StrictMode><App /></React.StrictMode>);
