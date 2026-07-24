/**
 * 毛竹 工具函数（参考 IOTHub）
 */
function showToast(message, type) {
    type = type || 'info';
    var container = document.querySelector('.toast-container');
    if (!container) {
        container = document.createElement('div');
        container.className = 'toast-container';
        document.body.appendChild(container);
    }
    var toast = document.createElement('div');
    toast.className = 'toast toast-' + type;
    toast.textContent = message;
    container.appendChild(toast);
    setTimeout(function() { toast.remove(); }, 3000);
}

function statusBadge(status) {
    var tfn = (typeof t === 'function' ? t : null);
    var map = {
        online: [tfn ? tfn('common.stOnline') : '在线', 'badge-online'],
        offline: [tfn ? tfn('common.stOffline') : '离线', 'badge-offline'],
        active: [tfn ? tfn('common.stActive') : '正常', 'badge-online'],
        locked: [tfn ? tfn('common.stLocked') : '安全锁定', 'badge-danger'],
        suspended: [tfn ? tfn('common.stSuspended') : '暂停', 'badge-danger'],
        disabled: [tfn ? tfn('common.stDisabled') : '禁用', 'badge-danger'],
        admin: [tfn ? tfn('common.stAdmin') : '管理员', 'badge-admin'],
        user: [tfn ? tfn('common.stUser') : '用户', 'badge-user'],
    };
    var pair = map[status] || [status, 'badge-info'];
    return '<span class="badge ' + pair[1] + '">' + pair[0] + '</span>';
}

function parseEdgeOpsDate(iso) {
    if (iso == null || iso === '') return null;
    if (iso instanceof Date) return isNaN(iso.getTime()) ? null : iso;
    if (typeof iso === 'number') {
        var dn = new Date(iso);
        return isNaN(dn.getTime()) ? null : dn;
    }
    var s = String(iso).trim();
    if (!s) return null;
    if (/^\d{4}-\d{2}-\d{2} \d{2}:\d{2}/.test(s)) s = s.replace(' ', 'T');
    if (/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}/.test(s)) {
        // 后端 SQLite 时间通常是 UTC（无偏移时按 UTC 解释，避免被当成本地时间）
        if (!/[zZ]$/.test(s) && !/[+-]\d{2}:?\d{2}$/.test(s)) s = s + 'Z';
    }
    var d = new Date(s);
    return isNaN(d.getTime()) ? null : d;
}

/** 按浏览器本地时区格式化时间，统一 YYYY-MM-DD HH:MM:SS。 */
function formatTime(iso) {
    if (!iso) return '-';
    var d = parseEdgeOpsDate(iso);
    if (!d) return String(iso);
    try {
        var _ui = (typeof I18n !== 'undefined' && I18n.locale) ? I18n.locale : 'zh-CN';
        var _fmtLoc = _ui === 'en' ? 'en-CA' : 'zh-CN';
        var fmt = new Intl.DateTimeFormat(_fmtLoc, {
            year: 'numeric',
            month: '2-digit',
            day: '2-digit',
            hour: '2-digit',
            minute: '2-digit',
            second: '2-digit',
            hourCycle: 'h23'
        });
        var parts = fmt.formatToParts(d);
        var get = function(t) {
            for (var i = 0; i < parts.length; i++) if (parts[i].type === t) return parts[i].value;
            return '';
        };
        return get('year') + '-' + get('month') + '-' + get('day') + ' ' + get('hour') + ':' + get('minute') + ':' + get('second');
    } catch (e) {
        var pad = function(n) { return String(n).padStart(2, '0'); };
        return d.getFullYear() + '-' + pad(d.getMonth() + 1) + '-' + pad(d.getDate()) + ' ' + pad(d.getHours()) + ':' + pad(d.getMinutes()) + ':' + pad(d.getSeconds());
    }
}

/** 与 formatTime 同样按浏览器本地时区，但仅返回到分钟（YYYY-MM-DD HH:MM）。
 *  用于留言板、反馈、离线申请等只需到分钟的展示位；避免直接对后端 UTC 字符串做 slice
 *  导致"展示成 UTC 当地化字符串"的歧义。 */
function formatTimeShort(iso) {
    if (!iso) return '-';
    var s = formatTime(iso);
    if (!s || s === '-') return s || '-';
    return s.length >= 16 ? s.slice(0, 16) : s;
}
if (typeof window !== 'undefined') {
    window.formatTime = formatTime;
    window.formatTimeShort = formatTimeShort;
    window.parseEdgeOpsDate = parseEdgeOpsDate;
}

function esc(str) {
    if (!str) return '';
    return String(str).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

/** 表格长文本预览：注册全文，返回 data-preview-id。 */
var _edgeopsCellPreviewStore = new Map();
var _edgeopsCellPreviewSeq = 0;
function edgeopsTablePreviewRegister(text) {
    var full = text == null ? '' : String(text);
    if (!full) return '';
    var id = 'pv' + (++_edgeopsCellPreviewSeq);
    _edgeopsCellPreviewStore.set(id, full);
    return id;
}

/** 表格单元格：长字段单行省略，悬停显示完整预览。opts: fill, mono, cls, placeholder */
function edgeopsTableTdEllipsis(text, opts) {
    opts = opts || {};
    var full = text == null ? '' : String(text);
    var classes = ['td-ellipsis'];
    if (opts.fill) classes.push('td-fill');
    if (opts.cls) classes.push(opts.cls);
    if (!full) {
        return '<td class="' + classes.join(' ') + '">' + esc(opts.placeholder || '—') + '</td>';
    }
    var pid = edgeopsTablePreviewRegister(full);
    var innerCls = 'cell-ellipsis cell-ellipsis-preview' + (opts.mono ? ' cell-ellipsis-mono' : '');
    return '<td class="' + classes.join(' ') + '"><span class="' + innerCls + '" data-preview-id="' + pid + '">' + esc(full) + '</span></td>';
}

/** 表格单元格：短字段不换行。opts.html=true 时 content 为已转义 HTML。 */
function edgeopsTableTdNowrap(content, opts) {
    opts = opts || {};
    var cls = 'td-nowrap' + (opts.cls ? ' ' + opts.cls : '');
    var body = opts.html ? (content || '') : esc(content == null ? '' : String(content));
    return '<td class="' + cls + '">' + body + '</td>';
}

(function edgeopsInstallTableCellPreview() {
    if (typeof document === 'undefined' || window._edgeopsTablePreviewInstalled) return;
    window._edgeopsTablePreviewInstalled = true;
    var pop = null;
    var hideTimer = null;
    function ensurePop() {
        if (pop) return pop;
        pop = document.createElement('div');
        pop.id = 'edgeops-table-cell-preview';
        pop.className = 'edgeops-table-cell-preview';
        pop.setAttribute('role', 'tooltip');
        document.body.appendChild(pop);
        pop.addEventListener('mouseenter', function() { if (hideTimer) clearTimeout(hideTimer); });
        pop.addEventListener('mouseleave', function() { scheduleHide(); });
        return pop;
    }
    function hidePop() {
        if (pop) pop.style.display = 'none';
    }
    function scheduleHide() {
        if (hideTimer) clearTimeout(hideTimer);
        hideTimer = setTimeout(hidePop, 100);
    }
    function cancelHide() {
        if (hideTimer) clearTimeout(hideTimer);
    }
    function showPreview(anchor) {
        var id = anchor.getAttribute('data-preview-id');
        var text = (id && _edgeopsCellPreviewStore.has(id)) ? _edgeopsCellPreviewStore.get(id) : anchor.textContent;
        if (!text) return;
        var p = ensurePop();
        p.textContent = text;
        p.classList.toggle('cell-ellipsis-mono', anchor.classList.contains('cell-ellipsis-mono'));
        p.style.display = 'block';
        p.style.visibility = 'hidden';
        p.style.left = '-9999px';
        p.style.top = '0';
        var pw = p.offsetWidth;
        var ph = p.offsetHeight;
        var rect = anchor.getBoundingClientRect();
        var pad = 8;
        var left = Math.max(pad, Math.min(rect.left, window.innerWidth - pw - pad));
        var top = rect.bottom + pad;
        if (top + ph > window.innerHeight - pad) {
            top = Math.max(pad, rect.top - ph - pad);
        }
        p.style.left = left + 'px';
        p.style.top = top + 'px';
        p.style.visibility = '';
    }
    document.addEventListener('mouseover', function(ev) {
        if (ev.target.closest && ev.target.closest('.edgeops-table-cell-preview')) {
            cancelHide();
            return;
        }
        var cell = ev.target.closest ? ev.target.closest('.cell-ellipsis-preview') : null;
        cancelHide();
        if (!cell) {
            scheduleHide();
            return;
        }
        showPreview(cell);
    });
    document.addEventListener('scroll', scheduleHide, true);
    window.addEventListener('resize', scheduleHide);
})();

/** 页面顶部说明段落；空字符串时不渲染。plain=true 时对文本转义（无 HTML）。 */
function edgeopsPageIntroHtml(text, plain) {
    var s = String(text == null ? '' : text).trim();
    if (!s) return '';
    var body = plain ? esc(s) : s;
    return '<p class="page-intro">' + body + '</p>';
}

var _modalKeyHandler = null;
var _modalOnCancel = null;
var EDGEOPS_SAFE_HTML_TAG_NAMES = 'strong|em|b|i|u|code|kbd|mark|span|a|sup|sub';
var EDGEOPS_SAFE_HTML_TAG_RE = new RegExp('^(?:' + EDGEOPS_SAFE_HTML_TAG_NAMES + ')$', 'i');
var EDGEOPS_DIAGRAM_LANG_MAP = {
    mermaid: 'mermaid',
    markmap: 'markmap',
    mindmap: 'markmap',
    echarts: 'echarts',
    'echarts-option': 'echarts',
    chart: 'echarts',
    svg: 'svg',
    xml: 'svg',
    three: 'three',
    'three-scene': 'three',
    'threejs': 'three',
    '3d': 'three'
};

/**
 * @param {string} title
 * @param {string} content HTML
 * @param {string} [footer] HTML
 * @param {{ onCancel?: function, enterSubmitsInput?: boolean, dangerOk?: boolean }} [opts]
 */
function showModal(title, content, footer, opts) {
    opts = opts || {};
    closeModal();
    footer = footer || '';
    _modalOnCancel = typeof opts.onCancel === 'function' ? opts.onCancel : null;
    var html = '<div class="modal-overlay" id="edgeopsModalOverlay">' +
        '<div class="modal" role="dialog" aria-modal="true">' +
        '<div class="modal-header"><h3>' + title + '</h3>' +
        '<button type="button" class="modal-close" data-edgeops-modal-cancel="1" aria-label="Close">&times;</button></div>' +
        '<div class="modal-body">' + content + '</div>' +
        (footer ? '<div class="modal-footer">' + footer + '</div>' : '') +
        '</div></div>';
    document.body.insertAdjacentHTML('beforeend', html);
    var overlay = document.getElementById('edgeopsModalOverlay');
    if (overlay) {
        overlay.addEventListener('click', function(e) {
            if (e.target === overlay) {
                if (_modalOnCancel) _modalOnCancel();
                else closeModal();
            }
        });
        var cancelBtns = overlay.querySelectorAll('[data-edgeops-modal-cancel="1"]');
        Array.prototype.forEach.call(cancelBtns, function(btn) {
            btn.addEventListener('click', function() {
                if (_modalOnCancel) _modalOnCancel();
                else closeModal();
            });
        });
    }
    _modalKeyHandler = function(e) {
        if (e.isComposing || e.keyCode === 229) return;
        if (e.key === 'Escape' || e.keyCode === 27) {
            e.preventDefault();
            if (_modalOnCancel) _modalOnCancel();
            else closeModal();
            return;
        }
        if (e.key === 'Enter' || e.keyCode === 13) {
            var active = document.activeElement;
            var ov = document.getElementById('edgeopsModalOverlay') || document.querySelector('.modal-overlay');
            if (!ov) return;
            // prompt：输入框内 Enter = 确定
            if (opts.enterSubmitsInput && active && ov.contains(active)
                && (active.tagName === 'INPUT' || active.tagName === 'TEXTAREA')) {
                e.preventDefault();
                var okIn = ov.querySelector('.modal-footer [data-edgeops-modal-ok="1"]')
                    || ov.querySelector('.modal-footer .btn-primary')
                    || ov.querySelector('.modal-footer .btn-danger');
                if (okIn) okIn.click();
                return;
            }
            if (active && (active.tagName === 'INPUT' || active.tagName === 'TEXTAREA' || active.tagName === 'SELECT')) return;
            e.preventDefault();
            if (active && ov.contains(active) && active.tagName === 'BUTTON' && !active.getAttribute('data-edgeops-modal-cancel')) {
                active.click();
                return;
            }
            var primary = ov.querySelector('.modal-footer [data-edgeops-modal-ok="1"]')
                || ov.querySelector('.modal-footer .btn-danger')
                || ov.querySelector('.modal-footer .btn-primary');
            if (primary) primary.click();
        }
    };
    document.addEventListener('keydown', _modalKeyHandler, true);
}

function closeModal() {
    if (_modalKeyHandler) {
        document.removeEventListener('keydown', _modalKeyHandler, true);
        _modalKeyHandler = null;
    }
    _modalOnCancel = null;
    document.querySelectorAll('.modal-overlay').forEach(function(el) { el.remove(); });
}

/** 确认框：Enter=确定，Esc/取消=否。返回 Promise&lt;boolean&gt; */
function showConfirm(title, message, opts) {
    opts = opts || {};
    return new Promise(function(resolve) {
        var settled = false;
        function finish(value) {
            if (settled) return;
            settled = true;
            closeModal();
            resolve(!!value);
        }
        var btnCancel = (typeof t === 'function' ? t('common.cancel') : 'Cancel');
        var btnOk = opts.okLabel || (typeof t === 'function' ? t('common.confirm') : 'Confirm');
        var okClass = opts.danger === false ? 'btn btn-primary' : 'btn btn-danger';
        var msgHtml = '<p class="edgeops-dialog-message" style="white-space:pre-wrap;margin:0;line-height:1.5">'
            + esc(String(message == null ? '' : message)) + '</p>';
        var hint = '<p class="edgeops-dialog-hotkeys" style="margin:10px 0 0;font-size:12px;opacity:.55">'
            + esc(typeof t === 'function' ? t('common.dialogHotkeysConfirm') : 'Enter confirm · Esc cancel')
            + '</p>';
        showModal(
            esc(String(title || (typeof t === 'function' ? t('common.confirm') : 'Confirm'))),
            msgHtml + hint,
            '<button type="button" class="btn" data-edgeops-modal-cancel="1">' + esc(btnCancel) + '</button>'
                + ' <button type="button" class="' + okClass + '" data-edgeops-modal-ok="1" id="edgeopsConfirmOk">'
                + esc(btnOk) + '</button>',
            { onCancel: function() { finish(false); } }
        );
        var okBtn = document.getElementById('edgeopsConfirmOk');
        if (okBtn) okBtn.onclick = function() { finish(true); };
        setTimeout(function() {
            if (okBtn) try { okBtn.focus(); } catch (_e) {}
        }, 0);
    });
}

/** 输入框：Enter=确定，Esc/取消=null。返回 Promise&lt;string|null&gt; */
function showPrompt(title, message, defaultValue, opts) {
    opts = opts || {};
    return new Promise(function(resolve) {
        var settled = false;
        function finish(value) {
            if (settled) return;
            settled = true;
            closeModal();
            resolve(value);
        }
        var btnCancel = (typeof t === 'function' ? t('common.cancel') : 'Cancel');
        var btnOk = opts.okLabel || (typeof t === 'function' ? t('common.ok') : 'OK');
        var inputId = 'edgeopsPromptInput';
        var msgHtml = message
            ? ('<p class="edgeops-dialog-message" style="white-space:pre-wrap;margin:0 0 10px;line-height:1.5">'
                + esc(String(message)) + '</p>')
            : '';
        var inputHtml = '<input class="form-control" id="' + inputId + '" type="'
            + (opts.password ? 'password' : 'text') + '" value="'
            + esc(String(defaultValue == null ? '' : defaultValue)) + '"'
            + (opts.placeholder ? (' placeholder="' + esc(String(opts.placeholder)) + '"') : '')
            + ' autocomplete="' + (opts.password ? 'new-password' : 'off') + '">';
        var hint = '<p class="edgeops-dialog-hotkeys" style="margin:10px 0 0;font-size:12px;opacity:.55">'
            + esc(typeof t === 'function' ? t('common.dialogHotkeysPrompt') : 'Enter OK · Esc cancel')
            + '</p>';
        showModal(
            esc(String(title || (typeof t === 'function' ? t('common.input') : 'Input'))),
            msgHtml + inputHtml + hint,
            '<button type="button" class="btn" data-edgeops-modal-cancel="1">' + esc(btnCancel) + '</button>'
                + ' <button type="button" class="btn btn-primary" data-edgeops-modal-ok="1" id="edgeopsPromptOk">'
                + esc(btnOk) + '</button>',
            { onCancel: function() { finish(null); }, enterSubmitsInput: true }
        );
        function submitOk() {
            var inp = document.getElementById(inputId);
            var v = inp ? String(inp.value) : '';
            if (opts.trim !== false) v = v.trim();
            if (!opts.allowEmpty && !v) {
                if (inp) try { inp.focus(); } catch (_f) {}
                return;
            }
            finish(v);
        }
        var okBtn = document.getElementById('edgeopsPromptOk');
        if (okBtn) okBtn.onclick = function() { submitOk(); };
        setTimeout(function() {
            var inp = document.getElementById(inputId);
            if (inp) {
                try {
                    inp.focus();
                    if (typeof inp.select === 'function') inp.select();
                } catch (_e) {}
            }
        }, 0);
    });
}

/** 提示框：Enter/Esc 关闭。返回 Promise&lt;void&gt; */
function showAlert(title, message) {
    return new Promise(function(resolve) {
        var settled = false;
        function finish() {
            if (settled) return;
            settled = true;
            closeModal();
            resolve();
        }
        var btnOk = (typeof t === 'function' ? t('common.ok') : 'OK');
        var msgHtml = '<p class="edgeops-dialog-message" style="white-space:pre-wrap;margin:0;line-height:1.5">'
            + esc(String(message == null ? '' : message)) + '</p>';
        var hint = '<p class="edgeops-dialog-hotkeys" style="margin:10px 0 0;font-size:12px;opacity:.55">'
            + esc(typeof t === 'function' ? t('common.dialogHotkeysAlert') : 'Enter / Esc to close')
            + '</p>';
        showModal(
            esc(String(title || (typeof t === 'function' ? t('common.tip') : 'Notice'))),
            msgHtml + hint,
            '<button type="button" class="btn btn-primary" data-edgeops-modal-ok="1" id="edgeopsAlertOk">'
                + esc(btnOk) + '</button>',
            { onCancel: finish }
        );
        var okBtn = document.getElementById('edgeopsAlertOk');
        if (okBtn) okBtn.onclick = function() { finish(); };
        setTimeout(function() {
            if (okBtn) try { okBtn.focus(); } catch (_e) {}
        }, 0);
    });
}

/** 兼容原生 confirm(msg)：仅 message。Enter=是 Esc=否 */
function edgeopsConfirm(message, title) {
    return showConfirm(
        title || (typeof t === 'function' ? t('common.confirm') : '确认'),
        message
    );
}

/** 兼容原生 prompt(msg, default)：取消返回 null */
function edgeopsPrompt(message, defaultValue, title, opts) {
    return showPrompt(
        title || (typeof t === 'function' ? t('common.input') : '输入'),
        message,
        defaultValue,
        opts
    );
}

function isAdmin() {
    if (!API.user || !API.user.role) return false;
    var r = String(API.user.role).trim().toLowerCase();
    return r === 'admin' || r === 'manager' || API.user.role === '管理员';
}

/** 安全转义 HTML（用于代码块/表格等，来自 IOTHub） */
function escapeHtmlForCode(text) {
    if (text == null) return '';
    return String(text)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;');
}

/**
 * 替换 Markdown 管道生成的编号占位符（EDGEOPSMDINLINE1、EDGEOPSMDINLINECODE10 等）。
 *
 * 禁止 `str.replace('INLINE_1', ...)`：
 * 1) 只会替换第一次出现；
 * 2) 会误匹配 `INLINE_10` / `INLINE_100` 的前缀，把单元格渲染成未展开的 INLINE… 或破碎内容
 *    （用户常见现象：`INLINE0 → INLINE1` 一类「占位没被换掉」）。
 * 3) 占位符不能包含 `_`，否则后续 Markdown 斜体规则会把两个 `_` 之间的内容吃掉，导致无法还原。
 */
function edgeopsReplaceIndexedPlaceholder(str, prefix, index, replacement) {
    if (str == null || str === '') return str;
    var esc = String(prefix).replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
    var re = new RegExp(esc + Number(index) + '(?!\\d)', 'g');
    return String(str).replace(re, replacement);
}

function edgeopsToHalfwidthAscii(text) {
    if (text == null) return '';
    return String(text)
        .replace(/[\uFF01-\uFF5E]/g, function(ch) { return String.fromCharCode(ch.charCodeAt(0) - 0xFEE0); })
        .replace(/\u3000/g, ' ');
}

function normalizeSafeHtmlTags(text) {
    if (!text) return '';
    return String(text).replace(/[<＜][^>＞]{0,200}[>＞]/g, function(match) {
        var ascii = edgeopsToHalfwidthAscii(match).replace(/＜/g, '<').replace(/＞/g, '>');
        var tagMatch = ascii.match(/^<\s*(\/?)\s*([a-z][a-z0-9]*)\s*([^>]*)>$/i);
        if (!tagMatch) return match;
        if (!EDGEOPS_SAFE_HTML_TAG_RE.test(tagMatch[2])) return match;
        var closing = tagMatch[1] || '';
        var attrs = closing ? '' : (tagMatch[3] || '').trim();
        return '<' + closing + tagMatch[2].toLowerCase() + (attrs ? ' ' + attrs : '') + '>';
    });
}

function edgeopsCompactMarkdownBlankLines(text) {
    if (!text) return '';
    var blocks = [];
    var s = String(text).replace(/\x60\x60\x60([^\n`]*)\n?([\s\S]*?)\x60\x60\x60/g, function(match) {
        var id = 'EDGEOPSMDBLANKBLOCK' + blocks.length;
        blocks.push(match);
        return id;
    });
    s = s.replace(/[ \t]+\n/g, '\n');
    // 连续列表项之间的空白行通常只是模型输出噪声，会导致多个 ul/ol 被分开渲染。
    s = s.replace(/^(\s*(?:[-*+]|\d+\.)\s+[^\n]*?)\n[ \t]*\n(?=\s*(?:[-*+]|\d+\.)\s+)/gm, '$1\n');
    // 段落之间最多保留一个空白行，避免聊天气泡出现大片无意义留白。
    s = s.replace(/\n{3,}/g, '\n\n');
    for (var i = 0; i < blocks.length; i++) {
        s = edgeopsReplaceIndexedPlaceholder(s, 'EDGEOPSMDBLANKBLOCK', i, blocks[i]);
    }
    return s.trim();
}

function edgeopsNormalizeSafeHref(href) {
    href = String(href || '').trim();
    if (!href) return '#';
    if (/^(https?:|mailto:|tel:|\/|#)/i.test(href)) return edgeopsRewriteChatAttachmentUrl(href);
    return '#';
}

/** 从 /api/ai/attachments/<uuid> 路径提取 uuid（忽略已有 query）。 */
function edgeopsExtractAttachmentUuidFromUrl(url) {
    if (!url) return null;
    var m = String(url).trim().match(/\/api\/ai\/attachments\/([0-9a-fA-F]+)/i);
    return m ? m[1] : null;
}

/** 聊天图片 URL：附件与 web/fs 直链须带 ?token=（浏览器无法带 Authorization 头）。 */
function edgeopsRewriteChatAttachmentUrl(url) {
    var raw = String(url || '').trim();
    if (!raw) return raw;
    var uuid = edgeopsExtractAttachmentUuidFromUrl(raw);
    if (uuid) {
        if (typeof API !== 'undefined' && API && typeof API.buildChatAttachmentUrl === 'function') {
            return API.buildChatAttachmentUrl(uuid);
        }
        var out = '/api/ai/attachments/' + encodeURIComponent(uuid);
        if (typeof API !== 'undefined' && API && API.token) {
            out += '?token=' + encodeURIComponent(API.token);
        }
        return out;
    }
    var fsRel = '';
    var fsM = raw.match(/^\/api\/fs\/file\/([^?#]+)/i);
    if (fsM) {
        try {
            fsRel = fsM[1].split('/').map(function(seg) { return decodeURIComponent(seg); }).join('/');
        } catch (_eFs) {
            fsRel = fsM[1];
        }
    } else if (!/^https?:\/\//i.test(raw) && !raw.startsWith('/') && /^chats\//i.test(raw)) {
        fsRel = raw.split('?')[0].split('#')[0];
    }
    if (fsRel && typeof API !== 'undefined' && API && typeof API.buildFsFileUrl === 'function') {
        return API.buildFsFileUrl(fsRel);
    }
    return raw;
}

function edgeopsSanitizeInlineStyle(styleText) {
    if (!styleText) return '';
    var allowed = {
        color: true,
        'background-color': true,
        'font-weight': true,
        'font-style': true,
        'text-decoration': true
    };
    var out = [];
    String(styleText).split(';').forEach(function(part) {
        var idx = part.indexOf(':');
        if (idx <= 0) return;
        var prop = part.slice(0, idx).trim().toLowerCase();
        var value = part.slice(idx + 1).trim();
        if (!allowed[prop] || !value) return;
        var safe = '';
        if (prop === 'color' || prop === 'background-color') {
            if (/^(#[0-9a-f]{3,8}|rgba?\([0-9.,%\s]+\)|hsla?\([0-9.,%\s]+\)|currentcolor|transparent|inherit|white|black|gray|grey|silver|red|orange|yellow|green|blue|cyan|purple|pink)$/i.test(value)) {
                safe = value;
            }
        } else if (prop === 'font-weight') {
            if (/^(normal|bold|bolder|lighter|[1-9]00)$/i.test(value)) safe = value;
        } else if (prop === 'font-style') {
            if (/^(normal|italic|oblique)$/i.test(value)) safe = value;
        } else if (prop === 'text-decoration') {
            if (/^(none|underline|line-through|overline|underline line-through|line-through underline)$/i.test(value)) safe = value;
        }
        if (safe) out.push(prop + ': ' + safe);
    });
    return out.join('; ');
}

function edgeopsBuildSafeInlineAttrsFromNode(node) {
    if (!node || node.nodeType !== 1) return '';
    var tag = (node.tagName || '').toLowerCase();
    var attrs = [];
    if (tag === 'a') {
        attrs.push('href="' + escapeHtmlForCode(edgeopsNormalizeSafeHref(node.getAttribute('href'))) + '"');
        attrs.push('target="_blank"');
        attrs.push('rel="noopener noreferrer"');
    } else if (tag === 'span' || tag === 'mark') {
        var safeStyle = edgeopsSanitizeInlineStyle(node.getAttribute('style'));
        if (safeStyle) attrs.push('style="' + escapeHtmlForCode(safeStyle) + '"');
    }
    return attrs.length ? (' ' + attrs.join(' ')) : '';
}

function edgeopsBuildSafeInlineTag(tag, attrs, inner) {
    tag = String(tag || '').toLowerCase();
    if (!EDGEOPS_SAFE_HTML_TAG_RE.test(tag)) return inner || '';
    if (tag === 'br') return '<br>';
    var wrapper = document.createElement('div');
    wrapper.innerHTML = '<' + tag + (attrs ? ' ' + attrs : '') + '></' + tag + '>';
    var node = wrapper.firstElementChild;
    var safeAttrs = node ? edgeopsBuildSafeInlineAttrsFromNode(node) : '';
    return '<' + tag + safeAttrs + '>' + (inner || '') + '</' + tag + '>';
}

function edgeopsSanitizeSafeInlineHtml(text) {
    if (!text) return '';
    var container = document.createElement('div');
    container.innerHTML = normalizeSafeHtmlTags(text);
    function sanitizeNode(node) {
        if (!node) return '';
        if (node.nodeType === 3) return escapeHtmlForCode(node.nodeValue || '');
        if (node.nodeType !== 1) return '';
        var tag = (node.tagName || '').toLowerCase();
        if (!EDGEOPS_SAFE_HTML_TAG_RE.test(tag)) {
            return escapeHtmlForCode(node.outerHTML || node.textContent || '');
        }
        if (tag === 'br') return '<br>';
        var inner = '';
        for (var i = 0; i < node.childNodes.length; i++) {
            inner += sanitizeNode(node.childNodes[i]);
        }
        return '<' + tag + edgeopsBuildSafeInlineAttrsFromNode(node) + '>' + inner + '</' + tag + '>';
    }
    var out = '';
    for (var i = 0; i < container.childNodes.length; i++) {
        out += sanitizeNode(container.childNodes[i]);
    }
    return out;
}

function edgeopsNormalizeDiagramLang(lang) {
    lang = String(lang || '').trim().toLowerCase();
    return EDGEOPS_DIAGRAM_LANG_MAP[lang] || '';
}

function edgeopsBuildDiagramBlockHtml(type, source, lang) {
    var encoded = '';
    try { encoded = encodeURIComponent(source || ''); } catch (e) {}
    var label = type === 'mermaid' ? 'Mermaid'
        : (type === 'markmap' ? 'Markmap'
            : (type === 'svg' ? 'SVG'
                : (type === 'three' ? 'Three.js' : 'ECharts')));
    var _t = (typeof t === 'function' ? t : function (k) { return k; });
    return ''
        + '<div class="chat-diagram-block" data-diagram-type="' + escapeHtmlForCode(type) + '" data-diagram-source="' + escapeHtmlForCode(encoded) + '" data-diagram-lang="' + escapeHtmlForCode(lang || type) + '">'
        + '<div class="chat-diagram-toolbar">'
        + '<span class="chat-diagram-kind">' + label + '</span>'
        + '<button type="button" class="btn btn-sm chat-diagram-btn" data-diagram-action="preview">' + _t('ui.diagram.preview') + '</button>'
        + '<button type="button" class="btn btn-sm chat-diagram-btn" data-diagram-action="png">' + _t('ui.diagram.exportPng') + '</button>'
        + '<button type="button" class="btn btn-sm chat-diagram-btn" data-diagram-action="svg">' + _t('ui.diagram.exportSvg') + '</button>'
        + '<button type="button" class="btn btn-sm chat-diagram-btn" data-diagram-action="copy">' + _t('ui.diagram.copySource') + '</button>'
        + '<button type="button" class="btn btn-sm chat-diagram-btn" data-diagram-action="toggle-source">' + _t('ui.diagram.expandSource') + '</button>'
        + '</div>'
        + '<div class="chat-diagram-canvas"><div class="chat-diagram-placeholder">' + _t('ui.diagram.loading') + '</div></div>'
        + '<div class="chat-diagram-error" style="display:none"></div>'
        + '<pre class="chat-diagram-source" style="display:none"><code>' + escapeHtmlForCode(source || '') + '</code></pre>'
        + '</div>';
}

function edgeopsNormalizeMathDelimiters(text) {
    return String(text || '').replace(/\uFF04/g, '$');
}

function edgeopsReplaceDollarMathWithPlaceholders(text, bucket, prefix) {
    prefix = prefix || 'EDGEOPSMDMATHINLINE';
    return String(text || '').replace(/(^|[^\\$])\$([^\n$]+?)\$/g, function (match, lead, expr) {
        var id = prefix + bucket.length;
        bucket.push(String(expr || '').trim());
        return lead + id;
    });
}

/** 提取行内公式占位：$...$、\\(...\\)，全角 ＄ 会归一为 $。 */
function edgeopsApplyInlineMathPlaceholders(text, bucket, prefix) {
    text = edgeopsNormalizeMathDelimiters(text);
    text = edgeopsReplaceDollarMathWithPlaceholders(text, bucket, prefix);
    return text.replace(/\\\(([\s\S]+?)\\\)/g, function (match, expr) {
        var id = prefix + bucket.length;
        bucket.push(String(expr || '').trim());
        return id;
    });
}

function edgeopsRenderMathHtml(source, displayMode) {
    var expr = String(source || '').trim();
    if (!expr) return '';
    if (typeof window !== 'undefined' && window.katex && typeof window.katex.renderToString === 'function') {
        try {
            var rendered = window.katex.renderToString(expr, {
                displayMode: !!displayMode,
                throwOnError: false,
                strict: false,
                trust: false,
                output: 'html'
            });
            if (displayMode) {
                return '<span class="chat-math-katex-block">' + rendered + '</span>';
            }
            return '<span class="chat-math-katex-inline">' + rendered + '</span>';
        } catch (e) {}
    }
    var cls = displayMode ? 'chat-math-block' : 'chat-math-inline';
    return '<span class="' + cls + '">' + edgeopsRenderBasicLatexFallback(expr) + '</span>';
}

function edgeopsRenderBasicLatexFallback(expr) {
    function readGroup(src, start) {
        if (src[start] !== '{') return null;
        var depth = 0;
        for (var i = start; i < src.length; i++) {
            if (src[i] === '{') depth++;
            else if (src[i] === '}') {
                depth--;
                if (depth === 0) return { value: src.slice(start + 1, i), end: i + 1 };
            }
        }
        return null;
    }
    var symbols = {
        '\\times': '×',
        '\\cdot': '·',
        '\\approx': '≈',
        '\\le': '≤',
        '\\ge': '≥',
        '\\neq': '≠',
        '\\pm': '±',
        '\\to': '→',
        '\\rightarrow': '→',
        '\\Rightarrow': '⇒',
        '\\Leftarrow': '⇐',
        '\\iff': '⇔',
        '\\Leftrightarrow': '⇔',
        '\\implies': '⇒',
        '\\forall': '∀',
        '\\exists': '∃',
        '\\infty': '∞',
        '\\left': '',
        '\\right': ''
    };
    function renderSegment(src) {
        src = String(src || '');
        var out = '';
        for (var i = 0; i < src.length;) {
            if (src.slice(i, i + 6) === '\\frac{') {
                var num = readGroup(src, i + 5);
                var den = num ? readGroup(src, num.end) : null;
                if (num && den) {
                    out += '<span class="math-frac"><span class="math-num">' + renderSegment(num.value) + '</span><span class="math-den">' + renderSegment(den.value) + '</span></span>';
                    i = den.end;
                    continue;
                }
            }
            if (src.slice(i, i + 6) === '\\sqrt{') {
                var rad = readGroup(src, i + 5);
                if (rad) {
                    out += '<span class="math-sqrt"><span class="math-radicand">' + renderSegment(rad.value) + '</span></span>';
                    i = rad.end;
                    continue;
                }
            }
            if (src.slice(i, i + 6) === '\\text{') {
                var txt = readGroup(src, i + 5);
                if (txt) {
                    out += '<span class="math-text">' + escapeHtmlForCode(txt.value) + '</span>';
                    i = txt.end;
                    continue;
                }
            }
            if (src.slice(i, i + 8) === '\\mathbf{') {
                var bold = readGroup(src, i + 7);
                if (bold) {
                    out += '<strong class="math-bold">' + renderSegment(bold.value) + '</strong>';
                    i = bold.end;
                    continue;
                }
            }
            if (src.slice(i, i + 8) === '\\mathrm{') {
                var roman = readGroup(src, i + 7);
                if (roman) {
                    out += '<span class="math-text">' + renderSegment(roman.value) + '</span>';
                    i = roman.end;
                    continue;
                }
            }
            if ((src[i] === '^' || src[i] === '_') && src[i + 1] === '{') {
                var grp = readGroup(src, i + 1);
                if (grp) {
                    out += (src[i] === '^' ? '<sup>' : '<sub>') + renderSegment(grp.value) + (src[i] === '^' ? '</sup>' : '</sub>');
                    i = grp.end;
                    continue;
                }
            }
            if (src[i] === '^' || src[i] === '_') {
                var m = src.slice(i + 1).match(/^[-+]?[A-Za-z0-9]+/);
                if (m) {
                    out += (src[i] === '^' ? '<sup>' : '<sub>') + renderSegment(m[0]) + (src[i] === '^' ? '</sup>' : '</sub>');
                    i += 1 + m[0].length;
                    continue;
                }
            }
            var matched = false;
            for (var key in symbols) {
                if (src.slice(i, i + key.length) === key) {
                    out += escapeHtmlForCode(symbols[key]);
                    i += key.length;
                    matched = true;
                    break;
                }
            }
            if (matched) continue;
            if (src[i] === '\\') {
                var spacing = src.slice(i, i + 2);
                if (spacing === '\\,' || spacing === '\\;' || spacing === '\\ ') {
                    out += ' ';
                    i += 2;
                    continue;
                }
            }
            out += escapeHtmlForCode(src[i]);
            i++;
        }
        return out;
    }
    return renderSegment(expr || '');
}

function edgeopsLooksLikeStandaloneLatexMath(line) {
    var s = String(line || '').trim();
    if (!s || s.length > 300) return false;
    if (/^(\||[-*+] |\d+\. |&gt;|<)/.test(s)) return false;
    if (!/\\(?:sqrt|frac|times|cdot|approx|le|ge|neq|pm|sum|int|alpha|beta|gamma|theta|lambda|mu|pi|sigma|omega)\b/.test(s)) return false;
    return /[=+\-*/^]|\\(?:approx|le|ge|neq)\b/.test(s);
}

function edgeopsNormalizeCodeLang(lang) {
    lang = String(lang || '').trim().toLowerCase();
    var map = {
        sh: 'bash',
        shell: 'bash',
        zsh: 'bash',
        ps1: 'powershell',
        py: 'python',
        js: 'javascript',
        ts: 'typescript',
        yml: 'yaml',
        docker: 'dockerfile',
        dockerfile: 'dockerfile',
        console: 'log',
        terminal: 'log',
        txt: 'text',
        text: 'text'
    };
    return map[lang] || lang || 'text';
}

function edgeopsHighlightEscapedCode(escaped, lang) {
    var out = String(escaped || '');
    lang = edgeopsNormalizeCodeLang(lang);
    if (lang === 'diff') {
        return out.split('\n').map(function(line) {
            var cls = line.indexOf('+') === 0 ? 'tok-diff-add'
                : (line.indexOf('-') === 0 ? 'tok-diff-del'
                : (line.indexOf('@@') === 0 ? 'tok-diff-meta' : ''));
            return cls ? '<span class="' + cls + '">' + line + '</span>' : line;
        }).join('\n');
    }
    if (lang === 'log') {
        return out.split('\n').map(function(line) {
            var cls = /\b(error|failed|fatal|exception)\b/i.test(line) ? 'tok-log-error'
                : (/\b(warn|warning)\b/i.test(line) ? 'tok-log-warn'
                : (/\b(success|ok|done|passed)\b/i.test(line) ? 'tok-log-success' : ''));
            return cls ? '<span class="' + cls + '">' + line + '</span>' : line;
        }).join('\n');
    }
    var tokens = [];
    function hold(cls, value) {
        var id = 'EDGEOPSCODETOK' + tokens.length;
        tokens.push('<span class="' + cls + '">' + value + '</span>');
        return id;
    }
    out = out.replace(/(&quot;[^&]*(?:&amp;.[^&]*)*&quot;|'[^'\n]*')/g, function(m) { return hold('tok-string', m); });
    out = out.replace(/(^|\n)(\s*(?:#|\/\/|--).*)/g, function(_, prefix, comment) {
        return prefix + hold('tok-comment', comment);
    });
    out = out.replace(/\b(true|false|null|None|nil)\b/g, function(m) { return hold('tok-literal', m); });
    out = out.replace(/\b(\d+(?:\.\d+)?)\b/g, function(m) { return hold('tok-number', m); });
    if (/^(python|javascript|typescript|bash|powershell|sql|json|yaml|dockerfile|css|html|xml|http)$/.test(lang)) {
        out = out.replace(/\b(async|await|class|def|function|return|if|else|elif|for|while|try|catch|except|finally|import|from|export|const|let|var|new|in|is|and|or|not|SELECT|FROM|WHERE|JOIN|LEFT|RIGHT|INSERT|UPDATE|DELETE|CREATE|ALTER|DROP|GROUP|ORDER|BY|LIMIT|GET|POST|PUT|PATCH|DELETE|HTTP|RUN|CMD|ENTRYPOINT|FROM|COPY|ADD|ENV|EXPOSE|WORKDIR)\b/g, '<span class="tok-keyword">$1</span>');
    }
    for (var i = 0; i < tokens.length; i++) {
        out = edgeopsReplaceIndexedPlaceholder(out, 'EDGEOPSCODETOK', i, tokens[i]);
    }
    return out;
}

function edgeopsRenderCodeBlockHtml(lang, code) {
    var normalized = edgeopsNormalizeCodeLang(lang);
    var raw = String(code || '');
    var escaped = escapeHtmlForCode(raw);
    var highlighted = edgeopsHighlightEscapedCode(escaped, normalized);
    var label = normalized === 'text' ? 'text' : normalized;
    if (normalized === 'tree') label = 'tree';
    var copyLabel = (typeof t === 'function') ? t('common.copy') : '复制';
    // encodeURIComponent 保留原文，复制时不经高亮 DOM 二次提取
    var dataCode = encodeURIComponent(raw);
    return ''
        + '<div class="chat-code-block-wrap" data-code-lang="' + escapeHtmlForCode(label) + '">'
        + '<div class="chat-code-toolbar">'
        + '<span class="chat-code-lang">' + escapeHtmlForCode(label) + '</span>'
        + '<button type="button" class="btn btn-xs chat-code-copy" data-code="' + dataCode + '" title="' + escapeHtmlForCode(copyLabel) + '">'
        + '<span class="chat-code-copy-icon" aria-hidden="true">⧉</span> '
        + '<span class="chat-code-copy-text">' + escapeHtmlForCode(copyLabel) + '</span>'
        + '</button>'
        + '</div>'
        + '<pre class="chat-code-block language-' + escapeHtmlForCode(normalized) + '"><code>' + highlighted + '</code></pre>'
        + '</div>';
}

function edgeopsCopyTextToClipboard(text, onOk) {
    var txt = String(text || '');
    function ok() {
        if (typeof onOk === 'function') onOk();
        else if (typeof showToast === 'function') {
            showToast((typeof t === 'function' ? t('toast.copied') : '已复制'));
        }
    }
    function fallback() {
        try {
            var ta = document.createElement('textarea');
            ta.value = txt;
            ta.setAttribute('readonly', 'readonly');
            ta.style.cssText = 'position:fixed;left:-9999px;top:0';
            document.body.appendChild(ta);
            ta.select();
            document.execCommand('copy');
            document.body.removeChild(ta);
            ok();
        } catch (e) {
            if (typeof showToast === 'function') showToast((typeof t === 'function' ? t('toast.copyFailed') : '复制失败'), 'error');
        }
    }
    if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(txt).then(ok).catch(fallback);
    } else {
        fallback();
    }
}

/** 聊天代码块「复制」按钮（事件委托，覆盖流式与历史重渲） */
function edgeopsBindChatCodeCopy() {
    if (document.documentElement.getAttribute('data-edgeops-code-copy') === '1') return;
    document.documentElement.setAttribute('data-edgeops-code-copy', '1');
    document.addEventListener('click', function(ev) {
        var btn = ev.target && ev.target.closest ? ev.target.closest('.chat-code-copy') : null;
        if (!btn) return;
        ev.preventDefault();
        ev.stopPropagation();
        var encoded = btn.getAttribute('data-code') || '';
        var raw = '';
        try { raw = decodeURIComponent(encoded); } catch (e1) { raw = encoded; }
        if (!raw) {
            var wrap = btn.closest('.chat-code-block-wrap');
            var codeEl = wrap && wrap.querySelector('pre code');
            raw = codeEl ? (codeEl.textContent || '') : '';
        }
        edgeopsCopyTextToClipboard(raw, function() {
            var labelEl = btn.querySelector('.chat-code-copy-text');
            var prev = labelEl ? labelEl.textContent : '';
            if (labelEl) labelEl.textContent = (typeof t === 'function' ? t('common.copied') : '已复制');
            btn.classList.add('is-copied');
            if (typeof showToast === 'function') {
                showToast((typeof t === 'function' ? t('toast.copied') : '已复制'));
            }
            setTimeout(function() {
                if (labelEl) labelEl.textContent = prev || ((typeof t === 'function') ? t('common.copy') : '复制');
                btn.classList.remove('is-copied');
            }, 1400);
        });
    });
}
if (typeof document !== 'undefined') {
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', edgeopsBindChatCodeCopy);
    } else {
        edgeopsBindChatCodeCopy();
    }
}

function edgeopsStripHtmlComments(text) {
    if (!text) return '';
    return String(text).replace(/<!--[\s\S]*?-->/g, '');
}

/** AI 友好：标题 # 后无空格时补空格（CommonMark 要求空格）。 */
function edgeopsNormalizeAtxHeadingSpace(text) {
    return String(text || '').replace(/^(\s{0,3})(#{1,6})([^\s#\n])/gm, '$1$2 $3');
}

/**
 * markdown-it 单例：html=false（防 XSS）；breaks/linkify 便于 AI 输出。
 * 链接/图片走附件 token 改写；围栏代码由外层提取后交给 edgeops 图表/高亮。
 */
function edgeopsGetMarkdownIt() {
    if (typeof window !== 'undefined' && window._edgeopsMarkdownIt) {
        return window._edgeopsMarkdownIt;
    }
    var factory = (typeof window !== 'undefined' && window.markdownit)
        || (typeof markdownit !== 'undefined' ? markdownit : null);
    if (typeof factory !== 'function') {
        return null;
    }
    var md = factory({
        html: false,
        xhtmlOut: false,
        breaks: true,
        linkify: true,
        typographer: false,
    });
    md.renderer.rules.link_open = function (tokens, idx, options, env, self) {
        var token = tokens[idx];
        var hrefIdx = token.attrIndex('href');
        if (hrefIdx >= 0) {
            token.attrs[hrefIdx][1] = edgeopsNormalizeSafeHref(token.attrs[hrefIdx][1]);
        }
        token.attrSet('target', '_blank');
        token.attrSet('rel', 'noopener noreferrer');
        return self.renderToken(tokens, idx, options);
    };
    md.renderer.rules.image = function (tokens, idx, options, env, self) {
        var token = tokens[idx];
        var srcIdx = token.attrIndex('src');
        if (srcIdx >= 0) {
            token.attrs[srcIdx][1] = edgeopsRewriteChatAttachmentUrl(token.attrs[srcIdx][1] || '');
        }
        token.attrSet('class', 'chat-md-inline-image chat-attachment-image-inline');
        token.attrSet('loading', 'lazy');
        return self.renderToken(tokens, idx, options);
    };
    if (typeof window !== 'undefined') {
        window._edgeopsMarkdownIt = md;
    }
    return md;
}

/**
 * 仅处理行内格式（用于表格单元格、列表项、标题等）。
 * 基础引擎：markdown-it.renderInline；保留安全 HTML / 行内公式 / 附件 URL 改写。
 */
function formatMarkdownInline(text) {
    if (!text) return '';
    var s = edgeopsStripHtmlComments(normalizeSafeHtmlTags(text));
    var inlineMath = [];
    s = edgeopsApplyInlineMathPlaceholders(s, inlineMath, 'EDGEOPSMDINLINEMATH');
    var safeHtmlTags = [];
    s = s.replace(new RegExp('<(' + EDGEOPS_SAFE_HTML_TAG_NAMES + ')(\\s[^>]*)?>([\\s\\S]*?)</\\1>', 'gi'), function (match, tagName, attrs, content) {
        var id = 'EDGEOPSMDSAFE' + safeHtmlTags.length;
        safeHtmlTags.push({ tag: tagName.toLowerCase(), attrs: (attrs || '').trim(), content: content });
        return id;
    });
    s = s.replace(/<br\s*\/?>/gi, function () {
        var id = 'EDGEOPSMDSAFE' + safeHtmlTags.length;
        safeHtmlTags.push({ tag: 'br', attrs: '', content: null });
        return id;
    });
    // AI 友好：~~删除线~~（CommonMark 无，转成安全占位）
    s = s.replace(/~~([^~\n]+?)~~/g, function (_, inner) {
        var id = 'EDGEOPSMDSAFE' + safeHtmlTags.length;
        safeHtmlTags.push({ tag: 'del', attrs: '', content: inner });
        return id;
    });

    var md = edgeopsGetMarkdownIt();
    if (md) {
        try {
            s = md.renderInline(s);
        } catch (e) {
            s = escapeHtmlForCode(s);
        }
    } else {
        s = escapeHtmlForCode(s);
    }

    for (var i = 0; i < safeHtmlTags.length; i++) {
        var item = safeHtmlTags[i];
        if (item.content === null) {
            s = edgeopsReplaceIndexedPlaceholder(s, 'EDGEOPSMDSAFE', i, '<br>');
        } else if (item.tag === 'del') {
            s = edgeopsReplaceIndexedPlaceholder(s, 'EDGEOPSMDSAFE', i, '<del>' + escapeHtmlForCode(item.content) + '</del>');
        } else {
            var inner = edgeopsSanitizeSafeInlineHtml(item.content);
            if (item.tag === 'a' && item.attrs) {
                var hrefMatch = item.attrs.match(/href\s*=\s*["']([^"']+)["']/i);
                var href = hrefMatch ? edgeopsNormalizeSafeHref(hrefMatch[1]) : '#';
                s = edgeopsReplaceIndexedPlaceholder(s, 'EDGEOPSMDSAFE', i, '<a href="' + escapeHtmlForCode(href) + '" target="_blank" rel="noopener noreferrer">' + inner + '</a>');
            } else {
                s = edgeopsReplaceIndexedPlaceholder(s, 'EDGEOPSMDSAFE', i, edgeopsBuildSafeInlineTag(item.tag, item.attrs, inner));
            }
        }
    }
    for (var m = 0; m < inlineMath.length; m++) {
        s = edgeopsReplaceIndexedPlaceholder(s, 'EDGEOPSMDINLINEMATH', m, edgeopsRenderMathHtml(inlineMath[m], false));
    }
    s = s.replace(/\\iff\b/g, '⇔')
        .replace(/\\Leftrightarrow\b/g, '⇔')
        .replace(/\\Rightarrow\b/g, '⇒')
        .replace(/\\Leftarrow\b/g, '⇐')
        .replace(/\\le\b/g, '≤')
        .replace(/\\ge\b/g, '≥')
        .replace(/\\neq\b/g, '≠');
    return s;
}

function edgeopsIsMarkdownTableSeparatorLine(line) {
    var t = String(line || '').trim();
    if (!t || t.indexOf('-') === -1) return false;
    return /^[|\s:\-]+$/.test(t);
}

/** 流式输出中分隔行尚未收齐（如 `|---`）时也识别为表格尾部。 */
function edgeopsIsPartialMarkdownTableSeparatorLine(line) {
    var t = String(line || '').trim();
    if (!t || t.indexOf('-') === -1) return false;
    if (edgeopsIsMarkdownTableSeparatorLine(t)) return true;
    return /^[\|\s:\-]+$/.test(t);
}

function edgeopsIsMarkdownTableRowLine(line) {
    var t = String(line || '').trim();
    if (!t || t.indexOf('|') === -1) return false;
    return /^\|.+\|?$/.test(t) || /^\|/.test(t);
}

function edgeopsParseMarkdownTableRowCells(row) {
    var t = String(row || '').trim();
    if (!t) return [];
    var parts = t.split('|');
    if (parts.length && parts[0].trim() === '') parts.shift();
    if (parts.length && parts[parts.length - 1].trim() === '') parts.pop();
    return parts.map(function(c) { return c.trim(); });
}

function edgeopsBuildChatMarkdownTableHtml(headerLine, rowsText) {
    var tableHtml = '<div class="chat-md-table-wrap"><table class="chat-md-table"><thead><tr>';
    var headerCells = edgeopsParseMarkdownTableRowCells(headerLine);
    headerCells.forEach(function(cell) {
        tableHtml += '<th>' + formatMarkdownInline(cell) + '</th>';
    });
    tableHtml += '</tr></thead><tbody>';
    var rowLines = String(rowsText || '').trim() ? String(rowsText).trim().split('\n') : [];
    rowLines.forEach(function(row) {
        row = String(row || '').trim();
        if (!row || row.indexOf('|') === -1) return;
        var cells = edgeopsParseMarkdownTableRowCells(row);
        if (!cells.length) return;
        tableHtml += '<tr>';
        cells.forEach(function(cell) {
            tableHtml += '<td>' + formatMarkdownInline(cell) + '</td>';
        });
        tableHtml += '</tr>';
    });
    tableHtml += '</tbody></table></div>';
    return tableHtml;
}

/** 完整表格正则吃掉已生成行后，末尾未闭合的数据行并入上一张表，避免「表格 + 裸管道行」交替闪动。 */
function edgeopsAppendOrphanRowToLastMarkdownTable(html, tables) {
    if (!tables || !tables.length || html == null) return html;
    var m = String(html).match(/\n(\|[^\n]+)\s*$/);
    if (!m) return html;
    var row = m[1].trim();
    if (!edgeopsIsMarkdownTableRowLine(row) || edgeopsIsMarkdownTableSeparatorLine(row)) return html;
    var last = tables[tables.length - 1];
    last.rows = (last.rows ? last.rows + '\n' : '') + row;
    return String(html).slice(0, html.length - m[0].length);
}

/** 用与流式提交相同的扫描器提取「已完整」的 Markdown 表格，避免冻结段里表格回退成管道符原文。 */
function edgeopsExtractCompleteMarkdownTablesIterative(source) {
    var tables = [];
    var markers = [];
    if (!source) return { html: '', tables: tables };
    var pos = 0;
    var len = source.length;
    while (pos < len) {
        var atLine = pos === 0 || source[pos - 1] === '\n';
        if (!atLine) {
            var skipNl = source.indexOf('\n', pos);
            if (skipNl === -1) break;
            pos = skipNl + 1;
            continue;
        }
        var scan = edgeopsScanMarkdownTableFrom(source, pos);
        if (scan && scan.complete) {
            var block = source.slice(pos, scan.end);
            var lines = block.split('\n');
            var headerLine = String(lines[0] || '').trim();
            var rowLines = lines.slice(2).join('\n');
            var tid = 'EDGEOPSMDTABLE' + tables.length;
            tables.push({ header: headerLine, rows: rowLines });
            markers.push({ start: pos, end: scan.end, id: tid });
            pos = scan.end;
            continue;
        }
        pos++;
    }
    if (!markers.length) return { html: source, tables: tables };
    markers.sort(function(a, b) { return a.start - b.start; });
    var out = '';
    var cur = 0;
    for (var m = 0; m < markers.length; m++) {
        out += source.slice(cur, markers[m].start) + markers[m].id;
        cur = markers[m].end;
    }
    out += source.slice(cur);
    return { html: out, tables: tables };
}

/** 流式冻结段渲染：与 tail 表格共用扫描/build，避免 formatMarkdown 正则与流式路径不一致。 */
function edgeopsFormatStreamCommittedSegmentHtml(text) {
    text = text == null ? '' : String(text);
    if (!text.trim()) return '';
    var pipeAt = text.search(/\|/);
    if (pipeAt < 0) return formatMarkdown(text);
    var lineStart = pipeAt;
    while (lineStart > 0 && text[lineStart - 1] !== '\n') lineStart--;
    var scan = edgeopsScanMarkdownTableFrom(text, lineStart);
    if (!scan || !scan.complete) return formatMarkdown(text);
    var trimmedEnd = text.replace(/\s+$/, '').length;
    if (scan.end < trimmedEnd) return formatMarkdown(text);
    var prefix = text.slice(0, lineStart).replace(/\s+$/, '');
    var block = text.slice(lineStart, scan.end);
    var lines = block.split('\n');
    var tableHtml = edgeopsBuildChatMarkdownTableHtml(String(lines[0] || '').trim(), lines.slice(2).join('\n'));
    var suffix = text.slice(scan.end);
    var suffixHtml = suffix.trim() ? formatMarkdown(suffix) : '';
    var prefixHtml = prefix ? formatMarkdown(prefix + '\n') : '';
    return prefixHtml + tableHtml + suffixHtml;
}

/** 流式输出时文末未写完的表格：有表头+分隔行即渲染，避免在「占位符表格」与「转义管道符文本」间闪动。 */
function edgeopsExtractTrailingIncompleteMarkdownTable(text) {
    if (!text) return null;
    var lines = text.split('\n');
    var end = lines.length - 1;
    while (end >= 0 && !String(lines[end] || '').trim()) end--;
    if (end < 1) return null;
    var start = end;
    while (start >= 0) {
        var trimmed = String(lines[start] || '').trim();
        if (!trimmed) break;
        if (edgeopsIsMarkdownTableRowLine(trimmed) || edgeopsIsMarkdownTableSeparatorLine(trimmed) || edgeopsIsPartialMarkdownTableSeparatorLine(trimmed)) {
            start--;
            continue;
        }
        break;
    }
    start++;
    var block = lines.slice(start, end + 1);
    if (block.length < 2) return null;
    var headerLine = String(block[0] || '').trim();
    var sepLine = String(block[1] || '').trim();
    var sepOk = edgeopsIsMarkdownTableSeparatorLine(sepLine) || edgeopsIsPartialMarkdownTableSeparatorLine(sepLine);
    if (!edgeopsIsMarkdownTableRowLine(headerLine) || !sepOk) return null;
    var rowLines = block.slice(2);
    var prefix = lines.slice(0, start).join('\n');
    var suffixNewline = (text.endsWith('\n') ? '\n' : '');
    return {
        prefix: prefix,
        header: headerLine,
        rows: rowLines.join('\n'),
        placeholder: 'EDGEOPSMDTABLE',
        consumedLength: (prefix ? prefix.length + 1 : 0) + block.join('\n').length + suffixNewline.length
    };
}

/**
 * Markdown 转 HTML。
 * 基础引擎：markdown-it（本地 markdown-it.min.js）。
 * 仍由本函数预处理：HTML 注释剥离、围栏代码/图表、表格（聊天样式）、公式、Callout、安全 HTML。
 */
function markdownToHtml(text) {
    if (!text) return '';
    var html = edgeopsStripHtmlComments(String(text));
    html = edgeopsCompactMarkdownBlankLines(normalizeSafeHtmlTags(html));
    html = edgeopsNormalizeAtxHeadingSpace(html);

    var codeBlocks = [];
    html = html.replace(/\x60\x60\x60([^\n`]*)\n?([\s\S]*?)\x60\x60\x60/g, function (match, lang, code) {
        var id = 'EDGEOPSMDCODEBLOCK' + codeBlocks.length;
        var info = String(lang || '').trim();
        codeBlocks.push({ lang: info ? info.split(/\s+/)[0] : '', code: code.trim() });
        return '\n\n' + id + '\n\n';
    });

    var mathBlocks = [];
    html = edgeopsNormalizeMathDelimiters(html);
    html = html.replace(/\$\$([\s\S]+?)\$\$/g, function (match, expr) {
        var id = 'EDGEOPSMDMATHBLOCK' + mathBlocks.length;
        mathBlocks.push(String(expr || '').trim());
        return '\n\n' + id + '\n\n';
    });
    html = html.replace(/\\\[([\s\S]+?)\\\]/g, function (match, expr) {
        var id = 'EDGEOPSMDMATHBLOCK' + mathBlocks.length;
        mathBlocks.push(String(expr || '').trim());
        return '\n\n' + id + '\n\n';
    });
    html = html.split('\n').map(function(line) {
        if (!edgeopsLooksLikeStandaloneLatexMath(line)) return line;
        var id = 'EDGEOPSMDMATHBLOCK' + mathBlocks.length;
        mathBlocks.push(line);
        return id;
    }).join('\n');

    var inlineMath = [];
    html = edgeopsApplyInlineMathPlaceholders(html, inlineMath, 'EDGEOPSMDMATHINLINE');

    var tableExtract = edgeopsExtractCompleteMarkdownTablesIterative(html);
    html = tableExtract.html;
    var tables = tableExtract.tables;
    html = edgeopsAppendOrphanRowToLastMarkdownTable(html, tables);

    var trailingTbl = edgeopsExtractTrailingIncompleteMarkdownTable(html);
    if (trailingTbl) {
        var tid = 'EDGEOPSMDTABLE' + tables.length;
        tables.push({ header: trailingTbl.header, rows: trailingTbl.rows });
        var tailPrefix = trailingTbl.prefix;
        html = (tailPrefix ? tailPrefix + '\n' : '') + tid;
    }

    var callouts = [];
    html = html.replace(/(?:^|\n)>\s*\[!(NOTE|TIP|IMPORTANT|WARNING|ERROR)\]\s*([^\n]*)(\n(?:>[^\n]*)*)?/gi, function(match, kind, title, body) {
        var type = String(kind || 'NOTE').toLowerCase();
        var bodyHtml = '';
        if (body) {
            bodyHtml = String(body).split('\n').map(function(line) {
                return line.replace(/^>\s?/, '').trim();
            }).filter(Boolean).join('<br>');
        }
        var titleHtml = formatMarkdownInline((title || '').trim() || kind);
        if (bodyHtml) bodyHtml = formatMarkdownInline(bodyHtml);
        var block = '<div class="chat-callout chat-callout-' + escapeHtmlForCode(type) + '">'
            + '<div class="chat-callout-title">' + titleHtml + '</div>'
            + (bodyHtml ? '<div class="chat-callout-body">' + bodyHtml + '</div>' : '')
            + '</div>';
        var id = 'EDGEOPSMDCALLOUT' + callouts.length;
        callouts.push(block);
        return '\n\n' + id + '\n\n';
    });

    var strikeTags = [];
    html = html.replace(/~~([^~\n]+?)~~/g, function (_, inner) {
        var id = 'EDGEOPSMDSTRIKE' + strikeTags.length;
        strikeTags.push(inner);
        return id;
    });

    var safeHtmlTags = [];
    html = html.replace(new RegExp('<(' + EDGEOPS_SAFE_HTML_TAG_NAMES + ')(\\s[^>]*)?>([\\s\\S]*?)</\\1>', 'gi'), function (match, tagName, attrs, content) {
        var id = 'EDGEOPSMDSAFEHTML' + safeHtmlTags.length;
        safeHtmlTags.push({ tag: tagName.toLowerCase(), attrs: (attrs || '').trim(), content: content });
        return id;
    });
    html = html.replace(/<br\s*\/?>/gi, function () {
        var id = 'EDGEOPSMDSAFEHTML' + safeHtmlTags.length;
        safeHtmlTags.push({ tag: 'br', content: null });
        return id;
    });

    var md = edgeopsGetMarkdownIt();
    if (md) {
        try {
            html = md.render(html);
        } catch (e) {
            html = '<p>' + escapeHtmlForCode(html) + '</p>';
        }
    } else {
        html = '<p>' + escapeHtmlForCode(html).replace(/\n/g, '<br>') + '</p>';
    }

    html = html.replace(
        /<p>\s*(EDGEOPSMD(?:CODEBLOCK|TABLE|MATHBLOCK|CALLOUT|SAFEHTML|STRIKE|MATHINLINE)\d+)\s*<\/p>/g,
        '$1'
    );

    for (var si = 0; si < strikeTags.length; si++) {
        html = edgeopsReplaceIndexedPlaceholder(
            html,
            'EDGEOPSMDSTRIKE',
            si,
            '<del>' + escapeHtmlForCode(strikeTags[si]) + '</del>'
        );
    }

    for (var i = 0; i < safeHtmlTags.length; i++) {
        var item = safeHtmlTags[i];
        if (item.content === null) {
            html = edgeopsReplaceIndexedPlaceholder(html, 'EDGEOPSMDSAFEHTML', i, '<br>');
        } else {
            var inner = edgeopsSanitizeSafeInlineHtml(item.content);
            if (item.tag === 'a' && item.attrs) {
                var hrefMatch = item.attrs.match(/href\s*=\s*["']([^"']+)["']/i);
                var href = hrefMatch ? edgeopsNormalizeSafeHref(hrefMatch[1]) : '#';
                html = edgeopsReplaceIndexedPlaceholder(html, 'EDGEOPSMDSAFEHTML', i, '<a href="' + escapeHtmlForCode(href) + '" target="_blank" rel="noopener noreferrer">' + inner + '</a>');
            } else {
                html = edgeopsReplaceIndexedPlaceholder(html, 'EDGEOPSMDSAFEHTML', i, edgeopsBuildSafeInlineTag(item.tag, item.attrs, inner));
            }
        }
    }

    for (var c = 0; c < callouts.length; c++) {
        html = edgeopsReplaceIndexedPlaceholder(html, 'EDGEOPSMDCALLOUT', c, callouts[c]);
    }

    for (var k = 0; k < codeBlocks.length; k++) {
        var block = codeBlocks[k];
        var diagramType = edgeopsNormalizeDiagramLang(block.lang);
        var pre = diagramType
            ? edgeopsBuildDiagramBlockHtml(diagramType, block.code, block.lang)
            : edgeopsRenderCodeBlockHtml(block.lang, block.code);
        html = edgeopsReplaceIndexedPlaceholder(html, 'EDGEOPSMDCODEBLOCK', k, pre);
    }

    for (var mb = 0; mb < mathBlocks.length; mb++) {
        html = edgeopsReplaceIndexedPlaceholder(html, 'EDGEOPSMDMATHBLOCK', mb, '<div class="chat-math-block-wrap">' + edgeopsRenderMathHtml(mathBlocks[mb], true) + '</div>');
    }

    for (var t = tables.length - 1; t >= 0; t--) {
        var tb = tables[t];
        html = edgeopsReplaceIndexedPlaceholder(html, 'EDGEOPSMDTABLE', t, edgeopsBuildChatMarkdownTableHtml(tb.header, tb.rows));
    }

    for (var mi = 0; mi < inlineMath.length; mi++) {
        html = edgeopsReplaceIndexedPlaceholder(html, 'EDGEOPSMDMATHINLINE', mi, edgeopsRenderMathHtml(inlineMath[mi], false));
    }

    return html;
}

function formatMarkdown(text) {
    if (!text) return '';
    return markdownToHtml(text);
}

/** 剥离模型误写入正文/推理的 XML 工具调用标签（Qwen 等）。 */
function edgeopsSanitizeLeakedToolMarkup(text) {
    if (text == null || text === '') return '';
    var s = String(text);
    s = s.replace(/<tool_call>[\s\S]*?<\/tool_call>/gi, '');
    s = s.replace(/<\/?(?:tool_call|function|parameter|arguments|invoke|tool)\b[^>]*>/gi, '');
    s = s.replace(/(?:<\/?(?:tool_call|function|parameter|arguments)\b[^>]*>)+\s*$/gi, '');
    return s;
}

/** 文末是否为正在生成的 Markdown 表格（用于流式增量渲染）。 */
function edgeopsSplitTextAndTailMarkdownTable(text) {
    var trailing = edgeopsExtractTrailingIncompleteMarkdownTable(text || '');
    if (trailing) {
        return {
            hasTailTable: true,
            before: trailing.prefix || '',
            header: trailing.header,
            rows: trailing.rows || ''
        };
    }
    return { hasTailTable: false, before: text || '' };
}

function edgeopsFindCodeFenceCloseEnd(text, openStart) {
    var idx = openStart + 3;
    var nl = text.indexOf('\n', idx);
    if (nl === -1) return null;
    idx = nl + 1;
    while (idx <= text.length) {
        var lineEnd = text.indexOf('\n', idx);
        if (lineEnd === -1) lineEnd = text.length;
        var line = text.slice(idx, lineEnd).replace(/\r$/, '').trim();
        if (line === '```') {
            return lineEnd < text.length ? lineEnd + 1 : lineEnd;
        }
        if (lineEnd >= text.length) return null;
        idx = lineEnd + 1;
    }
    return null;
}

/** 从 start 起若存在已结束的 Markdown 表格，返回 { end, complete }；complete 表示表后已有空行或非表行。 */
function edgeopsScanMarkdownTableFrom(text, start) {
    if (start > 0 && text[start - 1] !== '\n') return null;
    var rest = text.slice(start);
    if (!rest) return null;
    var lines = rest.split('\n');
    var header = String(lines[0] || '').trim();
    if (!edgeopsIsMarkdownTableRowLine(header)) return null;
    var sep = lines.length > 1 ? String(lines[1] || '').trim() : '';
    if (!sep || (!edgeopsIsMarkdownTableSeparatorLine(sep) && !edgeopsIsPartialMarkdownTableSeparatorLine(sep))) return null;
    var endLine = 2;
    while (endLine < lines.length) {
        var t = String(lines[endLine] || '').trim();
        if (!t) break;
        if (!edgeopsIsMarkdownTableRowLine(t) && !edgeopsIsMarkdownTableSeparatorLine(t)) break;
        endLine++;
    }
    var complete = endLine < lines.length;
    if (!complete && endLine >= 2 && endLine === lines.length && edgeopsIsMarkdownTableSeparatorLine(String(lines[1] || '').trim())) {
        complete = true;
    }
    var consumed = 0;
    for (var i = 0; i < endLine; i++) {
        consumed += lines[i].length + (i < endLine - 1 ? 1 : 0);
    }
    if (endLine < lines.length && lines[endLine] === '' && complete) consumed += 1;
    return { end: start + consumed, complete: complete };
}

function edgeopsFindNextSpecialBlockStart(text, from) {
    var fence = text.indexOf('```', from);
    var tableAt = text.length;
    var pos = from;
    if (pos > 0 && text[pos - 1] !== '\n') {
        var jumpNl = text.indexOf('\n', from);
        pos = jumpNl === -1 ? text.length : jumpNl + 1;
    }
    while (pos < text.length) {
        var scan = edgeopsScanMarkdownTableFrom(text, pos);
        if (scan) {
            tableAt = pos;
            break;
        }
        var nl = text.indexOf('\n', pos);
        if (nl === -1) break;
        pos = nl + 1;
    }
    if (fence < 0) return tableAt;
    return Math.min(fence, tableAt);
}

function edgeopsComputeStreamCommittedEnd(text) {
    var i = 0;
    var len = text.length;
    while (i < len) {
        if (text.slice(i, i + 3) === '```') {
            var closeEnd = edgeopsFindCodeFenceCloseEnd(text, i);
            if (closeEnd == null) return i;
            i = closeEnd;
            continue;
        }
        var atLine = i === 0 || text[i - 1] === '\n';
        if (atLine) {
            var tbl = edgeopsScanMarkdownTableFrom(text, i);
            if (tbl) {
                if (!tbl.complete) return i;
                i = tbl.end;
                continue;
            }
        }
        var nextSpecial = edgeopsFindNextSpecialBlockStart(text, i);
        var dbl = text.indexOf('\n\n', i);
        if (dbl >= 0 && dbl < nextSpecial) {
            i = dbl + 2;
            continue;
        }
        return i;
    }
    return len;
}

function edgeopsParseCommittedStreamSegments(text) {
    var segments = [];
    var i = 0;
    var len = text.length;
    while (i < len) {
        if (text.slice(i, i + 3) === '```') {
            var closeEnd = edgeopsFindCodeFenceCloseEnd(text, i);
            if (closeEnd == null) break;
            segments.push({ type: 'md', text: text.slice(i, closeEnd) });
            i = closeEnd;
            continue;
        }
        var atLine = i === 0 || text[i - 1] === '\n';
        if (atLine) {
            var tbl = edgeopsScanMarkdownTableFrom(text, i);
            if (tbl && tbl.complete) {
                segments.push({ type: 'md', text: text.slice(i, tbl.end) });
                i = tbl.end;
                continue;
            }
        }
        var nextSpecial = edgeopsFindNextSpecialBlockStart(text, i);
        var dbl = text.indexOf('\n\n', i);
        if (dbl >= 0 && dbl < nextSpecial) {
            var chunk = text.slice(i, dbl + 2);
            if (chunk.trim()) segments.push({ type: 'md', text: chunk });
            i = dbl + 2;
            continue;
        }
        break;
    }
    if (i < len) {
        var rest = text.slice(i);
        if (rest.trim() && edgeopsComputeStreamCommittedEnd(rest) === rest.length) {
            segments.push({ type: 'md', text: rest });
        }
    }
    return segments;
}

function edgeopsProseItemSummary(text, index) {
    text = text == null ? '' : String(text);
    var lines = text.split(/\r?\n/);
    var first = '';
    for (var li = 0; li < lines.length; li++) {
        var line = String(lines[li] || '').trim();
        if (line) { first = line; break; }
    }
    first = first.replace(/^#+\s*/, '').replace(/\*\*/g, '').replace(/`/g, '').trim();
    if (first.length > 56) first = first.slice(0, 53) + '\u2026';
    var label = (typeof t === 'function')
        ? t('hostAi.proseItemLabel', { n: (index | 0) + 1 })
        : ('#' + ((index | 0) + 1));
    return first ? (label + ' \u00b7 ' + first) : label;
}

function edgeopsWrapProseStreamItem(segEl, segText, index) {
    var item = document.createElement('div');
    item.className = 'ai-reply-prose-item';
    var head = document.createElement('button');
    head.type = 'button';
    head.className = 'ai-reply-prose-item-head';
    head.innerHTML = '<span class="ai-reply-prose-item-chevron">\u25bc</span>'
        + '<span class="ai-reply-prose-item-title">' + esc(edgeopsProseItemSummary(segText, index)) + '</span>';
    var body = document.createElement('div');
    body.className = 'ai-reply-prose-item-body';
    body.appendChild(segEl);
    if (typeof edgeopsBindProseItemToggle === 'function') {
        edgeopsBindProseItemToggle(head, body, false);
    } else {
        item.appendChild(head);
        item.appendChild(body);
        return item;
    }
    item.appendChild(head);
    item.appendChild(body);
    return item;
}

function edgeopsEnsureStreamTailLiveItem(mountEl) {
    var tail = mountEl && mountEl.querySelector('[data-edgeops-stream-tail]');
    if (!tail) return null;
    if (tail.closest('.ai-reply-prose-item-live')) return tail.closest('.ai-reply-prose-item-live');
    var item = document.createElement('div');
    item.className = 'ai-reply-prose-item ai-reply-prose-item-live';
    var head = document.createElement('div');
    head.className = 'ai-reply-prose-item-head';
    head.textContent = (typeof t === 'function') ? t('hostAi.proseItemLive') : '\u2026';
    var body = document.createElement('div');
    body.className = 'ai-reply-prose-item-body';
    mountEl.insertBefore(item, tail);
    body.appendChild(tail);
    item.appendChild(head);
    item.appendChild(body);
    return item;
}

function edgeopsEnsureStreamIncrementalDom(mountEl) {
    if (mountEl._edgeopsIncrementalStream) return;
    mountEl._edgeopsIncrementalStream = true;
    mountEl._edgeopsStreamSegmentCount = 0;
    mountEl._edgeopsStreamCommittedLen = 0;
    mountEl.classList.remove('ai-reply-stream-plain');
    mountEl.classList.add('ai-reply-stream-incremental');
    mountEl.textContent = '';
    var tail = document.createElement('div');
    tail.className = 'edgeops-stream-tail';
    tail.setAttribute('data-edgeops-stream-tail', '1');
    var tailPlain = document.createElement('div');
    tailPlain.className = 'edgeops-stream-tail-plain ai-reply-stream-plain';
    tailPlain.setAttribute('data-edgeops-stream-tail-plain', '1');
    var tailTable = document.createElement('div');
    tailTable.setAttribute('data-edgeops-stream-table', '1');
    tail.appendChild(tailPlain);
    tail.appendChild(tailTable);
    mountEl.appendChild(tail);
    mountEl._edgeopsStreamTailPlain = tailPlain;
    mountEl._edgeopsStreamTailTable = tailTable;
    if (mountEl.closest('.ai-reply-prose-panel')) edgeopsEnsureStreamTailLiveItem(mountEl);
}

function edgeopsUpdatePlainTail(tailEl, fullPlain) {
    if (!tailEl) return;
    fullPlain = fullPlain == null ? '' : String(fullPlain);
    var prev = tailEl._edgeopsPlainLen || 0;
    var cached = tailEl._edgeopsPlainCached || '';
    if (fullPlain.length >= prev && fullPlain.slice(0, prev) === cached) {
        if (fullPlain.length > prev) tailEl.append(document.createTextNode(fullPlain.slice(prev)));
    } else {
        tailEl.textContent = fullPlain;
    }
    tailEl._edgeopsPlainLen = fullPlain.length;
    tailEl._edgeopsPlainCached = fullPlain;
    tailEl.classList.add('ai-reply-stream-plain');
    tailEl._edgeopsMdPrefixKey = '';
}

/** 将 tail 中已闭合的前缀（含完整表格/段落）并入 committed，避免留在 tailPlain 里以管道符原文显示。 */
function edgeopsPromoteCommittableTailPrefix(committedText, tail) {
    committedText = committedText == null ? '' : String(committedText);
    tail = tail == null ? '' : String(tail);
    var totalPromoted = 0;
    while (tail) {
        var split = edgeopsSplitTextAndTailMarkdownTable(tail);
        if (split.hasTailTable && split.before && split.before.trim()) {
            var plen = split.before.length;
            if (plen < tail.length && tail.charAt(plen) === '\n') plen += 1;
            committedText += tail.slice(0, plen);
            tail = tail.slice(plen);
            totalPromoted += plen;
            continue;
        }
        var cEnd = edgeopsComputeStreamCommittedEnd(tail);
        if (!cEnd) break;
        committedText += tail.slice(0, cEnd);
        tail = tail.slice(cEnd);
        totalPromoted += cEnd;
    }
    return { committedText: committedText, tail: tail, totalPromoted: totalPromoted };
}

/** tail 中「正在生成、尚未闭合」的 Markdown 片段：已可提交部分走 formatMarkdown，其余逐字追加。 */
function edgeopsUpdateHybridMarkdownTail(tailEl, fullText, hydrateRoot) {
    if (!tailEl) return;
    fullText = fullText == null ? '' : String(fullText);
    if (!fullText) {
        tailEl.innerHTML = '';
        tailEl._edgeopsHybridCommittedEnd = 0;
        tailEl._edgeopsHybridRestLen = 0;
        tailEl._edgeopsHybridRestCached = '';
        tailEl._edgeopsPlainLen = 0;
        tailEl._edgeopsPlainCached = '';
        tailEl._edgeopsMdPrefixKey = '';
        return;
    }
    var committedEnd = edgeopsComputeStreamCommittedEnd(fullText);
    var rest = fullText.slice(committedEnd);
    var prevCommitted = tailEl._edgeopsHybridCommittedEnd || 0;
    if (committedEnd !== prevCommitted) {
        tailEl._edgeopsHybridCommittedEnd = committedEnd;
        tailEl._edgeopsMdPrefixKey = '';
        var safe = fullText.slice(0, committedEnd);
        var html = safe.trim() ? formatMarkdown(safe) : '';
        if (rest) {
            html += '<span class="edgeops-stream-incomplete-chunk ai-reply-stream-plain"></span>';
        }
        tailEl.innerHTML = html;
        tailEl.classList.toggle('ai-reply-stream-plain', !safe.trim() && !!rest);
        tailEl._edgeopsHybridRestLen = 0;
        tailEl._edgeopsHybridRestCached = '';
        if (hydrateRoot && safe.trim() && typeof edgeopsHydrateChatDiagrams === 'function') {
            edgeopsHydrateChatDiagrams(tailEl);
        }
    }
    if (rest) {
        var span = tailEl.querySelector('.edgeops-stream-incomplete-chunk');
        if (!span) {
            span = document.createElement('span');
            span.className = 'edgeops-stream-incomplete-chunk ai-reply-stream-plain';
            tailEl.appendChild(span);
        }
        var prevRest = tailEl._edgeopsHybridRestCached || '';
        var prevRestLen = tailEl._edgeopsHybridRestLen || 0;
        if (rest.length >= prevRestLen && rest.slice(0, prevRestLen) === prevRest) {
            if (rest.length > prevRestLen) span.append(document.createTextNode(rest.slice(prevRestLen)));
        } else {
            span.textContent = rest;
        }
        tailEl._edgeopsHybridRestLen = rest.length;
        tailEl._edgeopsHybridRestCached = rest;
    } else {
        var staleSpan = tailEl.querySelector('.edgeops-stream-incomplete-chunk');
        if (staleSpan) staleSpan.remove();
        tailEl._edgeopsHybridRestLen = 0;
        tailEl._edgeopsHybridRestCached = '';
    }
    tailEl._edgeopsPlainLen = fullText.length;
    tailEl._edgeopsPlainCached = fullText;
}

/** tail 表格前的已闭合 Markdown（含完整表格）渲染为 HTML，避免仅显示管道符原文。 */
function edgeopsUpdateMarkdownTailPrefix(tailEl, beforeText, hydrateRoot) {
    if (!tailEl) return;
    beforeText = beforeText == null ? '' : String(beforeText);
    if (tailEl._edgeopsMdPrefixKey === beforeText) return;
    tailEl._edgeopsMdPrefixKey = beforeText;
    tailEl._edgeopsHybridKey = '';
    tailEl._edgeopsPlainLen = 0;
    tailEl._edgeopsPlainCached = '';
    tailEl.innerHTML = beforeText.trim() ? formatMarkdown(beforeText) : '';
    tailEl.classList.remove('ai-reply-stream-plain');
    if (hydrateRoot && beforeText.trim() && typeof edgeopsHydrateChatDiagrams === 'function') {
        edgeopsHydrateChatDiagrams(tailEl);
    }
}

function edgeopsUpdateStreamTailTable(tableHost, header, rows) {
    if (!tableHost) return;
    var tableKey = header + '\n---\n' + (rows || '');
    if (tableHost._edgeopsStreamKey === tableKey) return;
    var existingTbody = tableHost.querySelector('table.chat-md-table tbody');
    if (existingTbody && tableHost._edgeopsStreamHeader === header) {
        var tmp = document.createElement('div');
        tmp.innerHTML = edgeopsBuildChatMarkdownTableHtml(header, rows);
        var newTbody = tmp.querySelector('tbody');
        if (newTbody) {
            existingTbody.innerHTML = newTbody.innerHTML;
            tableHost._edgeopsStreamKey = tableKey;
            tableHost._edgeopsStreamHeader = header;
            tableHost.style.display = '';
            return;
        }
    }
    tableHost.innerHTML = edgeopsBuildChatMarkdownTableHtml(header, rows);
    tableHost._edgeopsStreamHeader = header;
    tableHost._edgeopsStreamKey = tableKey;
    tableHost.style.display = '';
}

/**
 * 流式分块渲染：闭合的代码块/表格/段落立即 formatMarkdown 并冻结 DOM；
 * 尾部未完成块用纯文本追加或仅更新表格 tbody，避免逐字全量重绘闪动。
 * @param {HTMLElement} mountEl
 * @param {string} fullText
 * @param {HTMLElement} [hydrateRoot] 新插入图表块后调用 edgeopsHydrateChatDiagrams
 */
function edgeopsRenderStreamIncremental(mountEl, fullText, hydrateRoot) {
    if (!mountEl) return;
    fullText = fullText == null ? '' : String(fullText);
    edgeopsEnsureStreamIncrementalDom(mountEl);

    var committedEnd = edgeopsComputeStreamCommittedEnd(fullText);
    var committedText = fullText.slice(0, committedEnd);
    var tail = fullText.slice(committedEnd);
    var promoted = edgeopsPromoteCommittableTailPrefix(committedText, tail);
    committedText = promoted.committedText;
    tail = promoted.tail;
    committedEnd += promoted.totalPromoted;
    if (committedText.length < (mountEl._edgeopsStreamCommittedLen || 0)) {
        mountEl.innerHTML = '';
        mountEl._edgeopsIncrementalStream = false;
        mountEl._edgeopsStreamSegmentCount = 0;
        mountEl._edgeopsStreamCommittedLen = 0;
        edgeopsEnsureStreamIncrementalDom(mountEl);
    }

    var segments = edgeopsParseCommittedStreamSegments(committedText);
    var domCount = mountEl._edgeopsStreamSegmentCount || 0;
    var tailWrap = mountEl.querySelector('[data-edgeops-stream-tail]');
    var tailPlain = mountEl._edgeopsStreamTailPlain;
    var tailTable = mountEl._edgeopsStreamTailTable;
    var migratedTableHtml = '';
    if (domCount < segments.length && tailTable && tailTable.innerHTML
        && tailTable.style.display !== 'none') {
        migratedTableHtml = tailTable.innerHTML;
    }
    var inProcessPanel = !!(mountEl.closest && mountEl.closest('.ai-reply-prose-panel'));
    for (var s = domCount; s < segments.length; s++) {
        var segEl = document.createElement('div');
        segEl.className = 'edgeops-stream-segment';
        segEl.setAttribute('data-edgeops-stream-segment', String(s));
        var segText = segments[s].text || '';
        var useMigrated = false;
        if (migratedTableHtml && s === segments.length - 1) {
            var pipeAt = segText.search(/\|/);
            var lineStart = pipeAt >= 0 ? pipeAt : -1;
            if (lineStart >= 0) {
                while (lineStart > 0 && segText[lineStart - 1] !== '\n') lineStart--;
            }
            var scanM = lineStart >= 0 ? edgeopsScanMarkdownTableFrom(segText, lineStart) : null;
            if (scanM && scanM.complete) {
                var trimmedLen = segText.replace(/\s+$/, '').length;
                if (scanM.end >= trimmedLen) useMigrated = true;
            }
        }
        if (useMigrated) {
            var prefixOnly = lineStart >= 0 ? segText.slice(0, lineStart).replace(/\s+$/, '') : '';
            segEl.innerHTML = (prefixOnly ? formatMarkdown(prefixOnly + '\n') : '') + migratedTableHtml;
            migratedTableHtml = '';
            tailTable.innerHTML = '';
            tailTable._edgeopsStreamKey = '';
            tailTable._edgeopsStreamHeader = '';
            tailTable.style.display = 'none';
        } else {
            segEl.innerHTML = typeof edgeopsFormatStreamCommittedSegmentHtml === 'function'
                ? edgeopsFormatStreamCommittedSegmentHtml(segText)
                : formatMarkdown(segText);
        }
        if (inProcessPanel) {
            // 过程面板：流式段落仅作临时 DOM，工具执行时再合并为一条「过程 N」；勿提前包 prose 标题，避免空壳「过程 1」与提交后「过程 10」重复。
            var insertEl = segEl;
            var liveItem = edgeopsEnsureStreamTailLiveItem(mountEl);
            if (liveItem) mountEl.insertBefore(insertEl, liveItem);
            else if (tailWrap) mountEl.insertBefore(insertEl, tailWrap);
            else mountEl.appendChild(insertEl);
        } else if (tailWrap) {
            mountEl.insertBefore(segEl, tailWrap);
        } else {
            mountEl.appendChild(segEl);
        }
        if (hydrateRoot && typeof edgeopsHydrateChatDiagrams === 'function') {
            edgeopsHydrateChatDiagrams(segEl);
        }
    }
    var addedSegments = segments.length - domCount;
    mountEl._edgeopsStreamSegmentCount = segments.length;
    mountEl._edgeopsStreamCommittedLen = committedEnd;

    if (addedSegments > 0 && tailPlain) {
        tailPlain.textContent = '';
        tailPlain.innerHTML = '';
        tailPlain._edgeopsPlainLen = 0;
        tailPlain._edgeopsPlainCached = '';
        tailPlain._edgeopsMdPrefixKey = '';
        tailPlain._edgeopsHybridKey = '';
    }
    var split = edgeopsSplitTextAndTailMarkdownTable(tail);
    if (split.hasTailTable) {
        edgeopsUpdateMarkdownTailPrefix(tailPlain, split.before || '', hydrateRoot);
        edgeopsUpdateStreamTailTable(tailTable, split.header, split.rows || '');
    } else {
        if (tailTable) {
            tailTable.innerHTML = '';
            tailTable._edgeopsStreamKey = '';
            tailTable.style.display = 'none';
        }
        edgeopsUpdateHybridMarkdownTail(tailPlain, tail, hydrateRoot);
    }
    if (typeof edgeopsRefreshReplyProseHeader === 'function') {
        var prosePanel = mountEl.closest('.ai-reply-prose-panel');
        if (prosePanel) edgeopsRefreshReplyProseHeader(prosePanel, { live: !!(tail && tail.trim()) });
    }
}

function edgeopsEnsureStreamPlainLiveMount(mountEl) {
    if (!mountEl) return null;
    var live = mountEl.querySelector('[data-edgeops-plain-live]');
    if (live) return live;
    live = document.createElement('div');
    live.className = 'edgeops-stream-plain-live ai-reply-stream-plain';
    live.setAttribute('data-edgeops-plain-live', '1');
    if (mountEl.closest && mountEl.closest('.ai-reply-prose-panel')) {
        var item = document.createElement('div');
        item.className = 'ai-reply-prose-item ai-reply-prose-item-live';
        var head = document.createElement('div');
        head.className = 'ai-reply-prose-item-head';
        head.textContent = (typeof t === 'function') ? t('hostAi.proseItemLive') : '\u2026';
        var body = document.createElement('div');
        body.className = 'ai-reply-prose-item-body';
        body.appendChild(live);
        item.appendChild(head);
        item.appendChild(body);
        mountEl.appendChild(item);
    } else {
        mountEl.appendChild(live);
    }
    return live;
}

function edgeopsResolveStreamPlainMount(mountEl) {
    if (!mountEl) return null;
    if (mountEl.querySelector('.ai-reply-prose-item-frozen, .edgeops-stream-segment')) {
        return edgeopsEnsureStreamPlainLiveMount(mountEl);
    }
    if (mountEl._edgeopsPlainUsesChild) {
        return mountEl.querySelector('[data-edgeops-plain-live]') || mountEl;
    }
    return mountEl;
}

/**
 * 流式输出期间仅追加纯文本（不解析 Markdown）。无分块需求时仍可用。
 */
function edgeopsRenderStreamPlainText(mountEl, fullText) {
    if (!mountEl) return;
    fullText = fullText == null ? '' : String(fullText);
    var target = edgeopsResolveStreamPlainMount(mountEl);
    if (!target) return;
    if (target !== mountEl) mountEl._edgeopsPlainUsesChild = true;
    if (!target._edgeopsPlainStream) {
        if (target === mountEl && typeof edgeopsClearStreamIncrementalState === 'function') {
            edgeopsClearStreamIncrementalState(mountEl);
        }
        target._edgeopsPlainStream = true;
        target._edgeopsPlainLen = 0;
        target.classList.add('ai-reply-stream-plain');
        target.textContent = '';
    }
    var prev = target._edgeopsPlainLen || 0;
    if (fullText.length < prev) {
        target.textContent = fullText;
        target._edgeopsPlainLen = fullText.length;
        return;
    }
    if (fullText.length > prev) {
        target.append(document.createTextNode(fullText.slice(prev)));
        target._edgeopsPlainLen = fullText.length;
    }
}

function edgeopsClearStreamPlainTextState(mountEl) {
    if (!mountEl) return;
    mountEl._edgeopsPlainStream = false;
    mountEl._edgeopsPlainLen = 0;
    mountEl._edgeopsPlainUsesChild = false;
    mountEl.classList.remove('ai-reply-stream-plain');
    var plainLive = mountEl.querySelector('[data-edgeops-plain-live]');
    if (plainLive) {
        plainLive._edgeopsPlainStream = false;
        plainLive._edgeopsPlainLen = 0;
    }
    var liveWrap = mountEl.querySelector('.ai-reply-prose-item-live');
    if (liveWrap && liveWrap.querySelector('[data-edgeops-plain-live]')) liveWrap.remove();
}

function edgeopsClearStreamIncrementalState(mountEl) {
    if (!mountEl) return;
    mountEl._edgeopsIncrementalStream = false;
    mountEl._edgeopsStreamSegmentCount = 0;
    mountEl._edgeopsStreamCommittedLen = 0;
    mountEl._edgeopsStreamTailPlain = null;
    mountEl._edgeopsStreamTailTable = null;
    mountEl.classList.remove('ai-reply-stream-incremental');
    edgeopsClearStreamPlainTextState(mountEl);
    var tp = mountEl.querySelector('[data-edgeops-stream-tail-plain]');
    if (tp) {
        tp._edgeopsMdPrefixKey = '';
        tp._edgeopsHybridKey = '';
    }
}

/**
 * @deprecated 流式阶段请用 edgeopsRenderStreamPlainText；保留供非逐字场景可选。
 */
function edgeopsFormatMarkdownStreaming(text, mountEl) {
    if (!mountEl) return formatMarkdown(text);
    var raw = text == null ? '' : String(text);
    var split = edgeopsSplitTextAndTailMarkdownTable(raw);
    if (!split.hasTailTable) {
        mountEl.innerHTML = formatMarkdown(raw);
        mountEl._edgeopsStreamMode = '';
        return;
    }
    mountEl._edgeopsStreamMode = 'table';
    var prefixEl = mountEl.querySelector('[data-edgeops-stream-prefix]');
    var tableHost = mountEl.querySelector('[data-edgeops-stream-table]');
    if (!prefixEl || !tableHost) {
        mountEl.innerHTML = '';
        prefixEl = document.createElement('div');
        prefixEl.setAttribute('data-edgeops-stream-prefix', '1');
        tableHost = document.createElement('div');
        tableHost.setAttribute('data-edgeops-stream-table', '1');
        mountEl.appendChild(prefixEl);
        mountEl.appendChild(tableHost);
    }
    var prefixKey = split.before;
    if (prefixEl._edgeopsStreamKey !== prefixKey) {
        prefixEl.innerHTML = prefixKey ? formatMarkdown(prefixKey) : '';
        prefixEl._edgeopsStreamKey = prefixKey;
    }
    var tableKey = split.header + '\n---\n' + split.rows;
    if (tableHost._edgeopsStreamKey === tableKey) return;
    var existingTbody = tableHost.querySelector('table.chat-md-table tbody');
    if (existingTbody && tableHost._edgeopsStreamHeader === split.header) {
        var tmp = document.createElement('div');
        tmp.innerHTML = edgeopsBuildChatMarkdownTableHtml(split.header, split.rows);
        var newTbody = tmp.querySelector('tbody');
        if (newTbody) {
            existingTbody.innerHTML = newTbody.innerHTML;
            tableHost._edgeopsStreamKey = tableKey;
            tableHost._edgeopsStreamHeader = split.header;
            return;
        }
    }
    tableHost.innerHTML = edgeopsBuildChatMarkdownTableHtml(split.header, split.rows);
    tableHost._edgeopsStreamHeader = split.header;
    tableHost._edgeopsStreamKey = tableKey;
}

/** 提示词 Markdown 预览 HTML */
function edgeopsPromptMarkdownPreview(text) {
    var s = text == null ? '' : String(text);
    return (typeof formatMarkdown !== 'undefined' ? formatMarkdown(s) : (typeof esc !== 'undefined' ? esc(s) : s));
}

function edgeopsShowPromptEditTab(editWrap, previewDiv, editTab, previewTab) {
    if (editWrap) editWrap.style.display = 'flex';
    if (previewDiv) previewDiv.style.display = 'none';
    if (editTab) editTab.classList.add('active');
    if (previewTab) previewTab.classList.remove('active');
}

function edgeopsShowPromptPreviewTab(textEl, previewDiv, editWrap, editTab, previewTab) {
    if (previewDiv) {
        previewDiv.innerHTML = edgeopsPromptMarkdownPreview(textEl ? textEl.value : '');
        previewDiv.style.display = 'block';
    }
    if (editWrap) editWrap.style.display = 'none';
    if (previewTab) previewTab.classList.add('active');
    if (editTab) editTab.classList.remove('active');
}

function edgeopsRefreshPromptPreviewIfVisible(textEl, previewDiv) {
    if (!previewDiv || previewDiv.style.display === 'none') return;
    previewDiv.innerHTML = edgeopsPromptMarkdownPreview(textEl ? textEl.value : '');
}

/** 绑定提示词弹窗/卡片上的「编辑 / 预览」Tab；默认 preview */
function edgeopsBindPromptEditPreview(opts) {
    if (!opts || !opts.editTab || !opts.previewTab) return;
    opts.editTab.onclick = function() {
        edgeopsShowPromptEditTab(opts.editWrap, opts.previewDiv, opts.editTab, opts.previewTab);
    };
    opts.previewTab.onclick = function() {
        edgeopsShowPromptPreviewTab(opts.textEl, opts.previewDiv, opts.editWrap, opts.editTab, opts.previewTab);
    };
    if (opts.applyInitial === false) return;
    if ((opts.defaultMode || 'preview') === 'edit') {
        edgeopsShowPromptEditTab(opts.editWrap, opts.previewDiv, opts.editTab, opts.previewTab);
    } else {
        edgeopsShowPromptPreviewTab(opts.textEl, opts.previewDiv, opts.editWrap, opts.editTab, opts.previewTab);
    }
}

if (typeof window !== 'undefined') {
    window.formatMarkdown = formatMarkdown;
    window.markdownToHtml = markdownToHtml;
    window.edgeopsGetMarkdownIt = edgeopsGetMarkdownIt;
    window.edgeopsStripHtmlComments = edgeopsStripHtmlComments;
    window.edgeopsSanitizeLeakedToolMarkup = edgeopsSanitizeLeakedToolMarkup;
    window.edgeopsProseItemSummary = edgeopsProseItemSummary;
    window.edgeopsParseCommittedStreamSegments = edgeopsParseCommittedStreamSegments;
    window.edgeopsRenderStreamIncremental = edgeopsRenderStreamIncremental;
    window.edgeopsRenderStreamPlainText = edgeopsRenderStreamPlainText;
    window.edgeopsClearStreamIncrementalState = edgeopsClearStreamIncrementalState;
    window.edgeopsClearStreamPlainTextState = edgeopsClearStreamPlainTextState;
    window.edgeopsFormatMarkdownStreaming = edgeopsFormatMarkdownStreaming;
    window.formatMarkdownInline = formatMarkdownInline;
    window.edgeopsSanitizeSafeInlineHtml = edgeopsSanitizeSafeInlineHtml;
    window.edgeopsSanitizeInlineStyle = edgeopsSanitizeInlineStyle;
    window.edgeopsPromptMarkdownPreview = edgeopsPromptMarkdownPreview;
    window.edgeopsShowPromptEditTab = edgeopsShowPromptEditTab;
    window.edgeopsShowPromptPreviewTab = edgeopsShowPromptPreviewTab;
    window.edgeopsRefreshPromptPreviewIfVisible = edgeopsRefreshPromptPreviewIfVisible;
    window.edgeopsBindPromptEditPreview = edgeopsBindPromptEditPreview;
}
