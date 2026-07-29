/**
 * 毛竹 前端 i18n
 * - 未设置 edgeops.uiLocale 时使用浏览器语言（normalize 为 en 或 zh-CN）
 * - 已设置时以用户选择为准
 * - 合并规则：先加载完整 zh-CN 各模块，再叠加载目标语言各模块并深度合并
 */
(function (global) {
    "use strict";

    var LOCALE_KEY = "edgeops.uiLocale";
    var SUPPORTED = ["en", "zh-CN"];
    var MANIFEST = ["meta", "nav", "common", "layout", "auth", "settings", "model-config", "mcp", "skills", "pages", "toasts", "api", "ai", "host", "batch", "files", "feedback", "misc", "events"];

    function normalizeTag(tag) {
        if (!tag) return "zh-CN";
        var t = String(tag).trim().replace(/_/g, "-");
        var l = t.toLowerCase();
        if (l === "zh" || l === "zh-hans" || l === "zh-cn" || l === "zh-sg" || l === "zh-hk" || l === "zh-tw" || l === "zh-mo") return "zh-CN";
        if (l === "en" || l.indexOf("en-") === 0) return "en";
        return "zh-CN";
    }

    function getBrowserLocale() {
        try {
            if (global.navigator && navigator.languages && navigator.languages.length) {
                for (var i = 0; i < navigator.languages.length; i++) {
                    var n = normalizeTag(navigator.languages[i]);
                    if (SUPPORTED.indexOf(n) >= 0) return n;
                }
            }
            if (global.navigator && navigator.language) return normalizeTag(navigator.language);
        } catch (e) {}
        return "zh-CN";
    }

    function getUserLocaleOverride() {
        try {
            var v = localStorage.getItem(LOCALE_KEY);
            if (v == null || v === "") return null;
            var n = normalizeTag(String(v).trim());
            if (SUPPORTED.indexOf(n) < 0) return null;
            return n;
        } catch (e) { return null; }
    }

    function getEffectiveLocale() {
        var o = getUserLocaleOverride();
        if (o) return o;
        return getBrowserLocale();
    }

    function setUserLocale(tag) {
        var n = normalizeTag(tag);
        if (SUPPORTED.indexOf(n) < 0) n = "zh-CN";
        try { localStorage.setItem(LOCALE_KEY, n); } catch (e) {}
    }

    function deepMerge(a, b) {
        if (b == null) return a;
        if (a == null) return b;
        if (typeof a !== "object" || Array.isArray(a) || typeof b !== "object" || Array.isArray(b)) return b;
        var out = {};
        var k;
        for (k in a) {
            if (Object.prototype.hasOwnProperty.call(a, k)) out[k] = a[k];
        }
        for (k in b) {
            if (!Object.prototype.hasOwnProperty.call(b, k)) continue;
            if (a[k] && typeof a[k] === "object" && !Array.isArray(a[k]) && b[k] && typeof b[k] === "object" && !Array.isArray(b[k])) {
                out[k] = deepMerge(a[k], b[k]);
            } else {
                out[k] = b[k];
            }
        }
        return out;
    }

    function getByPath(obj, path) {
        if (obj == null || !path) return null;
        var parts = String(path).split(".");
        var cur = obj;
        for (var i = 0; i < parts.length; i++) {
            if (cur == null || typeof cur !== "object") return null;
            cur = cur[parts[i]];
        }
        return cur;
    }

    var I18n = {
        LOCALE_KEY: LOCALE_KEY,
        SUPPORTED: SUPPORTED,
        /** 合并后查找用（zh + 用户语言覆盖） */
        bundle: {},
        locale: "zh-CN",
        getBrowserLocale: getBrowserLocale,
        getUserLocaleOverride: getUserLocaleOverride,
        getEffectiveLocale: getEffectiveLocale,
        normalizeTag: normalizeTag,
        setUserLocale: setUserLocale,

        t: function (key, params) {
            if (!key) return "";
            var v = getByPath(this.bundle, key);
            if (v == null || (typeof v === "object" && v !== null)) v = String(key);
            else v = String(v);
            if (params && typeof params === "object") {
                v = v.replace(/\{\{(\w+)\}\}/g, function (_, name) {
                    return params[name] != null ? String(params[name]) : "";
                });
            }
            return v;
        },

        _fetchMod: function (loc, name) {
            return fetch("/static/locales/" + loc + "/" + name + ".json", { cache: "no-cache" })
                .then(function (r) {
                    if (!r.ok) throw r;
                    return r.text().then(function (t) {
                        if (t && t.charCodeAt(0) === 0xFEFF) t = t.slice(1);
                        try { return JSON.parse(t); } catch (e) { return {}; }
                    });
                })
                .catch(function () { return {}; });
        },

        init: function () {
            var self = this;
            this.locale = this.getEffectiveLocale();
            if (SUPPORTED.indexOf(this.locale) < 0) this.locale = "zh-CN";
            return Promise.all(MANIFEST.map(function (m) { return I18n._fetchMod("zh-CN", m); })).then(function (zhParts) {
                var zh = {};
                for (var i = 0; i < zhParts.length; i++) {
                    zh = deepMerge(zh, zhParts[i] || {});
                }
                if (self.locale === "zh-CN") {
                    self.bundle = zh;
                    return null;
                }
                return Promise.all(MANIFEST.map(function (m) { return I18n._fetchMod(self.locale, m); })).then(function (exParts) {
                    var ex = {};
                    for (var j = 0; j < exParts.length; j++) {
                        ex = deepMerge(ex, exParts[j] || {});
                    }
                    self.bundle = deepMerge(zh, ex);
                });
            }).then(function () {
                try {
                    document.documentElement.lang = self.locale === "en" ? "en" : "zh-CN";
                } catch (e) {}
            });
        }
    };

    global.I18n = I18n;
    global.t = function (k, p) { return I18n.t(k, p); };
})(typeof window !== "undefined" ? window : this);
