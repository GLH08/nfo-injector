/**
 * NFO MediaInfo 注入管理器 — 前端应用逻辑
 * 三栏布局：目录树 | 文件详情 | 任务&日志
 */

// ─── 全局状态 ───────────────────────────────────────────
const API = '';  // 同源，路径直接用 /api/...
let currentFile = null;      // 当前选中的 STRM 文件相对路径
let currentDirCtx = null;    // 右键菜单目标目录
let currentWs = null;        // 当前 WebSocket 连接
let config = {};             // 应用配置
let taskPollingTimer = null;

// ─── DOM 引用 ──────────────────────────────────────────
const $ = id => document.getElementById(id);

// ─── Toast 通知 ────────────────────────────────────────────
let toastContainer = null;
function showToast(msg, type = 'info', duration = 3500) {
  if (!toastContainer) {
    toastContainer = document.createElement('div');
    toastContainer.className = 'toast-container';
    document.body.appendChild(toastContainer);
  }
  const t = document.createElement('div');
  t.className = `toast ${type}`;
  t.textContent = msg;
  toastContainer.appendChild(t);
  if (duration > 0) {
    setTimeout(() => _hideToast(t), duration);
  }
  return t;
}

function updateToast(el, msg, type) {
  if (!el) return;
  el.textContent = msg;
  if (type) el.className = `toast ${type}`;
}

function _hideToast(t) {
  t.style.opacity = '0';
  t.style.transform = 'translateX(20px)';
  t.style.transition = '0.3s';
  setTimeout(() => t.remove(), 300);
}

// ─── API 封装 ───────────────────────────────────────────
async function api(method, path, body) {
  const opts = {
    method,
    headers: { 'Content-Type': 'application/json' },
  };
  if (body !== undefined) opts.body = JSON.stringify(body);
  const res = await fetch(API + path, opts);
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: res.statusText }));
    throw new Error(err.detail || `HTTP ${res.status}`);
  }
  return res.json();
}

const GET = path => api('GET', path);
const POST = (path, body) => api('POST', path, body);
const PUT = (path, body) => api('PUT', path, body);
const DEL = path => api('DELETE', path);

// ─── 任务管理 ──────────────────────────────────────────────────
let serverTimeOffset = 0;
async function fetchTasks() {
  try {
    const res = await GET('/api/tasks');

    // 计算时钟偏移，以修复前端计时器误差问题
    if (res.server_time) {
      const serverMs = new Date(res.server_time).getTime();
      serverTimeOffset = Date.now() - serverMs;
    }

    renderTasks(res.tasks || []);
  } catch (e) {
    console.error('获取任务失败', e);
  }
}

// ─── 初始化 ───────────────────────────────────────────────
async function init() {
  try {
    config = await GET('/api/config');
    await loadTreeRoot();
    // 全局统计耗时（全库递归扫描），不阻塞页面交互，后台异步填充顶栏
    refreshGlobalStats().catch(e => console.error('全局统计失败', e));
    startTaskPolling();
  } catch (e) {
    showToast('连接后端失败: ' + e.message, 'error');
  }

  // 事件绑定
  $('btnConfig').addEventListener('click', openConfigModal);
  $('btnRefreshRoot').addEventListener('click', async () => {
    await refreshGlobalStats(true);
    showToast('统计已刷新', 'info', 2000);
  });
  $('btnCollapseAll').addEventListener('click', collapseAllTree);
  $('btnScanAll').addEventListener('click', () => refreshGlobalStats(true));
  $('treeSearch').addEventListener('input', filterTree);

  $('btnProbeOnly').addEventListener('click', probeOnly);
  $('btnInjectFile').onclick = () => injectCurrentFile(false);
  $('btnForceInject').onclick = confirmForceInject;
  $('btnMockInjectFile').onclick = () => {
    showConfirm(
      '确认虚拟注入',
      `将向该文件强制写入“通用/虚拟”的 MediaInfo（1080p, H264, AAC），跳过所有探测！\n这能解决死机超时问题，但媒体信息并非真实数据。`,
      () => injectCurrentFileMock()
    );
  };
  $('btnRefreshDetail').onclick = () => { selectFile(currentFile); };
  $('btnCopyXml').addEventListener('click', () => copyText($('xmlViewer').textContent));
  $('btnCopyProbe').addEventListener('click', () => copyText($('probeViewer').textContent));

  $('btnClearHistory').addEventListener('click', async () => {
    await DEL('/api/tasks/history');
    renderTaskPanel();
  });
  $('btnClearLog').addEventListener('click', () => { $('logContainer').innerHTML = ''; });
  $('toggleHistory').addEventListener('click', toggleHistorySection);

  // 右键菜单
  document.addEventListener('click', () => hideContextMenu());
  document.addEventListener('contextmenu', e => { if (!e.target.closest('.context-menu')) hideContextMenu(); });
  $('ctxScanNfo').addEventListener('click', () => scanNfoStatus());
  $('ctxFindIssues').addEventListener('click', () => findIssues());
  $('ctxScan').addEventListener('click', () => scanContextDir());
  $('ctxRefreshMediaIndex').addEventListener('click', refreshMediaIndex);
  $('ctxInjectEmpty').addEventListener('click', () => injectDir('EMPTY'));
  $('ctxInjectNeedFix').addEventListener('click', () => injectDir('PARTIAL'));
  $('ctxInjectAll').addEventListener('click', () => confirmInjectAll());
  $('ctxInjectMockAll').addEventListener('click', () => confirmInjectMockAll());

  // 问题文件弹窗
  $('issuesClose').addEventListener('click', () => { $('issuesModal').style.display = 'none'; });

  // 配置模态框
  $('btnConfig').addEventListener('click', openConfigModal);
  $('configCancel').addEventListener('click', () => { $('configModal').style.display = 'none'; });
  $('configSave').addEventListener('click', saveConfig);
  $('btnAddLibrary').addEventListener('click', addLibraryRule);
  document.querySelectorAll('.config-tab').forEach(btn => {
    btn.addEventListener('click', () => switchConfigTab(btn.dataset.tab));
  });

  // 确认弹窗
  $('confirmCancel').addEventListener('click', () => { $('confirmModal').style.display = 'none'; });
}

// ─── 目录树 ────────────────────────────────────────────────
let treeData = {};   // 缓存已加载的目录内容 path → entries
let scanCache = {};  // 目录状态统计缓存 path → counts

// ─── scan 请求调度器：去重 + 并发限制，避免展开深层目录时几百个 /api/scan 并发打爆后端 ──
const MAX_SCAN_CONCURRENCY = 2;
const _scanInflight = new Map();   // path → Promise（去重：相同 path 复用同一 Promise）
const _scanQueue = [];             // 待执行的 { path, resolver } 队列
let _scanActive = 0;               // 当前真正在飞的请求数

function _drainScanQueue() {
  while (_scanQueue.length && _scanActive < MAX_SCAN_CONCURRENCY) {
    const { path, resolver } = _scanQueue.shift();
    _scanActive++;
    _runScan(path).then(c => {
      resolver(c);
    }).finally(() => {
      _scanActive--;
      _scanInflight.delete(path);
      _drainScanQueue();
    });
  }
}

async function _runScan(path) {
  try {
    const counts = await GET(`/api/scan?path=${encodeURIComponent(path)}`);
    scanCache[path] = counts;
    return counts;
  } catch (e) { return null; }
}

/** 调度一次 scan（去重 + 限流）。返回 Promise<counts|null>。 */
function scheduleScan(path) {
  if (scanCache[path]) return Promise.resolve(scanCache[path]);
  const existing = _scanInflight.get(path);
  if (existing) return existing;
  let resolver;
  const promise = new Promise(r => { resolver = r; });
  _scanInflight.set(path, promise);
  _scanQueue.push({ path, resolver });
  _drainScanQueue();
  return promise;
}

async function loadTreeRoot() {
  const container = $('treeContainer');
  container.innerHTML = '<div class="loading-placeholder"><div class="spinner"></div><span>加载目录树…</span></div>';
  try {
    const data = await GET('/api/browse?path=');
    treeData[''] = data.entries;
    container.innerHTML = '';
    renderTreeLevel(container, data.entries, 0, '');
  } catch (e) {
    container.innerHTML = `<div class="loading-placeholder" style="color:#ef4444">加载失败: ${e.message}</div>`;
  }
}

function renderTreeLevel(container, entries, depth, parentPath) {
  entries.forEach(entry => {
    const node = document.createElement('div');
    node.className = `tree-node tree-node-${entry.entry_type}`;
    node.dataset.path = entry.relative_path;
    node.dataset.type = entry.entry_type;

    const item = document.createElement('div');
    item.className = 'tree-item';
    item.dataset.path = entry.relative_path;

    // 缩进
    for (let i = 0; i < depth; i++) {
      const indent = document.createElement('span');
      indent.className = 'tree-indent';
      item.appendChild(indent);
    }

    if (entry.entry_type === 'library' || entry.entry_type === 'directory') {
      // 展开/折叠按钮
      const toggle = document.createElement('span');
      toggle.className = `tree-toggle ${entry.has_children ? '' : 'empty'}`;
      toggle.textContent = '▶';
      item.appendChild(toggle);

      const icon = document.createElement('span');
      icon.className = 'tree-icon';
      icon.textContent = entry.entry_type === 'library' ? '📚' : '📁';
      item.appendChild(icon);

      const name = document.createElement('span');
      name.className = 'tree-name';
      name.textContent = entry.name;
      item.appendChild(name);

      // 目录徽章（视口可见时才加载，避免一次展开几百个目录并发 scan）
      const badge = document.createElement('span');
      badge.className = 'tree-dir-badge';
      badge.dataset.dirPath = entry.relative_path;
      item.appendChild(badge);

      // 子节点容器
      const children = document.createElement('div');
      children.className = 'tree-children collapsed';
      children.dataset.loaded = 'false';
      children.dataset.depth = String(depth + 1);

      // 仅非库目录、且尚无缓存时，等徽章进入视口再加载统计
      if (entry.entry_type !== 'library' && !scanCache[entry.relative_path]) {
        observeDirBadge(badge, entry.relative_path);
      } else if (scanCache[entry.relative_path]) {
        renderDirBadge(badge, scanCache[entry.relative_path]);
      }

      // 点击展开
      item.addEventListener('click', async (e) => {
        e.stopPropagation();
        if (!entry.has_children) return;
        const isOpen = !children.classList.contains('collapsed');
        if (isOpen) {
          children.classList.add('collapsed');
          toggle.classList.remove('open');
          icon.textContent = entry.entry_type === 'library' ? '📚' : '📁';
        } else {
          toggle.classList.add('open');
          icon.textContent = entry.entry_type === 'library' ? '📚' : '📂';
          children.classList.remove('collapsed');
          if (children.dataset.loaded === 'false') {
            children.dataset.loaded = 'true';
            await loadTreeChildren(children, entry.relative_path, depth + 1);
          }
        }
      });

      // 右键菜单 (only for directories, not libraries)
      if (entry.entry_type === 'directory') {
        item.addEventListener('contextmenu', (e) => {
          e.preventDefault();
          e.stopPropagation();
          showContextMenu(e.clientX, e.clientY, entry.relative_path);
        });
      }

      node.appendChild(item);
      node.appendChild(children);

    } else if (entry.entry_type === 'strm') {
      // STRM 文件
      const toggle = document.createElement('span');
      toggle.className = 'tree-toggle empty';
      item.appendChild(toggle);

      const icon = document.createElement('span');
      icon.className = 'tree-icon';
      icon.textContent = '🎬';
      item.appendChild(icon);

      const name = document.createElement('span');
      name.className = 'tree-name';
      name.textContent = entry.name.replace(/\.strm$/i, '');
      item.appendChild(name);

      // 媒体索引徽章：已索引绿，未索引灰
      const idx = document.createElement('span');
      idx.className = 'tree-index-badge' + (entry.indexed ? ' indexed' : '');
      idx.textContent = entry.indexed ? '🔗' : '⬚';
      idx.title = entry.indexed ? '已索引媒体文件名' : '未索引（右键目录刷新媒体文件名索引）';
      item.appendChild(idx);

      // 状态点
      const dot = document.createElement('span');
      dot.className = 'tree-status-dot';
      dot.style.background = entry.nfo_status_color || '#6b7280';
      dot.title = entry.nfo_status_label || entry.nfo_status || '未知';
      item.appendChild(dot);

      item.addEventListener('click', (e) => {
        e.stopPropagation();
        selectFile(entry.relative_path);
        // 高亮选中
        document.querySelectorAll('.tree-item.selected').forEach(el => el.classList.remove('selected'));
        item.classList.add('selected');
      });

      node.appendChild(item);
    }

    container.appendChild(node);
  });
}

async function loadTreeChildren(container, dirPath, depth) {
  container.innerHTML = '<div style="padding: 4px 0 4px 32px"><div class="spinner" style="width:14px;height:14px;border-width:2px"></div></div>';
  try {
    const data = await GET(`/api/browse?path=${encodeURIComponent(dirPath)}`);
    treeData[dirPath] = data.entries;
    container.innerHTML = '';
    renderTreeLevel(container, data.entries, depth, dirPath);
  } catch (e) {
    container.innerHTML = `<div style="padding:6px 14px;color:#ef4444;font-size:11px">加载失败: ${e.message}</div>`;
  }
}

/** 注入完成后重载指定目录的可见子节点（就地重绘，刷新圆点/索引徽章）。幂等。 */
async function reloadDirChildren(dirPath) {
  if (!dirPath) return;
  const node = document.querySelector(`.tree-node[data-path="${cssEscape(dirPath)}"]`);
  if (!node) return;
  const children = node.querySelector(':scope > .tree-children');
  if (!children) return;
  // 只重载已展开的目录；折叠的不打扰
  if (children.classList.contains('collapsed')) return;
  // depth = 该 children 内子节点的缩进层级，从 dataset 拿不到则用 0
  const depth = parseInt(children.dataset.depth || '0', 10);
  // 清缓存强制后端重读（_FILE_CACHE 已被注入流程翻新，browse 会拿到新状态）
  delete treeData[dirPath];
  await loadTreeChildren(children, dirPath, depth);
}

/** CSS 选择器转义：路径含括号/特殊字符时安全定位节点。 */
function cssEscape(s) {
  if (window.CSS && CSS.escape) return CSS.escape(s);
  return String(s).replace(/["\\]/g, '\\$&');
}

// 目录徽章 IntersectionObserver：进入视口才加载统计，离开不再重复
const _dirBadgeObserver = ('IntersectionObserver' in window) ? new IntersectionObserver((entries, obs) => {
  entries.forEach(ent => {
    if (ent.isIntersecting) {
      const el = ent.target;
      obs.unobserve(el);
      loadDirStats(el.dataset.dirPath, el);
    }
  });
}, { root: null, rootMargin: '200px' }) : null;

function observeDirBadge(badgeEl, dirPath) {
  if (_dirBadgeObserver) {
    _dirBadgeObserver.observe(badgeEl);
  } else {
    // 无 IntersectionObserver 支持时退化为直接加载（限流仍在）
    loadDirStats(dirPath, badgeEl);
  }
}

async function loadDirStats(dirPath, badgeEl) {
  if (scanCache[dirPath]) {
    renderDirBadge(badgeEl, scanCache[dirPath]);
    return;
  }
  const counts = await scheduleScan(dirPath);
  if (counts) renderDirBadge(badgeEl, counts);
}

function renderDirBadge(el, counts) {
  if (!counts || counts.total === 0) { el.textContent = ''; return; }
  // 索引覆盖度徽章：全已索引✓、部分未索引⚠、全未索引○
  let idxHtml = '';
  if (counts.indexed !== undefined && counts.unindexed !== undefined) {
    if (counts.unindexed === 0) {
      idxHtml = `<span class="b-idx all" title="媒体索引：全部已索引(${counts.indexed})">🔗</span>`;
    } else if (counts.indexed === 0) {
      idxHtml = `<span class="b-idx none" title="媒体索引：全部未索引(${counts.unindexed})">⛓</span>`;
    } else {
      idxHtml = `<span class="b-idx partial" title="媒体索引：已索引 ${counts.indexed} / 未索引 ${counts.unindexed}">🔗⚠</span>`;
    }
  }
  el.innerHTML = `
    ${idxHtml}
    ${counts.healthy  ? `<span class="b-h" title="健康">${counts.healthy}✓</span>` : ''}
    ${counts.partial  ? `<span class="b-p" title="不完整">${counts.partial}⚠</span>` : ''}
    ${counts.empty    ? `<span class="b-e" title="空白">${counts.empty}✕</span>` : ''}
    ${counts.missing  ? `<span class="b-m" title="缺失">${counts.missing}?</span>` : ''}
  `;
}

function collapseAllTree() {
  document.querySelectorAll('.tree-children').forEach(el => el.classList.add('collapsed'));
  document.querySelectorAll('.tree-toggle.open').forEach(el => { el.classList.remove('open'); });
  document.querySelectorAll('.tree-icon').forEach(el => {
    if (el.textContent === '📂') el.textContent = '📁';
    if (el.textContent === '📚') el.textContent = '📚';
  });
}

function filterTree() {
  const q = $('treeSearch').value.toLowerCase().trim();
  document.querySelectorAll('.tree-node').forEach(node => {
    const name = node.dataset.path?.split('/').pop()?.toLowerCase() || '';
    node.style.display = (!q || name.includes(q)) ? '' : 'none';
  });
}

async function refreshGlobalStats(force = false) {
  try {
    // 仅手动「刷新统计」时清服务端扫描缓存；进页面默认用已有缓存，避免每次全库重扫
    if (force) await DEL('/api/scan-cache');
    const counts = await GET('/api/scan?path=');
    $('statHealthy').textContent = `${counts.healthy} ✅`;
    $('statPartial').textContent = `${counts.partial} ⚠️`;
    $('statEmpty').textContent = `${counts.empty} 🔘`;
    $('statMissing').textContent = `${counts.missing} ⚫`;
    // 清除前端目录缓存
    scanCache = {};
  } catch (e) { /* 静默 */ }
}

// ─── 文件详情 ─────────────────────────────────────────────
async function selectFile(relPath) {
  if (!relPath) return;
  currentFile = relPath;

  $('detailEmpty').style.display = 'none';
  $('detailContent').style.display = 'flex';
  $('sectionProbeResult').style.display = 'none';

  // 显示加载状态
  $('detailFileName').textContent = relPath.split('/').pop().replace(/\.strm$/i, '');
  $('detailFilePath').textContent = relPath;
  $('detailStatusBadge').textContent = '加载中…';
  $('mediaInfoContent').innerHTML = '<div class="loading-placeholder" style="padding:16px"><div class="spinner"></div><span>加载中…</span></div>';
  $('xmlViewer').textContent = '';

  try {
    const data = await GET(`/api/nfo?path=${encodeURIComponent(relPath)}`);
    renderFileDetail(data);
  } catch (e) {
    $('detailStatusBadge').textContent = '加载失败: ' + e.message;
    $('mediaInfoContent').innerHTML = '';
  }
}

function renderFileDetail(data) {
  // 状态徽章
  const badge = $('detailStatusBadge');
  badge.textContent = data.status_label || data.status;
  badge.style.color = data.status_color;
  badge.style.borderColor = data.status_color;

  // 按钮状态
  const isHealthy = data.status === 'HEALTHY';
  const hasMissing = data.status === 'MISSING' || !data.nfo_path;
  $('btnInjectFile').disabled = hasMissing;
  $('btnForceInject').disabled = hasMissing;

  // 联动更新左侧树的圆点状态
  if (data.strm_path) {
    // 处理可能带单引号双引号等特殊字符的选择器
    const sel = `.tree-item[data-path="${data.strm_path.replace(/"/g, '\\"')}"] .tree-status-dot`;
    const dot = document.querySelector(sel);
    if (dot) {
      dot.style.background = data.status_color;
      dot.title = data.status_label || data.status;
    }
  }

  // MediaInfo
  const miContainer = $('mediaInfoContent');
  if (data.stream_details && (data.stream_details.video.length || data.stream_details.audio.length)) {
    miContainer.innerHTML = '';
    const grid = document.createElement('div');
    grid.className = 'mediainfo-grid';

    data.stream_details.video.forEach((v, i) => {
      grid.appendChild(buildStreamCard('video', `视频流 #${i+1}`, [
        ['编解码器', v.codec, true],
        ['分辨率', v.width && v.height ? `${v.width}×${v.height}` : null, true],
        ['帧率', formatFramerate(v.framerate)],
        ['比特率', v.bitrate ? `${Math.round(v.bitrate/1000)} kbps` : null],
        ['宽高比', v.aspectratio],
        ['时长', v.duration_seconds ? formatDuration(v.duration_seconds) : null],
        ['语言', v.language],
        ['扫描方式', v.scantype],
      ]));
    });

    data.stream_details.audio.forEach((a, i) => {
      grid.appendChild(buildStreamCard('audio', `音频流 #${i+1}`, [
        ['编解码器', a.codec, true],
        ['声道', a.channels ? `${a.channels}ch` : null, true],
        ['采样率', a.samplingrate ? `${a.samplingrate} Hz` : null],
        ['比特率', a.bitrate ? `${Math.round(a.bitrate/1000)} kbps` : null],
        ['语言', a.language],
      ]));
    });

    data.stream_details.subtitle.forEach((s, i) => {
      grid.appendChild(buildStreamCard('subtitle', `字幕流 #${i+1}`, [
        ['格式', s.codec],
        ['语言', s.language],
      ]));
    });

    miContainer.appendChild(grid);

    if (data.missing_fields && data.missing_fields.length > 0) {
      const warn = document.createElement('div');
      warn.className = 'missing-fields-warning';
      warn.innerHTML = `⚠ 缺少字段: <strong>${data.missing_fields.join(', ')}</strong>`;
      miContainer.appendChild(warn);
    }
  } else {
    let msg = '', hint = '';
    if (data.status === 'MISSING') { msg = '⚫ NFO 文件缺失'; hint = '无法注入 — NFO 不存在'; }
    else if (data.status === 'EMPTY') { msg = '🔴 MediaInfo 为空'; hint = 'Emby 刷库后覆盖了 streamdetails'; }
    else { msg = '无 MediaInfo 信息'; }
    miContainer.innerHTML = `<div class="no-info-placeholder">${msg}${hint ? ' — <span style="color:var(--color-partial)">' + hint + '</span>' : ''}</div>`;
  }

  if (data.parse_error) {
    const warn = document.createElement('div');
    warn.className = 'missing-fields-warning';
    warn.style.marginTop = '8px';
    warn.textContent = '解析错误: ' + data.parse_error;
    miContainer.appendChild(warn);
  }

  // NFO XML
  $('xmlViewer').textContent = data.raw_xml ? formatXml(data.raw_xml) : '（NFO 文件不存在）';
}

function buildStreamCard(type, title, fields) {
  const card = document.createElement('div');
  card.className = 'mi-stream-card';

  const typeEl = document.createElement('div');
  typeEl.className = `mi-stream-type ${type}`;
  typeEl.textContent = title;
  card.appendChild(typeEl);

  fields.forEach(([key, val, highlight]) => {
    if (!val) return;
    const row = document.createElement('div');
    row.className = 'mi-row';
    row.innerHTML = `<span class="mi-key">${key}</span><span class="mi-val${highlight ? ' highlight' : ''}">${val}</span>`;
    card.appendChild(row);
  });

  return card;
}

function formatFramerate(fr) {
  if (!fr) return null;
  if (fr.includes('/')) {
    const [num, den] = fr.split('/').map(Number);
    if (den) return `${(num/den).toFixed(3)} fps`;
  }
  return fr + ' fps';
}

function formatDuration(sec) {
  if (!sec) return null;
  const h = Math.floor(sec / 3600);
  const m = Math.floor((sec % 3600) / 60);
  const s = sec % 60;
  return h > 0 ? `${h}:${String(m).padStart(2,'0')}:${String(s).padStart(2,'0')}` : `${m}:${String(s).padStart(2,'0')}`;
}

function formatXml(xml) {
  // 简单美化 XML（单行 → 多行）
  let result = '';
  let indent = 0;
  const tokens = xml.replace(/>\s*</g, '>\n<').split('\n');
  tokens.forEach(token => {
    const trimmed = token.trim();
    if (!trimmed) return;
    if (trimmed.startsWith('</')) indent = Math.max(0, indent - 1);
    result += '  '.repeat(indent) + trimmed + '\n';
    if (!trimmed.startsWith('</') && !trimmed.endsWith('/>') && !trimmed.startsWith('<?') && trimmed.includes('>') && !trimmed.includes('</')) {
      indent++;
    }
  });
  return result.trim();
}

// ─── 探测 & 注入操作 ────────────────────────────────────────────────
async function probeOnly() {
  if (!currentFile) return;
  $('sectionProbeResult').style.display = '';
  $('probeViewer').textContent = '正在探测…';
  try {
    const result = await GET(`/api/ffprobe?path=${encodeURIComponent(currentFile)}`);
    $('probeViewer').textContent = JSON.stringify(result.data || { error: result.error }, null, 2);
    if (!result.success) {
      showToast('探测失败: ' + result.error, 'error');
    } else {
      showToast('探测成功', 'success', 2000);
    }
  } catch (e) {
    $('probeViewer').textContent = '请求失败: ' + e.message;
    showToast('探测请求失败: ' + e.message, 'error');
  }
}

async function injectCurrentFile(force) {
  if (!currentFile) return;
  try {
    const res = await POST('/api/inject', {
      path: currentFile,
      scope: 'file',
      force: force,
      filter_status: [],
      concurrency: config.max_concurrency || 2,
      timeout: config.ffprobe_timeout || 75,
    });
    showToast(`任务已创建: ${res.task_id.slice(0, 8)}…`, 'info');
    watchTask(res.task_id);
    startTaskPolling();
  } catch (e) {
    showToast('创建任务失败: ' + e.message, 'error');
  }
}

async function injectCurrentFileMock() {
  if (!currentFile) return;
  try {
    const res = await POST('/api/inject', {
      path: currentFile,
      scope: 'file',
      force: true,
      filter_status: [],
      concurrency: config.max_concurrency || 2,
      timeout: config.ffprobe_timeout || 75,
      use_mock: true,
    });
    showToast(`虚拟注入已创建: ${res.task_id.slice(0, 8)}…`, 'warning');
    watchTask(res.task_id);
    startTaskPolling();
  } catch (e) {
    showToast('创建任务失败: ' + e.message, 'error');
  }
}

function confirmForceInject() {
  showConfirm(
    '确认强制覆盖',
    `将强制重新探测并注入 MediaInfo 到：\n${currentFile}\n\n即使当前 NFO 已有健康的 MediaInfo 信息，也会被覆盖。`,
    () => injectCurrentFile(true)
  );
}

// ─── 右键菜单（目录操作）────────────────────────────────────
function showContextMenu(x, y, dirPath) {
  currentDirCtx = dirPath;
  const menu = $('contextMenu');
  menu.style.display = 'block';
  menu.style.left = `${Math.min(x, window.innerWidth - 220)}px`;
  menu.style.top = `${Math.min(y, window.innerHeight - 160)}px`;
}

function hideContextMenu() {
  $('contextMenu').style.display = 'none';
}

// ─── 主动扫描 NFO 状态（不运行 FFprobe，仅读 XML）───────
async function scanNfoStatus() {
  if (!currentDirCtx) return;
  hideContextMenu();

  showToast(`正在扫描 NFO 状态: ${currentDirCtx}…`, 'info', 2500);

  try {
    const counts = await GET(`/api/scan?path=${encodeURIComponent(currentDirCtx)}&force=1`);

    // 更新目录徽章缓存
    scanCache[currentDirCtx] = counts;
    const badgeEl = document.querySelector(`.tree-dir-badge[data-dir-path="${currentDirCtx}"]`);
    if (badgeEl) renderDirBadge(badgeEl, counts);

    // 弹出摘要
    const lines = [
      `📊 NFO 状态扫描结果`,
      `目录: ${currentDirCtx}`,
      `─────────────────────`,
      `✅ 健康 (HEALTHY):    ${counts.healthy}`,
      `⚠️ 不完整 (PARTIAL):  ${counts.partial}`,
      `🔴 空白 (EMPTY):      ${counts.empty}  ← Emby 刷库覆盖`,
      `⚫ 缺失 (MISSING):    ${counts.missing}`,
      `─────────────────────`,
      `📁 合计 STRM 文件:   ${counts.total}`,
      `🔧 需要注入:          ${(counts.partial + counts.empty + counts.missing)}`,
    ].join('\n');

    showConfirm('NFO 状态扫描结果', lines, null);
    // 调整确认弹窗为纯信息展示
    $('confirmOk').textContent = '关闭';
    $('confirmCancel').style.display = 'none';
    $('confirmOk').onclick = () => {
      $('confirmModal').style.display = 'none';
      $('confirmCancel').style.display = '';
      $('confirmOk').textContent = '确认';
    };
  } catch (e) {
    showToast('扫描失败: ' + e.message, 'error');
  }
}

async function findIssues() {
  if (!currentDirCtx) return;
  hideContextMenu();

  showToast(`正在查找目录下的问题文件: ${currentDirCtx}…`, 'info', 2500);

  try {
    const res = await GET(`/api/issues?path=${encodeURIComponent(currentDirCtx)}`);
    const issues = res.issues || [];

    $('issuesCount').textContent = `(共找到 ${issues.length} 个问题文件)`;
    const list = $('issuesList');
    list.innerHTML = '';

    if (issues.length === 0) {
      list.innerHTML = '<div style="padding:20px;text-align:center;color:var(--text-muted);">🎉 未发现任何问题文件，该目录下全部健康</div>';
    } else {
      issues.forEach(issue => {
        const item = document.createElement('div');
        item.className = 'issue-item';
        item.innerHTML = `
          <div class="issue-path" title="${issue.path}">${issue.path}</div>
          <div class="issue-status" style="color:${issue.status_color}; border-color:${issue.status_color};">${issue.status_label}</div>
        `;
        item.addEventListener('click', () => {
          $('issuesModal').style.display = 'none';
          selectFile(issue.path);
        });
        list.appendChild(item);
      });
    }

    $('issuesModal').style.display = 'flex';
  } catch (e) {
    showToast('查找失败: ' + e.message, 'error');
  }
}

async function scanContextDir() {
  if (!currentDirCtx) return;
  hideContextMenu();
  showToast(`扫描目录: ${currentDirCtx}`, 'info', 2000);
  delete scanCache[currentDirCtx];
  // 手动刷新：强制服务端重扫该子树（绕过扫描缓存），不复用 lazy-load 的 loadDirStats
  const badgeEl = document.querySelector(`.tree-dir-badge[data-dir-path="${currentDirCtx}"]`);
  try {
    const counts = await GET(`/api/scan?path=${encodeURIComponent(currentDirCtx)}&force=1`);
    scanCache[currentDirCtx] = counts;
    if (badgeEl) renderDirBadge(badgeEl, counts);
  } catch (e) { /* 静默失败 */ }
}

async function refreshMediaIndex() {
  if (!currentDirCtx) return;
  hideContextMenu();
  const toastId = showToast(`正在刷新媒体文件名索引: ${currentDirCtx}…`, 'info', 0);
  try {
    const start = await POST(`/api/media-index/refresh?path=${encodeURIComponent(currentDirCtx)}`);
    const jobId = start.job_id;
    // 轮询后台任务进度
    let s;
    for (let i = 0; i < 600; i++) {  // 最多约 10 分钟
      await new Promise(r => setTimeout(r, 1000));
      s = await GET(`/api/media-index/refresh/status?job_id=${jobId}`);
      const p = s.progress || {};
      const pct = p.total ? Math.round(p.scanned / p.total * 100) : 0;
      updateToast(toastId, `刷新中… ${p.scanned}/${p.total || '?'} (${pct}%)`, 'info');
      if (s.status === 'completed' || s.status === 'failed') break;
    }
    if (s.status === 'completed') {
      const r = s.result || {};
      updateToast(toastId, `已刷新: 扫描 ${r.scanned} / 索引 ${r.indexed} / 未匹配 ${r.missing}`, 'success');
      setTimeout(() => _hideToast(toastId), 4000);
    } else if (s.status === 'failed') {
      updateToast(toastId, '刷新媒体索引失败: ' + (s.error || '未知错误'), 'error');
      setTimeout(() => _hideToast(toastId), 4000);
    } else {
      updateToast(toastId, '刷新超时（仍在后台运行，稍后可重试查看）', 'warning');
      setTimeout(() => _hideToast(toastId), 4000);
    }
  } catch (e) {
    if (toastId) { updateToast(toastId, '刷新媒体索引失败: ' + e.message, 'error'); setTimeout(() => _hideToast(toastId), 4000); }
    else showToast('刷新媒体索引失败: ' + e.message, 'error');
  }
}

async function injectDir(mode) {
  if (!currentDirCtx) return;
  hideContextMenu();

  let filterStatus, label;
  if (mode === 'EMPTY') {
    filterStatus = ['EMPTY'];
    label = '[空白] 状态文件';
  } else if (mode === 'PARTIAL') {
    filterStatus = ['PARTIAL'];
    label = '[不完整] 状态文件';
  }

  try {
    const res = await POST('/api/inject', {
      path: currentDirCtx,
      scope: 'recursive',
      force: false,
      filter_status: filterStatus,
      concurrency: config.max_concurrency || 2,
      timeout: config.ffprobe_timeout || 75,
    });
    showToast(`批量任务已创建: ${res.task_id.slice(0, 8)}…`, 'info');
    watchTask(res.task_id);
    startTaskPolling();
  } catch (e) {
    showToast('创建任务失败: ' + e.message, 'error');
  }
}

function confirmInjectAll() {
  hideContextMenu();
  showConfirm(
    '⚡ 危险：强制全部注入',
    `将对目录 "${currentDirCtx}" 下所有文件进行强制 FFprobe 注入，\n无论当前 MediaInfo 状态如何，均会被覆盖。可能会非常耗时且触发风控！\n\n确认继续？`,
    async () => {
      try {
        const res = await POST('/api/inject', {
          path: currentDirCtx,
          scope: 'recursive',
          force: true,
          filter_status: [],
          concurrency: config.max_concurrency || 2,
          timeout: config.ffprobe_timeout || 75,
        });
        showToast(`强制注入任务已创建: ${res.task_id.slice(0, 8)}…`, 'info');
        watchTask(res.task_id);
        startTaskPolling();
      } catch (e) {
        showToast('创建任务失败: ' + e.message, 'error');
      }
    }
  );
}

function confirmInjectMockAll() {
  hideContextMenu();
  showConfirm(
    '💊 确认全部虚拟注入 (智能跳过健康文件)',
    `将向目录 "${currentDirCtx}" 下所有【有问题（空白/不完整/缺失）】的文件写入“通用/虚拟”的 MediaInfo，跳过 FFprobe 探测！\n\n放心，已经处于【健康】状态的文件会被自动跳过，不会被破坏真实数据。\n\n确认继续？`,
    async () => {
      try {
        const res = await POST('/api/inject', {
          path: currentDirCtx,
          scope: 'recursive',
          force: false,
          filter_status: ['EMPTY', 'PARTIAL', 'MISSING'],
          concurrency: config.max_concurrency || 2,
          timeout: config.ffprobe_timeout || 75,
          use_mock: true,
        });
        showToast(`全部虚拟注入已创建: ${res.task_id.slice(0, 8)}…`, 'warning');
        watchTask(res.task_id);
        startTaskPolling();
      } catch (e) {
        showToast('创建任务失败: ' + e.message, 'error');
      }
    }
  );
}
// ─── 任务面板 ─────────────────────────────────────────────
let tasks = [];
let subscribedTasks = new Set();

function startTaskPolling() {
  if (taskPollingTimer) return;

  const poll = async () => {
    try {
      const data = await GET('/api/tasks');
      if (data.server_time) {
        const serverMs = new Date(data.server_time).getTime();
        serverTimeOffset = Date.now() - serverMs;
      }
      tasks = data.tasks;
      renderTaskPanel();
      // 如果无活跃任务，降低轮询频率
      const hasActive = tasks.some(t => t.status === 'running' || t.status === 'pending');
      if (!hasActive) {
        clearInterval(taskPollingTimer);
        taskPollingTimer = null;
      }
    } catch (e) { /* 静默 */ }
  };

  poll(); // 立即执行一次，修复“先出日志后出活跃任务”的问题
  taskPollingTimer = setInterval(poll, 2000);
}

function renderTaskPanel() {
  const active = tasks.filter(t => t.status === 'running' || t.status === 'pending');
  const history = tasks.filter(t => t.status !== 'running' && t.status !== 'pending');

  const activeEl = $('activeTasks');
  const histEl = $('historyTasks');

  if (active.length === 0) {
    activeEl.innerHTML = '<div class="tasks-empty">暂无活跃任务</div>';
  } else {
    activeEl.innerHTML = active.map(t => renderTaskCard(t)).join('');
    active.forEach(t => {
      const cancelBtn = activeEl.querySelector(`#cancel-${t.task_id}`);
      if (cancelBtn) {
        cancelBtn.addEventListener('click', async () => {
          cancelBtn.textContent = '正在终止…';
          cancelBtn.disabled = true;
          try {
            await DEL(`/api/task/${t.task_id}`);
            showToast('终止信号已发送，正在 kill FFprobe 子进程…', 'info', 3000);
          } catch (e) {
            showToast('发送取消失败: ' + e.message, 'error');
          }
        });
      }
    });
  }

  if (history.length === 0) {
    histEl.innerHTML = '';
  } else {
    histEl.innerHTML = history.slice(0, 20).map(t => renderTaskCard(t)).join('');
  }
  updateTaskTimers();
}

// ─── 任务时间更新 ─────────────────────────────────────────────
function formatDuration(ms) {
  if (ms < 0) ms = 0;
  const totalSeconds = Math.floor(ms / 1000);
  const m = Math.floor(totalSeconds / 60);
  const s = totalSeconds % 60;
  return `${m.toString().padStart(2, '0')}:${s.toString().padStart(2, '0')}`;
}

function updateTaskTimers() {
  document.querySelectorAll('.task-timer').forEach(el => {
    const started = el.dataset.started;
    if (!started) {
      el.textContent = '等待中';
      return;
    }
    // 加上 serverTimeOffset 来修正服务器与本地电脑的时差
    const startMs = new Date(started).getTime() + serverTimeOffset;

    let endMs = Date.now();
    const status = el.dataset.status;
    if (status === 'completed' || status === 'failed' || status === 'cancelled') {
      const finished = el.dataset.finished;
      if (finished) endMs = new Date(finished).getTime() + serverTimeOffset;
    }

    el.textContent = '⏱ ' + formatDuration(endMs - startMs);
  });
}

// 每秒刷新时间
setInterval(updateTaskTimers, 1000);

function renderTaskCard(t) {
  const pct = t.progress?.percent ?? 0;
  const isDone = t.status === 'completed' || t.status === 'failed' || t.status === 'cancelled';
  const fillClass = t.status === 'completed' ? 'done' : t.status === 'failed' ? 'error' : '';
  const shortPath = t.relative_path.split('/').slice(-2).join('/') || t.relative_path;
  const p = t.progress || {};
  const errs = p.errors || {};

  // 错误细分标签
  const errParts = [];
  if (errs.timeout > 0) errParts.push(`<span class="err-tag timeout" title="FFprobe 超时">⏱×${errs.timeout}</span>`);
  if (errs.forbidden > 0) errParts.push(`<span class="err-tag forbidden" title="115 风控 403">🚫×${errs.forbidden}</span>`);
  if (errs.not_found > 0) errParts.push(`<span class="err-tag notfound" title="未找到媒体文件">?×${errs.not_found}</span>`);
  if (errs.inject > 0) errParts.push(`<span class="err-tag inject" title="NFO 注入失败">✍×${errs.inject}</span>`);
  if (errs.other > 0) errParts.push(`<span class="err-tag other" title="其他错误">!×${errs.other}</span>`);

  return `
    <div class="task-card ${t.status}">
      <div class="task-card-header">
        <span class="task-path" title="${t.relative_path}">${shortPath}</span>
        <div style="display: flex; align-items: center; gap: 8px;">
          <span class="task-timer" data-started="${t.started_at || ''}" data-finished="${t.finished_at || ''}" data-status="${t.status}">00:00</span>
          <span class="task-status-pill ${t.status}">${statusLabel(t.status)}</span>
        </div>
      </div>
      <div class="task-progress-bar">
        <div class="task-progress-fill ${fillClass}" style="width:${pct}%"></div>
      </div>
      <div class="task-stats">
        <span class="s-ok" title="成功注入">✓ ${p.success ?? 0}</span>
        <span class="s-skip" title="跳过（已健康或过滤）">→ ${p.skipped ?? 0}</span>
        <span class="s-fail" title="失败">${p.failed > 0 ? '✗' : ''} ${p.failed ?? 0}</span>
        ${p.cancelled > 0 ? `<span class="s-cancel" title="取消时中断">⏹ ${p.cancelled}</span>` : ''}
        <span class="s-pct">${p.processed ?? 0}/${p.total ?? '?'} (${pct}%)</span>
      </div>
      ${errParts.length > 0 ? `<div class="task-err-breakdown">${errParts.join('')}</div>` : ''}
      ${!isDone ? `
        <div class="task-cancel-row">
          <button class="task-cancel-btn" id="cancel-${t.task_id}">
            ⏹ 终止任务
          </button>
          <span class="task-cancel-hint">将立即 kill 当前 FFprobe 进程</span>
        </div>` : ''}
    </div>
  `;
}

function statusLabel(s) {
  return { pending: '等待中', running: '运行中', completed: '完成', cancelled: '已取消', failed: '失败' }[s] || s;
}

// ─── WebSocket 日志订阅 ────────────────────────────────────
function watchTask(taskId) {
  if (subscribedTasks.has(taskId)) return;
  subscribedTasks.add(taskId);

  const wsProto = location.protocol === 'https:' ? 'wss:' : 'ws:';
  const ws = new WebSocket(`${wsProto}//${location.host}/ws/logs/${taskId}`);
  currentWs = ws;

  $('logConnStatus').textContent = '●';
  $('logConnStatus').className = 'log-status connected';

  ws.onmessage = (event) => {
    const msg = JSON.parse(event.data);
    if (msg.type === 'log') {
      appendLog(msg.level, msg.message, msg.timestamp);
    } else if (msg.type === 'progress') {
      startTaskPolling();
      renderTaskPanel();
    } else if (msg.type === 'status') {
      startTaskPolling();
    } else if (msg.type === 'done') {
      appendLog('info', `── 任务 ${taskId.slice(0, 8)} 已结束 ──`, new Date().toISOString());
      ws.close();
      // 重载注入目标目录的可见子节点（刷新圆点/索引徽章）+ 文件详情 + 全局统计
      // 单文件注入 → currentFile 所在目录优先；目录注入（currentFile 无关）→ currentDirCtx
      const fileParent = currentFile ? currentFile.split('/').slice(0, -1).join('/') : '';
      const reloadDir = fileParent || currentDirCtx;
      setTimeout(() => {
        if (reloadDir) reloadDirChildren(reloadDir).catch(() => {});
        if (currentFile) selectFile(currentFile);
      }, 800);
      setTimeout(() => refreshGlobalStats(), 1000);
    }
  };

  ws.onclose = () => {
    subscribedTasks.delete(taskId);
    $('logConnStatus').textContent = '─';
    $('logConnStatus').className = 'log-status disconnected';
  };

  ws.onerror = () => {
    appendLog('error', 'WebSocket 连接错误', new Date().toISOString());
  };
}

function appendLog(level, message, timestamp) {
  const container = $('logContainer');
  const line = document.createElement('div');
  line.className = `log-line ${level}`;

  const time = timestamp ? timestamp.slice(11, 19) : '';
  line.innerHTML = `<span class="log-time">${time}</span><span class="log-msg">${escapeHtml(message)}</span>`;
  container.appendChild(line);

  // 自动滚动到底部
  container.scrollTop = container.scrollHeight;

  // 限制日志行数
  while (container.children.length > 1000) {
    container.removeChild(container.firstChild);
  }
}

function escapeHtml(str) {
  return String(str).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}

function toggleHistorySection() {
  const histEl = $('historyTasks');
  const icon = $('toggleHistory').querySelector('.collapse-icon');
  const collapsed = histEl.style.display === 'none';
  histEl.style.display = collapsed ? '' : 'none';
  icon.style.transform = collapsed ? '' : 'rotate(-90deg)';
}

// ─── 配置模态框 ───────────────────────────────────────────────────
async function openConfigModal() {
  try {
    config = await GET('/api/config');
    // FFprobe settings
    $('cfgTimeout').value = config.ffprobe_timeout || 75;
    $('cfgConcurrency').value = config.max_concurrency || 2;
    $('cfgMaxRetries').value = config.max_retries || 3;
    $('cfgRetryDelay').value = config.retry_delay || 2;
    $('cfgForbiddenDelay').value = config.forbidden_retry_delay || 5;
    $('cfgExtensions').value = (config.guess_extensions || []).join(',');
    $('cfgScanCacheTtl').value = config.scan_cache_ttl ?? 600;
    // exclude_dirs 不在 UI 暴露：后端 PUT 时省略该字段即保留既有默认值

    renderLibrariesList(config.libraries || []);
    $('configModal').style.display = 'flex';
  } catch (e) {
    showToast('加载配置失败: ' + e.message, 'error');
  }
}

async function saveConfig() {
  const libraries = collectLibraries();
  const payload = {
    libraries: libraries,
    ffprobe_timeout: parseInt($('cfgTimeout').value),
    max_concurrency: parseInt($('cfgConcurrency').value),
    max_retries: parseInt($('cfgMaxRetries').value),
    retry_delay: parseFloat($('cfgRetryDelay').value),
    forbidden_retry_delay: parseFloat($('cfgForbiddenDelay').value),
    guess_extensions: $('cfgExtensions').value.split(',').map(s => s.trim()).filter(Boolean),
    scan_cache_ttl: parseFloat($('cfgScanCacheTtl').value),
    // exclude_dirs 故意省略：后端保留既有默认值（不在 UI 暴露）
  };
  try {
    config = await PUT('/api/config', payload);
    $('configModal').style.display = 'none';
    showToast('配置已保存', 'success', 2500);
    // 重新加载目录树
    treeData = {};
    scanCache = {};
    await loadTreeRoot();
    await refreshGlobalStats();
  } catch (e) {
    showToast('保存失败: ' + e.message, 'error');
  }
}

function switchConfigTab(tab) {
  document.querySelectorAll('.config-tab').forEach(btn => btn.classList.toggle('active', btn.dataset.tab === tab));
  document.querySelectorAll('.config-panel').forEach(panel => panel.classList.toggle('active', panel.id === `tab${tab.charAt(0).toUpperCase() + tab.slice(1)}`));
}

function renderLibrariesList(libraries) {
  const list = $('librariesList');
  list.innerHTML = '';
  libraries.forEach(l => addLibraryRow(list, l.name || '', l.strm_path || '', l.media_path || '', l.media_url_root || '', l.enabled ?? true, l.id));
}

function addLibraryRule() {
  addLibraryRow($('librariesList'), '', '', '', '', true);
}

function addLibraryRow(container, name, strmPath, mediaPath, mediaUrlRoot, enabled, id = '') {
  const row = document.createElement('div');
  row.className = 'mapping-rule library-rule';
  if (id) row.dataset.id = id;
  row.innerHTML = `
    <input type="text" placeholder="库名" value="${escapeHtml(name)}" class="library-name" />
    <input type="text" placeholder="STRM 路径（容器内绝对路径）" value="${escapeHtml(strmPath)}" class="library-strm" />
    <input type="text" placeholder="Media 路径（容器内绝对路径）" value="${escapeHtml(mediaPath)}" class="library-media" />
    <input type="text" placeholder="OpenList URL根(可选, 留空走ffprobe)" value="${escapeHtml(mediaUrlRoot)}" class="library-urlroot" />
    <label class="library-enabled" title="启用" style="padding:0 4px;"><input type="checkbox" ${enabled ? 'checked' : ''} />启用</label>
    <button class="mapping-remove" title="删除">✕</button>
  `;
  row.querySelector('.mapping-remove').addEventListener('click', () => row.remove());
  container.appendChild(row);
}

function collectLibraries() {
  return Array.from(document.querySelectorAll('.library-rule')).map(row => {
    const id = row.dataset.id || '';
    const res = {
      name: row.querySelector('.library-name').value.trim(),
      strm_path: row.querySelector('.library-strm').value.trim(),
      media_path: row.querySelector('.library-media').value.trim(),
      media_url_root: row.querySelector('.library-urlroot').value.trim(),
      enabled: row.querySelector('.library-enabled input').checked,
    };
    if (id) res.id = id;
    return res;
  }).filter(m => m.strm_path && m.media_path);
}

// ─── 确认弹窗 ───────────────────────────────────────────────
let confirmCallback = null;
function showConfirm(title, body, onConfirm) {
  $('confirmTitle').textContent = title;
  $('confirmBody').innerHTML = escapeHtml(body).replace(/\n/g, '<br>');
  confirmCallback = onConfirm;
  $('confirmModal').style.display = 'flex';
  $('confirmOk').onclick = () => {
    $('confirmModal').style.display = 'none';
    if (confirmCallback) confirmCallback();
  };
}

// ─── 辅助函数 ───────────────────────────────────────────────────
function copyText(text) {
  navigator.clipboard.writeText(text).then(
    () => showToast('已复制到剪贴板', 'success', 2000),
    () => showToast('复制失败', 'error')
  );
}

// ─── 启动 ───────────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', init);
