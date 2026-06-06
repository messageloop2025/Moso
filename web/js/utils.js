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

/** 页面顶部说明段落；空字符串时不渲染。plain=true 时对文本转义（无 HTML）。 */
function edgeopsPageIntroHtml(text, plain) {
    var s = String(text == null ? '' : text).trim();
    if (!s) return '';
    var body = plain ? esc(s) : s;
    return '<p class="page-intro">' + body + '</p>';
}

var _modalKeyHandler = null;
var EDGEOPS_SAFE_HTML_TAG_NAMES = 'strong|em|b|i|u|code|kbd|mark|span|a|sup|sub';
var EDGEOPS_SAFE_HTML_TAG_RE = new RegExp('^(?:' + EDGEOPS_SAFE_HTML_TAG_NAMES + ')$', 'i');
var EDGEOPS_DIAGRAM_LANG_MAP = {
    mermaid: 'mermaid',
    markmap: 'markmap',
    mindmap: 'markmap',
    echarts: 'echarts',
    'echarts-option': 'echarts',
    chart: 'echarts'
};

function showModal(title, content, footer) {
    closeModal();
    footer = footer || '';
    var html = '<div class="modal-overlay" onclick="if(event.target===this)closeModal()">' +
        '<div class="modal">' +
        '<div class="modal-header"><h3>' + title + '</h3><button class="modal-close" onclick="closeModal()">&times;</button></div>' +
        '<div class="modal-body">' + content + '</div>' +
        (footer ? '<div class="modal-footer">' + footer + '</div>' : '') +
        '</div></div>';
    document.body.insertAdjacentHTML('beforeend', html);
    _modalKeyHandler = function(e) {
        if (e.key === 'Escape') {
            closeModal();
        } else if (e.key === 'Enter') {
            var active = document.activeElement;
            if (active && (active.tagName === 'INPUT' || active.tagName === 'TEXTAREA' || active.tagName === 'SELECT')) return;
            var overlay = document.querySelector('.modal-overlay');
            if (!overlay) return;
            if (active && overlay.contains(active) && active.tagName === 'BUTTON') {
                active.click();
                return;
            }
            var primary = overlay.querySelector('.modal-footer .btn-danger') || overlay.querySelector('.modal-footer .btn-primary');
            if (primary) primary.click();
        }
    };
    document.addEventListener('keydown', _modalKeyHandler);
}

function closeModal() {
    if (_modalKeyHandler) {
        document.removeEventListener('keydown', _modalKeyHandler);
        _modalKeyHandler = null;
    }
    document.querySelectorAll('.modal-overlay').forEach(function(el) { el.remove(); });
}

function showConfirm(title, message) {
    return new Promise(function(resolve) {
        var btnCancel = (typeof t === 'function' ? t('common.cancel') : 'Cancel');
        var btnOk = (typeof t === 'function' ? t('common.confirm') : 'Confirm');
        showModal(title, '<p>' + message + '</p>',
            '<button class="btn" onclick="window._confirmResolve(false)">' + btnCancel + '</button>' +
            ' <button class="btn btn-danger" onclick="window._confirmResolve(true)">' + btnOk + '</button>');
        window._confirmResolve = function(value) {
            closeModal();
            resolve(value);
        };
        setTimeout(function() {
            var overlay = document.querySelector('.modal-overlay');
            if (overlay) {
                var primary = overlay.querySelector('.modal-footer .btn-danger') || overlay.querySelector('.modal-footer .btn-primary');
                if (primary) primary.focus();
            }
        }, 0);
    });
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
    var label = type === 'mermaid' ? 'Mermaid' : (type === 'markmap' ? 'Markmap' : 'ECharts');
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
    var escaped = escapeHtmlForCode(code || '');
    var highlighted = edgeopsHighlightEscapedCode(escaped, normalized);
    var label = normalized === 'text' ? 'text' : normalized;
    if (normalized === 'tree') label = 'tree';
    return ''
        + '<div class="chat-code-block-wrap" data-code-lang="' + escapeHtmlForCode(label) + '">'
        + '<div class="chat-code-toolbar"><span>' + escapeHtmlForCode(label) + '</span></div>'
        + '<pre class="chat-code-block language-' + escapeHtmlForCode(normalized) + '"><code>' + highlighted + '</code></pre>'
        + '</div>';
}

/**
 * 仅处理行内格式（用于表格单元格、列表项、标题等）：<strong>/<code> 等安全标签、**粗体**、*斜体*、`代码`、链接
 */
function formatMarkdownInline(text) {
    if (!text) return '';
    var s = normalizeSafeHtmlTags(text);
    var inlineMath = [];
    s = edgeopsApplyInlineMathPlaceholders(s, inlineMath, 'EDGEOPSMDINLINEMATH');
    var inlineCodes = [];
    s = s.replace(/\x60([^\x60\n]+)\x60/g, function (match, code) {
        var id = 'EDGEOPSMDINLINE' + inlineCodes.length;
        inlineCodes.push(code);
        return id;
    });
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
    s = s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
    for (var i = 0; i < safeHtmlTags.length; i++) {
        var item = safeHtmlTags[i];
        if (item.content === null) {
            s = edgeopsReplaceIndexedPlaceholder(s, 'EDGEOPSMDSAFE', i, '<br>');
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
    s = s.replace(/\*\*([^*]+?)\*\*/g, '<strong>$1</strong>');
    s = s.replace(/__([^_]+?)__/g, '<strong>$1</strong>');
    s = s.replace(/\*([^*\n]+?)\*/g, '<em>$1</em>');
    s = s.replace(/_([^_\n]+?)_/g, '<em>$1</em>');
    s = s.replace(/!\[([^\]]*)\]\(([^)]+)\)/g, function (_, alt, src) {
        var u = edgeopsRewriteChatAttachmentUrl(String(src || '').trim());
        return '<img class="chat-md-inline-image chat-attachment-image-inline" src="' + escapeHtmlForCode(u) + '" alt="' + escapeHtmlForCode(alt || 'image') + '" loading="lazy">';
    });
    s = s.replace(/\[([^\]]+)\]\(([^)]+)\)/g, function (_, label, href) {
        var u = edgeopsRewriteChatAttachmentUrl(String(href || '').trim());
        return '<a href="' + escapeHtmlForCode(u) + '" target="_blank" rel="noopener noreferrer">' + label + '</a>';
    });
    for (var j = 0; j < inlineCodes.length; j++) {
        s = edgeopsReplaceIndexedPlaceholder(s, 'EDGEOPSMDINLINE', j, '<code>' + escapeHtmlForCode(inlineCodes[j]) + '</code>');
    }
    for (var m = 0; m < inlineMath.length; m++) {
        s = edgeopsReplaceIndexedPlaceholder(s, 'EDGEOPSMDINLINEMATH', m, edgeopsRenderMathHtml(inlineMath[m], false));
    }
    // 模型偶发不写 $...$ 时，常见符号仍可读
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

function edgeopsIsMarkdownTableRowLine(line) {
    var t = String(line || '').trim();
    if (!t || t.indexOf('|') === -1) return false;
    return /^\|.+\|$/.test(t) || /^\|/.test(t);
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
        if (edgeopsIsMarkdownTableRowLine(trimmed) || edgeopsIsMarkdownTableSeparatorLine(trimmed)) {
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
    if (!edgeopsIsMarkdownTableRowLine(headerLine) || !edgeopsIsMarkdownTableSeparatorLine(sepLine)) return null;
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
 * Markdown 转 HTML（从 IOTHub 提取）：代码块、行内代码、表格、标题、列表、粗斜体、链接、段落换行
 * 表格在全局转义前提取，单元格用 formatMarkdownInline 解析，使表格内 strong/code/星斜杠/反引号 等正常显示
 */
function markdownToHtml(text) {
    if (!text) return '';
    var html = edgeopsCompactMarkdownBlankLines(normalizeSafeHtmlTags(text));

    var codeBlocks = [];
    html = html.replace(/\x60\x60\x60([^\n`]*)\n?([\s\S]*?)\x60\x60\x60/g, function (match, lang, code) {
        var id = 'EDGEOPSMDCODEBLOCK' + codeBlocks.length;
        var info = String(lang || '').trim();
        codeBlocks.push({ lang: info ? info.split(/\s+/)[0] : '', code: code.trim() });
        return id;
    });

    var mathBlocks = [];
    html = edgeopsNormalizeMathDelimiters(html);
    html = html.replace(/\$\$([\s\S]+?)\$\$/g, function (match, expr) {
        var id = 'EDGEOPSMDMATHBLOCK' + mathBlocks.length;
        mathBlocks.push(String(expr || '').trim());
        return '\n' + id + '\n';
    });
    html = html.replace(/\\\[([\s\S]+?)\\\]/g, function (match, expr) {
        var id = 'EDGEOPSMDMATHBLOCK' + mathBlocks.length;
        mathBlocks.push(String(expr || '').trim());
        return '\n' + id + '\n';
    });
    html = html.split('\n').map(function(line) {
        if (!edgeopsLooksLikeStandaloneLatexMath(line)) return line;
        var id = 'EDGEOPSMDMATHBLOCK' + mathBlocks.length;
        mathBlocks.push(line);
        return id;
    }).join('\n');

    var inlineMath = [];
    html = edgeopsApplyInlineMathPlaceholders(html, inlineMath, 'EDGEOPSMDMATHINLINE');

    // 先提取表格（与 edgeopsComputeStreamCommittedEnd 同一套扫描器，避免流式冻结段与收尾全量渲染不一致）
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

    var inlineCodes = [];
    html = html.replace(/\x60([^\x60\n]+)\x60/g, function (match, code) {
        var id = 'EDGEOPSMDINLINECODE' + inlineCodes.length;
        inlineCodes.push(code);
        return id;
    });

    // 安全 HTML 标签白名单：先提取再统一转义，最后还原，使 AI 返回的 <strong> 等能正确渲染
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

    html = html.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');

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

    for (var i = 0; i < inlineCodes.length; i++) {
        html = edgeopsReplaceIndexedPlaceholder(html, 'EDGEOPSMDINLINECODE', i, '<code>' + escapeHtmlForCode(inlineCodes[i]) + '</code>');
    }

    html = html.replace(/\*\*([^*]+?)\*\*/g, '<strong>$1</strong>');
    html = html.replace(/__([^_]+?)__/g, '<strong>$1</strong>');
    html = html.replace(/\*([^*\n]+?)\*/g, '<em>$1</em>');
    html = html.replace(/_([^_\n]+?)_/g, '<em>$1</em>');
    html = html.replace(/!\[([^\]]*)\]\(([^)]+)\)/g, function (_, alt, src) {
        var u = edgeopsRewriteChatAttachmentUrl(String(src || '').trim());
        return '<img class="chat-md-inline-image chat-attachment-image-inline" src="' + escapeHtmlForCode(u) + '" alt="' + escapeHtmlForCode(alt || 'image') + '" loading="lazy">';
    });
    html = html.replace(/\[([^\]]+)\]\(([^)]+)\)/g, function (_, label, href) {
        var u = edgeopsRewriteChatAttachmentUrl(String(href || '').trim());
        return '<a href="' + escapeHtmlForCode(u) + '" target="_blank" rel="noopener noreferrer">' + label + '</a>';
    });

    // 标题：逐行识别，兼容“###标题”、全角空格、零宽字符，以及模型偶尔输出的转义形式 \#\#\#。
    // 仅在行首识别，避免正文中的 # 被误当作标题。
    html = html.split('\n').map(function(line) {
        var m = String(line || '').match(/^[\s\u00a0\u3000\u200b\ufeff]*((?:\\?#){1,6})[\s\u00a0\u3000\u200b\ufeff]*(\S.*)$/);
        if (!m) return line;
        var marks = m[1].replace(/\\/g, '');
        var rest = (m[2] || '').trim();
        if (!rest) return line;
        var level = Math.min(6, Math.max(1, marks.length));
        return '<h' + level + '>' + formatMarkdownInline(rest) + '</h' + level + '>';
    }).join('\n');

    var lines = html.split('\n');
    var inUl = false, inOl = false, result = [];
    for (var j = 0; j < lines.length; j++) {
        var line = lines[j];
        if (/^[\s]*[-*]\s/.test(line)) {
            if (inOl) { result.push('</ol>'); inOl = false; }
            if (!inUl) { result.push('<ul>'); inUl = true; }
            result.push('<li>' + formatMarkdownInline(line.replace(/^[\s]*[-*]\s+/, '')) + '</li>');
        } else if (/^[\s]*\d+\.\s/.test(line)) {
            if (inUl) { result.push('</ul>'); inUl = false; }
            if (!inOl) { result.push('<ol>'); inOl = true; }
            result.push('<li>' + formatMarkdownInline(line.replace(/^[\s]*\d+\.\s+/, '')) + '</li>');
        } else {
            if (inUl) { result.push('</ul>'); inUl = false; }
            if (inOl) { result.push('</ol>'); inOl = false; }
            result.push(line);
        }
    }
    if (inUl) result.push('</ul>');
    if (inOl) result.push('</ol>');
    html = result.join('\n');

    html = html.replace(/^---$/gm, '<hr>');

    html = html.replace(/(?:^|\n)&gt;\s*\[!(NOTE|TIP|IMPORTANT|WARNING|ERROR)\]\s*([^\n]*)(\n(?:&gt;[^\n]*)*)?/gi, function(match, kind, title, body) {
        var type = String(kind || 'NOTE').toLowerCase();
        var bodyHtml = '';
        if (body) {
            bodyHtml = String(body).split('\n').map(function(line) {
                return line.replace(/^&gt;\s?/, '').trim();
            }).filter(Boolean).join('<br>');
        }
        var titleHtml = formatMarkdownInline((title || '').trim() || kind);
        if (bodyHtml) bodyHtml = formatMarkdownInline(bodyHtml);
        return '\n<div class="chat-callout chat-callout-' + escapeHtmlForCode(type) + '">'
            + '<div class="chat-callout-title">' + titleHtml + '</div>'
            + (bodyHtml ? '<div class="chat-callout-body">' + bodyHtml + '</div>' : '')
            + '</div>\n';
    });

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

    html = html.split(/\n\n+/).map(function (para) {
        para = para.trim();
        if (!para) return '';
        if (/^<(h[1-6]|ul|ol|li|p|div|table|pre|hr)/i.test(para) || /EDGEOPSMDCODEBLOCK\d|EDGEOPSMDTABLE\d/.test(para)) return para;
        return '<p>' + para.replace(/\n/g, '<br>') + '</p>';
    }).join('');

    for (var mi = 0; mi < inlineMath.length; mi++) {
        html = edgeopsReplaceIndexedPlaceholder(html, 'EDGEOPSMDMATHINLINE', mi, edgeopsRenderMathHtml(inlineMath[mi], false));
    }

    return html;
}

function formatMarkdown(text) {
    if (!text) return '';
    return markdownToHtml(text);
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
    if (!edgeopsIsMarkdownTableSeparatorLine(sep)) return null;
    var endLine = 2;
    while (endLine < lines.length) {
        var t = String(lines[endLine] || '').trim();
        if (!t) break;
        if (!edgeopsIsMarkdownTableRowLine(t) && !edgeopsIsMarkdownTableSeparatorLine(t)) break;
        endLine++;
    }
    var complete = endLine < lines.length;
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
        tailEl._edgeopsHybridKey = '';
        tailEl._edgeopsPlainLen = 0;
        tailEl._edgeopsPlainCached = '';
        tailEl._edgeopsMdPrefixKey = '';
        return;
    }
    var committedEnd = edgeopsComputeStreamCommittedEnd(fullText);
    var cacheKey = committedEnd + '|' + fullText.length;
    if (tailEl._edgeopsHybridKey === cacheKey) return;
    tailEl._edgeopsHybridKey = cacheKey;
    tailEl._edgeopsMdPrefixKey = '';
    var safe = fullText.slice(0, committedEnd);
    var rest = fullText.slice(committedEnd);
    var html = safe.trim() ? formatMarkdown(safe) : '';
    if (rest) {
        html += '<span class="edgeops-stream-incomplete-chunk ai-reply-stream-plain"></span>';
    }
    tailEl.innerHTML = html;
    tailEl.classList.toggle('ai-reply-stream-plain', !safe.trim() && !!rest);
    if (rest) {
        var span = tailEl.querySelector('.edgeops-stream-incomplete-chunk');
        if (span) span.textContent = rest;
    }
    tailEl._edgeopsPlainLen = fullText.length;
    tailEl._edgeopsPlainCached = fullText;
    if (hydrateRoot && safe.trim() && typeof edgeopsHydrateChatDiagrams === 'function') {
        edgeopsHydrateChatDiagrams(tailEl);
    }
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
        if (tailWrap) mountEl.insertBefore(segEl, tailWrap);
        else mountEl.appendChild(segEl);
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
}

/**
 * 流式输出期间仅追加纯文本（不解析 Markdown）。无分块需求时仍可用。
 */
function edgeopsRenderStreamPlainText(mountEl, fullText) {
    if (!mountEl) return;
    fullText = fullText == null ? '' : String(fullText);
    if (!mountEl._edgeopsPlainStream) {
        mountEl._edgeopsPlainStream = true;
        mountEl._edgeopsPlainLen = 0;
        mountEl.classList.add('ai-reply-stream-plain');
        mountEl.textContent = '';
    }
    var prev = mountEl._edgeopsPlainLen || 0;
    if (fullText.length < prev) {
        mountEl.textContent = fullText;
        mountEl._edgeopsPlainLen = fullText.length;
        return;
    }
    if (fullText.length > prev) {
        mountEl.append(document.createTextNode(fullText.slice(prev)));
        mountEl._edgeopsPlainLen = fullText.length;
    }
}

function edgeopsClearStreamPlainTextState(mountEl) {
    if (!mountEl) return;
    mountEl._edgeopsPlainStream = false;
    mountEl._edgeopsPlainLen = 0;
    mountEl.classList.remove('ai-reply-stream-plain');
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

if (typeof window !== 'undefined') {
    window.formatMarkdown = formatMarkdown;
    window.edgeopsRenderStreamIncremental = edgeopsRenderStreamIncremental;
    window.edgeopsRenderStreamPlainText = edgeopsRenderStreamPlainText;
    window.edgeopsClearStreamIncrementalState = edgeopsClearStreamIncrementalState;
    window.edgeopsClearStreamPlainTextState = edgeopsClearStreamPlainTextState;
    window.edgeopsFormatMarkdownStreaming = edgeopsFormatMarkdownStreaming;
    window.formatMarkdownInline = formatMarkdownInline;
    window.edgeopsSanitizeSafeInlineHtml = edgeopsSanitizeSafeInlineHtml;
    window.edgeopsSanitizeInlineStyle = edgeopsSanitizeInlineStyle;
}
