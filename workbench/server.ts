import { createServer } from 'node:http';
import { createReadStream } from 'node:fs';
import { rename, stat, writeFile } from 'node:fs/promises';
import { Readable } from 'node:stream';
import { pipeline } from 'node:stream/promises';
import { extname, resolve, sep } from 'node:path';
import { fileURLToPath } from 'node:url';

const preferredPort = Number(process.env.NEWS_WORKBENCH_PORT ?? 4380);
const dataPort = Number(process.env.NEWS_WORKBENCH_DATA_PORT || 4381);
const secret = process.env.NEWS_WORKBENCH_KEY;
const instance = process.env.NEWS_WORKBENCH_INSTANCE;
const readyFile = process.env.NEWS_WORKBENCH_READY_FILE;
const dist = fileURLToPath(new URL('./dist/', import.meta.url)).replace(/[\\/]+$/, '');
const validPort = (number: number) => Number.isInteger(number) && number > 1023 && number <= 65535;
if (!secret || !instance || !readyFile || !(preferredPort === 0 || validPort(preferredPort)) || !validPort(dataPort)) {
  throw new Error('Run the workbench through start.ps1 to initialize the local services.');
}
const contentTypes: Record<string, string> = {
  '.html': 'text/html; charset=utf-8', '.js': 'text/javascript; charset=utf-8',
  '.css': 'text/css; charset=utf-8', '.svg': 'image/svg+xml', '.png': 'image/png',
};
const routes = [
  { url: /^\/(?:health|summary|news|sources|batches|reports|drafts|settings|automation|preview)$/, methods: ['GET'] },
  { url: /^\/news\/[0-9]+\/related$/, methods: ['GET'] },
  { url: /^\/report-jobs$/, methods: ['GET'] },
  { url: /^\/report-jobs\/retry$/, methods: ['POST'] },
  { url: /^\/(?:batches\/[a-f0-9-]{1,36}|(?:reports|drafts)\/[0-9]+)$/, methods: ['GET'] },
  { url: /^\/drafts$/, methods: ['POST'] },
  { url: /^\/drafts\/[0-9]+$/, methods: ['PUT'] },
  { url: /^\/drafts\/[0-9]+$/, methods: ['DELETE'] },
  { url: /^\/drafts\/[0-9]+\/restore$/, methods: ['POST'] },
  { url: /^\/manage\/sources$/, methods: ['GET','POST','PUT','DELETE'] },
  { url: /^\/manage\/(?:sources\/test|news|news\/[0-9]+\/restore)$/, methods: ['POST'] },
  { url: /^\/manage\/news\/[0-9]+$/, methods: ['PUT','DELETE'] },
  { url: /^\/settings(?:\/(?:ai|automation|credentials))?$/, methods: ['PUT'] },
  { url: /^\/settings\/ai\/test$/, methods: ['POST'] },
  { url: /^\/preview$/, methods: ['POST'] },
  { url: /^\/editorial\/(?:summary\/[0-9]+|collect)$/, methods: ['GET','POST'] },
  { url: /^\/editorial\/(?:preview|send|automation)$/, methods: ['POST'] },
];
const server = createServer(async (req, res) => {
  const json = (status: number, detail: string) => {
    res.writeHead(status, { 'Content-Type': 'application/json; charset=utf-8', 'Cache-Control': 'no-store' });
    res.end(JSON.stringify({ detail }));
  };
  res.setHeader('X-Content-Type-Options', 'nosniff');
  res.setHeader('Referrer-Policy', 'no-referrer');
  res.setHeader('Cross-Origin-Resource-Policy', 'same-origin');
  res.setHeader('Content-Security-Policy', "default-src 'self'; style-src 'self' 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'");
  const address = server.address();
  const port = typeof address === 'object' ? address?.port : undefined;
  if (![`127.0.0.1:${port}`, `localhost:${port}`].includes(req.headers.host || '')) {
    return json(403, '请通过本机地址访问。');
  }
  try {
    const rawUrl = req.url || '/';
    if (!rawUrl.startsWith('/') || rawUrl.startsWith('//')) return json(400, '地址无效。');
    const url = new URL(rawUrl, 'http://127.0.0.1');
    if (req.method === 'GET' && url.pathname === '/_workbench') {
      res.writeHead(200, { 'Content-Type': 'application/json', 'Cache-Control': 'no-store' });
      res.end(JSON.stringify({ instance }));
      return;
    }
    if (url.pathname.startsWith('/api/')) {
      const path = url.pathname.slice(4);
      const method = req.method || 'GET';
      if (!routes.some(route => route.url.test(path) && route.methods.includes(method))) return json(404, '接口不存在。');
      let body: string | undefined;
      if (method !== 'GET') {
        if (req.headers.origin !== 'http://' + req.headers.host ||
            !req.headers['content-type']?.toLowerCase().startsWith('application/json')) {
          return json(403, '请从工作台页面提交操作。');
        }
        const chunks: Buffer[] = [];
        const maxBytes = path === '/settings' ? 2097152 : 131072;
        let size = 0;
        for await (const chunk of req) {
          size += chunk.length;
          if (size > maxBytes) {
            req.resume();
            return json(413, '请求内容超出大小限制。');
          }
          chunks.push(chunk);
        }
        body = Buffer.concat(chunks).toString('utf8');
      }
      const client = new AbortController();
      const cancel = () => client.abort();
      res.once('close', cancel);
      try {
        const upstream = await fetch('http://127.0.0.1:' + dataPort + path + url.search, {
          method, body, headers: { 'x-workbench-key': secret, 'content-type': 'application/json' },
          signal: AbortSignal.any([AbortSignal.timeout(path === '/editorial/send' ? 32000 : 20000), client.signal]),
        });
        res.writeHead(upstream.status, { 'Content-Type': 'application/json; charset=utf-8', 'Cache-Control': 'no-store' });
        if (upstream.body) {
          const stream = Readable.fromWeb(upstream.body as Parameters<typeof Readable.fromWeb>[0]);
          await pipeline(stream, res);
        }
        else res.end();
      } finally {
        res.off('close', cancel);
      }
      return;
    }
    if (req.method !== 'GET' && req.method !== 'HEAD') return json(405, '此地址只提供页面资源。');
    const path = url.pathname === '/' ? 'index.html' : decodeURIComponent(url.pathname).slice(1);
    const file = resolve(dist, path);
    if (!file.startsWith(dist + sep)) return json(404, '页面不存在。');
    const kind = contentTypes[extname(file)];
    const info = await stat(file).catch(() => null);
    if (!kind || !info?.isFile()) return json(404, '页面资源不存在。');
    const cache = /^assets\/.+-[\w-]{8,}\.[a-z]+$/i.test(path)
      ? 'public, max-age=31536000, immutable' : 'no-cache';
    res.writeHead(200, { 'Content-Type': kind, 'Cache-Control': cache, 'Content-Length': info.size });
    if (req.method === 'HEAD') res.end();
    else await pipeline(createReadStream(file), res);
  } catch {
    if (!res.destroyed) {
      if (!res.headersSent) json(503, '数据服务暂不可用，请检查工作台是否已完整启动。');
      else res.end();
    }
  }
});

function listen(port: number): Promise<void> {
  return new Promise((resolve, reject) => {
    const failed = (error: Error) => {
      server.off('listening', ready);
      reject(error);
    };
    const ready = () => {
      server.off('error', failed);
      resolve();
    };
    server.once('error', failed);
    server.once('listening', ready);
    server.listen(port, '127.0.0.1');
  });
}

try {
  await listen(preferredPort);
} catch (error) {
  const code = (error as NodeJS.ErrnoException).code;
  if (!preferredPort || (code !== 'EADDRINUSE' && code !== 'EACCES')) throw error;
  await listen(0);
}
const bound = server.address();
if (!bound || typeof bound === 'string') throw new Error('Gateway socket has no TCP address.');
await writeFile(readyFile + '.tmp', JSON.stringify({ instance, port: bound.port }));
await rename(readyFile + '.tmp', readyFile);
console.log('新闻工作台 http://127.0.0.1:' + bound.port);

process.stdin.resume();
process.stdin.once('end', () => {
  server.close();
  server.closeAllConnections();
});
