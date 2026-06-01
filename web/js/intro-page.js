/**
 * 毛竹 产品介绍页 /intro/ 浏览器端组字
 * - 布局仅一份 index.html；各语言文案在 /static/locales/{locale}/intro.json
 * - 语言：?lang= > localStorage edgeops.uiLocale > 浏览器 Accept-Language
 * - 新增语言：加 web/locales/{xx}/intro.json，并把 SUPPORTED 里加上即可
 */
(function () {
    "use strict";

    var SUPPORTED = ["zh-CN", "en"];
    var LOCALE_KEY = "edgeops.uiLocale";

    function normalizeTag(tag) {
        if (!tag) return "zh-CN";
        var t = String(tag).trim().replace(/_/g, "-");
        var l = t.toLowerCase();
        if (l === "zh" || l === "zh-hans" || l === "zh-cn" || l === "zh-sg" || l === "zh-hk" || l === "zh-tw" || l === "zh-mo") return "zh-CN";
        if (l === "en" || l.indexOf("en-") === 0) return "en";
        return "zh-CN";
    }

    /** 比 URLSearchParams 更稳：对 ?lang / ?Lang 均识别，并解析整段 href 防止边缘环境下 search 丢参 */
    function getQueryLang() {
        try {
            var href = String(window.location.href || "");
            var m = href.match(/[?&#]lang=([^&#]+)/i);
            if (m) {
                try {
                    return decodeURIComponent(m[1].replace(/\+/g, " "));
                } catch (e) {
                    return m[1];
                }
            }
        } catch (e) {}

        try {
            var p = new URLSearchParams(window.location.search);
            var v2 = p.get("lang");
            if (v2) return v2;
            if (p.forEach) {
                p.forEach(function (val, key) {
                    if (!v2 && key && String(key).toLowerCase() === "lang") v2 = val;
                });
            }
            if (v2) return v2;
        } catch (e2) {}
        return null;
    }

    function fromQuery() {
        try {
            var q = getQueryLang();
            if (q == null || q === "") return null;
            var n = normalizeTag(q);
            return SUPPORTED.indexOf(n) >= 0 ? n : null;
        } catch (e) {
            return null;
        }
    }

    function fromStorage() {
        try {
            var v = localStorage.getItem(LOCALE_KEY);
            if (v == null || v === "") return null;
            var n = normalizeTag(v);
            return SUPPORTED.indexOf(n) >= 0 ? n : null;
        } catch (e) {
            return null;
        }
    }

    function fromBrowser() {
        try {
            if (navigator.languages && navigator.languages.length) {
                for (var i = 0; i < navigator.languages.length; i++) {
                    var n = normalizeTag(navigator.languages[i]);
                    if (SUPPORTED.indexOf(n) >= 0) return n;
                }
            }
            if (navigator.language) return normalizeTag(navigator.language);
        } catch (e) {}
        return "zh-CN";
    }

    function effectiveLocale() {
        return fromQuery() || fromStorage() || fromBrowser();
    }

    function applyBundle(data) {
        if (!data || !data.partials) return;
        var p = data.partials;
        var nav = document.querySelector("nav.nav .nav-inner");
        if (nav && p.navInner) nav.innerHTML = p.navInner;
        var hero = document.querySelector("section#hero .hero-inner, section.hero .hero-inner");
        if (hero && p.heroInner) hero.innerHTML = p.heroInner;
        var ids = [
            "start-here",
            "positioning",
            "ai",
            "multihost",
            "features",
            "architecture",
            "security",
            "tasks",
            "tech-stack",
            "faq",
            "cta",
        ];
        for (var i = 0; i < ids.length; i++) {
            var id = ids[i];
            var sec = document.getElementById(id);
            if (sec && p[id] != null) sec.innerHTML = p[id];
        }
        var ft = document.querySelector("footer");
        if (ft && p.footer) ft.innerHTML = p.footer;
        if (data.meta) {
            if (data.meta.title) document.title = data.meta.title;
            var md = document.querySelector('meta[name="description"]');
            if (md && data.meta.description != null) md.setAttribute("content", data.meta.description);
        }
    }

    function initReveal() {
        var observer = new IntersectionObserver(
            function (entries) {
                entries.forEach(function (e) {
                    if (e.isIntersecting) {
                        e.target.classList.add("show");
                        observer.unobserve(e.target);
                    }
                });
            },
            { threshold: 0.12 }
        );
        document.querySelectorAll(".reveal").forEach(function (el) {
            observer.observe(el);
        });
    }

    function initHeroCanvas() {
        var c = document.getElementById("heroCanvas");
        if (!c) return;
        var ctx = c.getContext("2d");
        var w = 0,
            h = 0,
            dpr = window.devicePixelRatio || 1;
        var particles = [];
        var N = 80;
        function resize() {
            w = c.clientWidth = c.offsetWidth;
            h = c.clientHeight = c.offsetHeight;
            c.width = w * dpr;
            c.height = h * dpr;
            ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
        }
        function initP() {
            particles = [];
            for (var i = 0; i < N; i++) {
                particles.push({
                    x: Math.random() * w,
                    y: Math.random() * h,
                    vx: (Math.random() - 0.5) * 0.35,
                    vy: (Math.random() - 0.5) * 0.35,
                    r: Math.random() * 1.8 + 0.4,
                });
            }
        }
        function draw() {
            ctx.clearRect(0, 0, w, h);
            for (var i = 0; i < particles.length; i++) {
                var p = particles[i];
                p.x += p.vx;
                p.y += p.vy;
                if (p.x < 0 || p.x > w) p.vx *= -1;
                if (p.y < 0 || p.y > h) p.vy *= -1;
                for (var j = i + 1; j < particles.length; j++) {
                    var q = particles[j];
                    var dx = p.x - q.x,
                        dy = p.y - q.y;
                    var d = Math.sqrt(dx * dx + dy * dy);
                    if (d < 140) {
                        ctx.strokeStyle = "rgba(111,168,255," + 0.18 * (1 - d / 140) + ")";
                        ctx.lineWidth = 0.6;
                        ctx.beginPath();
                        ctx.moveTo(p.x, p.y);
                        ctx.lineTo(q.x, q.y);
                        ctx.stroke();
                    }
                }
            }
            for (var k = 0; k < particles.length; k++) {
                var pp = particles[k];
                ctx.fillStyle = "rgba(156,194,255,0.7)";
                ctx.beginPath();
                ctx.arc(pp.x, pp.y, pp.r, 0, Math.PI * 2);
                ctx.fill();
            }
            requestAnimationFrame(draw);
        }
        window.addEventListener("resize", function () {
            resize();
            initP();
        });
        resize();
        initP();
        draw();
    }

    var loc = effectiveLocale();
    document.documentElement.lang = loc === "en" ? "en" : "zh-CN";
    var fromUrl = fromQuery() != null;

    function introJsonUrls(tag) {
        var path = "/static/locales/" + tag + "/intro.json";
        var out = [path];
        try {
            var u = new URL(path, window.location.origin);
            if (out.indexOf(String(u)) < 0) out.push(String(u));
        } catch (e1) {}
        try {
            var u2 = new URL("../static/locales/" + tag + "/intro.json", window.location.href);
            if (u2 && out.indexOf(String(u2)) < 0) out.push(String(u2));
        } catch (e2) {}
        return out;
    }

    function loadLocale(tag) {
        var urls = introJsonUrls(tag);
        return new Promise(function (resolve, reject) {
            var i = 0;
            function tryNext(err) {
                if (i >= urls.length) {
                    reject(err || new Error("all attempts failed for " + tag));
                    return;
                }
                var url = urls[i++];
                fetch(url, { cache: "no-cache" })
                    .then(function (r) {
                        if (!r.ok) return Promise.reject(new Error("GET " + url + " " + r.status));
                        return r.json();
                    })
                    .then(resolve)
                    .catch(tryNext);
            }
            tryNext(null);
        });
    }

    function loadWithFallback() {
        return loadLocale(loc).catch(function (err) {
            if (loc !== "zh-CN") {
                if (typeof console !== "undefined" && console.warn) {
                    console.warn("[intro] failed to load /static/locales/" + loc + "/intro.json, falling back to zh-CN", err);
                }
                return loadLocale("zh-CN");
            }
            return Promise.reject(err);
        });
    }

    loadWithFallback()
        .then(function (data) {
            applyBundle(data);
            var applied = (data && data.locale) ? normalizeTag(data.locale) : loc;
            if (SUPPORTED.indexOf(applied) < 0) applied = "zh-CN";
            document.documentElement.lang = applied === "en" ? "en" : "zh-CN";
            if (fromUrl) {
                try {
                    localStorage.setItem(LOCALE_KEY, applied);
                } catch (e) {}
            }
            if (fromUrl && loc === "en" && applied !== "en") {
                document.body.insertAdjacentHTML(
                    "afterbegin",
                    '<p style="padding:10px 12px;margin:0;background:#1e293b;color:#fed7aa;text-align:center;font-size:0.9rem">英文文案未加载（请确认服务器上存在 <code>web/locales/en/intro.json</code> 且能访问 <code>/static/locales/en/intro.json</code>），已回退为中文。打开开发者工具 → Network/Console 可查看 <code>intro.json</code> 是否 404 或其它错误。</p>'
                );
            }
            initReveal();
            initHeroCanvas();
        })
        .catch(function () {
            document.body.insertAdjacentHTML(
                "afterbegin",
                '<p style="padding:12px;background:#3a0f0f;color:#fecaca;text-align:center">Failed to load intro copy. Refresh or check the network.</p>'
            );
        });
})();
