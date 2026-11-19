import { useEffect, useState } from 'react';
import type { ReactNode } from 'react';
import { ArrowDown, ArrowLeft, ArrowUp, Check, ClipboardList, RefreshCw, Send, Sparkles, X } from 'lucide-react';
import { StageDetails } from './run-diagnostics';
import type { ModelUsage, Stage } from './run-diagnostics';

type Request = <T>(path: string, options?: RequestInit) => Promise<T>;
type Entry = { id: number; title: string; source: string };
type Summary = { status: string; summary: string | null; evidence?: {field: 'title' | 'excerpt'; quote: string}[] };
type Preview = { id: string; count: number; markdown: string; visible_links: number };
type Job = { status: string; message?: string; fetched?: number; inserted?: number; summaries?: number; errors?: number;
  stages?: Stage[]; model_usage?: ModelUsage[] };

const post = (data: object = {}): RequestInit => ({
  method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(data),
});

function useStatus<T extends {status: string}>(request: Request, path: string, revision: number) {
  const [value, setValue] = useState<T>();
  const [problem, setProblem] = useState('');
  useEffect(() => {
    const controller = new AbortController();
    let timer: number;
    const read = async () => {
      try {
        const next = await request<T>(path, { signal: controller.signal });
        if (controller.signal.aborted) return;
        setValue(next);
        setProblem('');
        if (next.status === 'running') timer = window.setTimeout(read, 1300);
      } catch (error) {
        if (!controller.signal.aborted) setProblem((error as Error).message);
      }
    };
    void read();
    return () => { controller.abort(); window.clearTimeout(timer); };
  }, [request, path, revision]);
  return { value, problem };
}

export function ReadingSummary({ id, request }: { id: number; request: Request }) {
  const [revision, refresh] = useState(0);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState('');
  const {value, problem} = useStatus<Summary>(request, 'editorial/summary/' + id, revision);
  const canAddEvidence = value?.status === 'ready' && !value.evidence?.length;
  async function generate() {
    setSubmitting(true);
    setError('');
    try {
      await request('editorial/summary/' + id, post());
      refresh(value => value + 1);
    } catch (reason) { setError((reason as Error).message); }
    finally { setSubmitting(false); }
  }
  return <section className="reading-summary">
    <div className="operations-heading"><h4><Sparkles size={17} /> AI 摘要</h4>
      {(value?.status !== 'ready' || canAddEvidence) && <button className="button secondary" onClick={generate}
        disabled={submitting || value?.status === 'running'}>{canAddEvidence ? '补生成证据' : value?.status === 'failed' ? '重试' : '生成摘要'}</button>}
    </div>
    <p>{value?.summary || (value?.status === 'running' ? '模型正在整理这篇新闻…'
      : value?.status === 'failed' ? '上次摘要生成失败，可重新请求。' : value ? '等待后台整理，也可现在生成。' : '正在读取摘要…')}</p>
    <small>依据标题和订阅源摘录整理。</small>
    {!!value?.evidence?.length && <details className="summary-evidence">
      <summary>核对摘录证据（{value.evidence.length} 段）</summary>
      {value.evidence.map(item => <blockquote key={item.field + ':' + item.quote}>
        <small>{item.field === 'title' ? '来源标题' : '订阅源摘录'}</small>
        <p>{item.quote}</p>
      </blockquote>)}
    </details>}
    {(error || problem) && <p className="operation-alert error" role="alert">{error || problem}</p>}
  </section>;
}

type RelatedArticle = { id: number; title: string; source: string; published_at: string | null; created_at: string };
type Match<T> = { article: T; reason: string };
const relatedReasons: Record<string, string> = {
  same_link: '同一个原文地址',
  similar_title: '标题高度相近',
  model_action: '产品型号与事件动作相同',
  roundup: '新闻汇总提到同一产品事件',
  launch_review: '同一款产品发布后的评测或评论',
};

export function RelatedStories<T extends RelatedArticle>({ id, request, open, formatTime }: {
  id: number; request: Request; open: (article: T) => void; formatTime: (value: string) => string;
}) {
  const [attempt, find] = useState(0);
  const [matches, setMatches] = useState<Match<T>[]>();
  const [problem, setProblem] = useState('');
  const loading = attempt > 0 && !matches && !problem;
  useEffect(() => {
    if (!attempt) return;
    const control = new AbortController();
    request<{ items: Match<T>[] }>('news/' + id + '/related', { signal: control.signal })
      .then(response => { if (!control.signal.aborted) setMatches(response.items); })
      .catch(reason => { if (!control.signal.aborted) setProblem((reason as Error).message); });
    return () => control.abort();
  }, [attempt, id, request]);
  return <section className="related-stories">
    <div className="operations-heading"><h4>同事件候选</h4>
      {!matches && <button className="button secondary" disabled={loading}
        onClick={() => { setProblem(''); find(value => value + 1); }}>{problem ? '重试' : loading ? '查找中…' : '查找候选'}</button>}
    </div>
    {problem && <p className="operation-alert error" role="alert">{problem}</p>}
    {matches && (matches.length ? <ul className="detail-list">
      {matches.map(({article, reason}) => <li key={article.id}>
        <button className="record-link" onClick={() => open(article)}>{article.title}</button>
        <p>{relatedReasons[reason] || '相近报道'} · {article.source} · {formatTime(article.published_at || article.created_at)}</p>
      </li>)}
    </ul> : <p className="operation-note" role="status">前后 48 小时内未找到相近报道。</p>)}
  </section>;
}

export function CollectControl({request, completed}: {request: Request; completed: () => void}) {
  const [revision, refresh] = useState(0);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const {value, problem} = useStatus<Job>(request, 'editorial/collect', revision);
  useEffect(() => {
    if (value && (value.status === 'succeeded' || value.status === 'partial')) completed();
  }, [value]); // One refresh for each fetched result, independent of the parent callback identity.
  async function collect() {
    setBusy(true);
    setError('');
    try {
      await request('editorial/collect', post());
      refresh(value => value + 1);
    } catch (reason) { setError((reason as Error).message); }
    finally { setBusy(false); }
  }
  return <><section className="card editorial-collect">
    <div><strong>从订阅源采集</strong><p>{value?.status === 'running' ? '正在采集…'
      : value?.status === 'idle' || !value ? '现在检查并收录各订阅源的更新。'
      : typeof value.inserted === 'number' && value.status !== 'busy'
        ? '取回 ' + (value.fetched ?? 0) + ' 条 · 新增 ' + value.inserted + ' 条 · AI 摘要 ' + (value.summaries ?? 0) + ' 条 · 异常 ' + (value.errors ?? 0)
        : value.message}</p></div>
    <button className="button secondary" onClick={collect} disabled={busy || value?.status === 'running'}><RefreshCw size={16} />立即采集</button>
    {(error || problem) && <p className="operation-alert error" role="alert">{error || problem}</p>}
  </section><StageDetails stages={value?.stages} usage={value?.model_usage} /></>;
}

export function PublishingDesk({items, setItems, request, browse, onSent, render}: {
  items: Entry[]; setItems: (items: Entry[]) => void; request: Request; browse: () => void;
  onSent: () => void; render: (markdown: string, links: number) => ReactNode;
}) {
  const [headline, setHeadline] = useState('');
  const [preview, setPreview] = useState<Preview>();
  const [confirmed, setConfirmed] = useState(false);
  const [busy, setBusy] = useState(false);
  const [feedback, setFeedback] = useState('');
  const [uncertain, setUncertain] = useState(false);
  const reset = () => { setPreview(undefined); setConfirmed(false); setFeedback(''); setUncertain(false); };
  const update = (next: Entry[]) => { setItems(next); reset(); };
  function move(from: number, to: number) {
    const next = [...items];
    [next[from], next[to]] = [next[to], next[from]];
    update(next);
  }
  async function prepare() {
    reset();
    setBusy(true);
    try { setPreview(await request<Preview>('editorial/preview', post({article_ids: items.map(item => item.id), headline}))); }
    catch (error) { setFeedback((error as Error).message); }
    finally { setBusy(false); }
  }
  async function send() {
    if (!preview || !confirmed) return;
    setBusy(true);
    setFeedback('');
    try {
      const result = await request<{status: string}>('editorial/send', post({id: preview.id, confirmed: true}));
      if (result.status === 'sent') {
        setItems([]);
        setPreview(undefined);
        setConfirmed(false);
        setFeedback('钉钉已确认收到这份简报。');
        onSent();
      } else if (result.status === 'unknown') {
        setUncertain(true);
        setFeedback('送达状态未知，请先在推送批次核对，避免重复发送。');
      } else {
        setConfirmed(false);
        setFeedback('钉钉拒绝了本次发送。检查配置后，可以确认重试同一批次。');
      }
    } catch (error) {
      setConfirmed(false);
      setFeedback((error as Error).message);
    } finally { setBusy(false); }
  }
  return <div className="operations editorial-desk">
    <div className="page-title"><div><h1>发布台</h1><p>挑选和排序新闻，核对钉钉消息后发布。</p></div>
      <button className="button secondary" onClick={browse}><ArrowLeft size={16} />继续选稿</button></div>
    {feedback && <p className="operation-alert" role="status">{feedback}</p>}
    <div className="desk-columns">
      <section className="card operations-section">
        <div className="operations-heading"><h2>选稿清单</h2><span className="badge blue">{items.length} / 20</span></div>
        {items.length ? <ol className="desk-list">
          {items.map((item, index) => <li key={item.id}>
            <div><strong>{item.title}</strong><p>{item.source}</p></div>
            <div className="desk-item-actions">
              <button className="icon-button" disabled={busy || index === 0} onClick={() => move(index, index-1)} aria-label="上移"><ArrowUp size={16} /></button>
              <button className="icon-button" disabled={busy || index === items.length-1} onClick={() => move(index, index+1)} aria-label="下移"><ArrowDown size={16} /></button>
              <button className="icon-button" disabled={busy} onClick={() => update(items.filter(row => row.id !== item.id))} aria-label="移除"><X size={16} /></button>
            </div>
          </li>)}
        </ol> : <p className="operation-note">在新闻库勾选要发布的新闻，或从单篇详情加入选稿。</p>}
        <label>简报导语<input value={headline} onChange={event => { setHeadline(event.target.value); reset(); }} maxLength={160}
          disabled={busy} placeholder="选填，放在消息开头" /></label>
        <div className="operation-actions"><button className="button primary" onClick={prepare} disabled={!items.length || busy}>
          <ClipboardList size={16} />生成钉钉预览</button></div>
      </section>
      <section className="card operations-section"><h2>预览与发送</h2>
        {preview ? <>
          <p className="operation-note">{preview.count} 篇新闻 · {preview.visible_links} 个原文链接 · 接收方为当前配置的钉钉机器人</p>
          {render(preview.markdown, preview.visible_links)}
          <label className="desk-confirm"><input type="checkbox" checked={confirmed} disabled={busy || uncertain}
            onChange={event => setConfirmed(event.target.checked)} />我已核对上述内容，确认发送到钉钉</label>
          <div className="operation-actions"><button className="button primary" onClick={send} disabled={!confirmed || busy || uncertain}><Send size={16} />确认发送</button></div>
        </> : <p className="operation-note"><Check size={14} /> 完成选稿后生成预览，就能核对正式消息的内容和顺序。</p>}
      </section>
    </div>
  </div>;
}
