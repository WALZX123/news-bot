import { useEffect, useState } from 'react';
import type { FormEvent } from 'react';
import { Check, Play, Trash2 } from 'lucide-react';

type Request = <T>(path: string, options?: RequestInit) => Promise<T>;
type Source = {name: string; url: string | null};
type News = {id: number; title: string; source: string; summary: string; url: string | null;
  published_at: string | null; revision: string};

const json = (method: string, value: object): RequestInit => ({
  method, headers: {'Content-Type':'application/json'}, body: JSON.stringify(value),
});

function localInput(value?: string | null) {
  const time = value ? Date.parse(value) : NaN;
  return Number.isFinite(time) ? new Date(time + 8 * 3600000).toISOString().slice(0,16) : '';
}

export function NewsEditor({item, request, completed}: {item?: News; request: Request; completed: () => void}) {
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  async function save(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const values = Object.fromEntries(new FormData(event.currentTarget));
    setBusy(true);
    setError('');
    try {
      const body = {...values, published_at: values.published_at ? values.published_at + ':00+08:00' : null};
      if (item) await request('manage/news/' + item.id, json('PUT', {...body, revision: item.revision}));
      else await request('manage/news', json('POST', body));
      completed();
    } catch (reason) { setError((reason as Error).message); }
    finally { setBusy(false); }
  }
  return <form className="drawer-body operations-section management-form" onSubmit={save}>
    <label>新闻标题<input name="title" required maxLength={512} defaultValue={item?.title} /></label>
    <label>原文链接<input name="url" type="url" required maxLength={2048} defaultValue={item?.url ?? ''} /></label>
    <label>来源名称<input name="source" required maxLength={128} defaultValue={item?.source} /></label>
    <label>发布时间（北京时间）<input name="published_at" type="datetime-local" defaultValue={localInput(item?.published_at)} /></label>
    <label>新闻摘录<textarea name="summary" rows={5} maxLength={10000} defaultValue={item?.summary} /></label>
    {error && <p className="operation-alert error" role="alert">{error}</p>}
    <button className="button primary" type="submit" disabled={busy}><Check size={16} />保存新闻</button>
  </form>;
}

export function SourceEditor({item, request, completed, cancel}: {
  item?: Source; request: Request; completed: () => void; cancel: () => void;
}) {
  const [name, setName] = useState(item?.name || '');
  const [url, setUrl] = useState(item?.url || '');
  const [version, setVersion] = useState('');
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    const controller = new AbortController();
    request<{revision: string; sources: Source[]}>('manage/sources', {signal: controller.signal})
      .then(value => {
        if (item && !value.sources.some(source => source.name === item.name && source.url === item.url)) {
          setError('这个来源已经更新。请刷新列表后重新打开。');
        } else setVersion(value.revision);
      }).catch(reason => {
        if (!controller.signal.aborted) setError(reason.message);
      });
    return () => controller.abort();
  }, [item, request]);
  async function execute(action: 'save' | 'remove' | 'test') {
    if (action === 'remove' && (!item || !window.confirm('从订阅中移除「' + item.name + '」？'))) return;
    setBusy(true); setError(''); setNotice('');
    try {
      if (action === 'test') {
        const result = await request<{count: number; titles: string[]}>('manage/sources/test', json('POST', {name, url}));
        setNotice('试抓成功 · ' + result.count + ' 条符合当前采集条件' + (result.titles.length ? '。' + result.titles.join('；') : '。'));
      } else if (action === 'remove' && item) {
        await request('manage/sources', json('DELETE', {name:item.name, revision:version}));
        completed();
      } else {
        await request('manage/sources', json(item ? 'PUT' : 'POST', {name, url, revision:version, original:item?.name}));
        completed();
      }
    } catch (reason) { setError((reason as Error).message); }
    finally { setBusy(false); }
  }
  return <form className="card operations-section management-form" onSubmit={event => { event.preventDefault(); void execute('save'); }}>
    <h2>{item ? '编辑来源' : '添加订阅源'}</h2>
    <label>来源名称<input value={name} required maxLength={128} onChange={event => setName(event.target.value)} /></label>
    <label>RSS / Atom 地址<input type="url" value={url} required maxLength={2048} onChange={event => setUrl(event.target.value)} /></label>
    {(error || notice) && <p className={'operation-alert' + (error ? ' error' : '')} role="status">{error || notice}</p>}
    <div className="operation-actions">
      <button className="button primary" type="submit" disabled={busy || !version}><Check size={16} />保存来源</button>
      <button className="button secondary" type="button" disabled={busy || !url.trim()} onClick={() => execute('test')}><Play size={16} />试抓</button>
      {item && <button className="button secondary" type="button" disabled={busy || !version} onClick={() => execute('remove')}><Trash2 size={16} />移除订阅</button>}
      <button className="button secondary" type="button" disabled={busy} onClick={cancel}>取消</button>
    </div>
  </form>;
}
