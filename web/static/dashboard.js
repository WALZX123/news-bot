/* All remote text stays data. Deferred scripts run in order before this event. */
document.addEventListener('DOMContentLoaded', function () {
  'use strict';
  const settings = JSON.parse(document.getElementById('dashboard-data').textContent);
  const statusEl = document.getElementById('job-status');
  const retry = document.getElementById('job-retry');
  const buttons = Array.from(document.querySelectorAll('[data-job-action]'));
  let activeJob = null;
  let polling = false;

  function show(message, tone) {
    statusEl.textContent = message;
    statusEl.className = 'mt-2 small text-center text-' + tone;
  }

  function enableButtons() {
    buttons.forEach(function (button) { button.disabled = false; });
  }

  async function jsonRequest(url, options) {
    const controller = new AbortController();
    const timeout = setTimeout(function () { controller.abort(); }, 15000);
    try {
      const response = await fetch(url, Object.assign({
        signal: controller.signal, credentials: 'same-origin', redirect: 'error',
        headers: { 'Accept': 'application/json' }
      }, options));
      if (response.status === 401) throw new Error('登录已失效，请刷新页面重新登录。');
      if (!(response.headers.get('content-type') || '').includes('application/json')) {
        throw new Error('服务返回格式异常。请刷新页面检查登录或服务状态。');
      }
      const data = await response.json();
      if (!response.ok) throw new Error(data.message || '请求失败，请稍后重试。');
      return data;
    } finally { clearTimeout(timeout); }
  }

  function pausePolling(message) {
    polling = false;
    show(message + ' 后台任务可能仍在执行；请先重新查询，确认状态后再决定下一步。', 'danger');
    retry.hidden = false;
    enableButtons();
  }

  async function pollJob(jobId, deadline) {
    try {
      const data = await jsonRequest('/actions/status?job_id=' + encodeURIComponent(jobId));
      if (data.status === 'queued' || data.status === 'running') {
        show(data.message || '任务正在执行。', 'primary');
        if (Date.now() >= deadline) {
          pausePolling('等待超过十五分钟，已暂停自动查询。');
        } else {
          setTimeout(function () { pollJob(jobId, deadline); }, 2000);
        }
        return;
      }
      polling = false;
      // Only confirmed success uses green. Partial/unknown states need attention.
      const tone = data.status === 'succeeded' ? 'success'
        : ['partial', 'waiting', 'busy', 'skipped', 'recovery_required', 'unknown'].includes(data.status) ? 'warning' : 'danger';
      show(data.message || '任务状态：' + data.status, tone);
      retry.hidden = true;
      activeJob = null;
      enableButtons();
    } catch (error) {
      pausePolling(error.message || '查询任务失败。');
    }
  }

  buttons.forEach(function (button) {
    button.addEventListener('click', async function () {
      if (polling) return;
      if (activeJob) {
        show('上一个任务的结果尚未确认，请先重新查询任务状态。', 'warning');
        return;
      }
      buttons.forEach(function (item) { item.disabled = true; });
      show('正在提交任务…', 'primary');
      try {
        const data = await jsonRequest('/actions/' + button.dataset.jobAction, {
          method: 'POST', headers: { 'Accept': 'application/json', 'X-CSRF-Token': settings.csrf_token }
        });
        if (!data.job_id) throw new Error('服务未返回任务编号，请检查任务列表和日志。');
        activeJob = data.job_id;
        retry.hidden = true;
        polling = true;
        pollJob(activeJob, Date.now() + 15 * 60 * 1000);
      } catch (error) {
        show(error.message || '任务请求失败；提交结果可能未知，请核对服务日志后再操作。', 'danger');
        enableButtons();
      }
    });
  });
  retry.addEventListener('click', function () {
    if (!activeJob || polling) return;
    polling = true;
    retry.hidden = true;
    buttons.forEach(function (button) { button.disabled = true; });
    pollJob(activeJob, Date.now() + 15 * 60 * 1000);
  });

  if (typeof Chart === 'undefined') return;
  const isDark = document.documentElement.getAttribute('data-theme') === 'dark';
  const color = isDark ? '#94a3b8' : '#64748b';
  new Chart(document.getElementById('dailyChart'), {
    type: 'bar',
    data: { labels: settings.daily_labels, datasets: [{ data: settings.daily_values,
      backgroundColor: isDark ? 'rgba(129,140,248,0.6)' : 'rgba(79,110,247,0.55)', borderRadius: 5 }] },
    options: { responsive: true, maintainAspectRatio: false, plugins: { legend: { display: false } },
      scales: { x: { grid: { display: false }, ticks: { color: color } },
        y: { beginAtZero: true, ticks: { color: color, stepSize: 1 } } } }
  });
  const rows = settings.source_total_rows;
  new Chart(document.getElementById('sourcePieChart'), {
    type: 'doughnut',
    data: { labels: rows.map(function (row) { return row.source; }), datasets: [{
      data: rows.map(function (row) { return row.cnt; }),
      backgroundColor: ['#4f6ef7','#10b981','#f59e0b','#ef4444','#8b5cf6','#06b6d4','#f97316','#ec4899','#84cc16','#6366f1'],
      borderColor: 'transparent' }] },
    options: { responsive: true, maintainAspectRatio: false,
      plugins: { legend: { position: 'bottom', labels: { color: color, usePointStyle: true } } } }
  });
});
