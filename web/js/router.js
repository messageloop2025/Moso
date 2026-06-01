/**
 * 毛竹 前端路由（参考 IOTHub）
 * 菜单切换时保留历史页面 DOM，回到该页时恢复，不重新渲染，保证操作连续性。
 */
var Router = {
    routes: {},
    currentPage: null,
    /** 当前实际路径，用于缓存 key（静态路由与 path 一致，动态路由为完整 path 如 /hosts/123） */
    currentPath: null,
    /** 主机管理上次访问路径：点击「主机管理」菜单时优先回到此路径（如 /hosts/5），否则回列表 /hosts */
    lastHostsPath: null,
    /** 关闭当前页时设置此项，离开该 path 时不写入缓存，下次进入会重新初始化 */
    _skipCacheForPath: null,

    register: function(path, handler) {
        this.routes[path] = handler;
    },

    navigate: function(path) {
        history.pushState(null, '', path);
        this.resolve();
    },

    resolve: function() {
        var path = location.pathname;
        var publicPaths = ['/login', '/register', '/forgot-password', '/reset-password', '/unlock'];
        if (!API.isLoggedIn() && publicPaths.indexOf(path) === -1) {
            history.replaceState(null, '', '/login');
            this._render('/login');
            return;
        }
        if (path === '/local' && typeof isAdmin === 'function' && !isAdmin()) {
            history.replaceState(null, '', '/dashboard');
            this._render('/dashboard');
            return;
        }
        if (API.isLoggedIn() && (path === '/login' || path === '/register' || path === '/forgot-password' || path === '/reset-password' || path === '/unlock' || path === '/')) {
            history.replaceState(null, '', '/dashboard');
            this._render('/dashboard');
            return;
        }
        this._render(path);
    },

    /** 获取或创建页面缓存容器，挂到 body 上以便在登录/主布局切换时不被销毁 */
    _ensurePageCacheContainer: function() {
        var id = 'edgeops-page-cache';
        var el = document.getElementById(id);
        if (!el) {
            el = document.createElement('div');
            el.id = id;
            el.setAttribute('aria-hidden', 'true');
            el.style.cssText = 'display:none !important; position:absolute; left:-9999px; top:0;';
            document.body.appendChild(el);
        }
        return el;
    },

    /** 将当前 page-content 的子节点移入按 path 缓存的容器 */
    _cachePage: function(path, pageContent) {
        if (!pageContent || !path || pageContent.children.length === 0) return;
        var container = this._ensurePageCacheContainer();
        var wrap = null;
        var nodes = container.querySelectorAll('[data-cache-path]');
        for (var i = 0; i < nodes.length; i++) {
            if (nodes[i].getAttribute('data-cache-path') === path) { wrap = nodes[i]; break; }
        }
        if (!wrap) {
            wrap = document.createElement('div');
            wrap.setAttribute('data-cache-path', path);
            container.appendChild(wrap);
        }
        wrap.innerHTML = '';
        while (pageContent.firstChild) wrap.appendChild(pageContent.firstChild);
    },

    /** 从缓存恢复指定 path 的 DOM 到 pageContent；存在且非空则恢复并返回 true */
    _restoreFromCache: function(path, pageContent) {
        if (!pageContent) return false;
        var container = this._ensurePageCacheContainer();
        var wrap = null;
        var nodes = container.querySelectorAll('[data-cache-path]');
        for (var i = 0; i < nodes.length; i++) {
            if (nodes[i].getAttribute('data-cache-path') === path) { wrap = nodes[i]; break; }
        }
        if (!wrap || wrap.children.length === 0) return false;
        while (wrap.firstChild) pageContent.appendChild(wrap.firstChild);
        return true;
    },

    /** 清除指定 path 的页面缓存，下次进入该 path 时会重新执行 handler 初始化 */
    clearPageCache: function(path) {
        if (!path) return;
        var container = document.getElementById('edgeops-page-cache');
        if (!container) return;
        var nodes = container.querySelectorAll('[data-cache-path]');
        for (var i = 0; i < nodes.length; i++) {
            if (nodes[i].getAttribute('data-cache-path') === path) {
                nodes[i].parentNode.removeChild(nodes[i]);
                break;
            }
        }
    },

    /** 根据实际 path 得到用于导航高亮的 route（静态即 path，动态为模式如 /hosts/:id） */
    _getRouteForPath: function(path) {
        if (this.routes[path]) return path;
        for (var route in this.routes) {
            if (this.routes.hasOwnProperty(route) && route.indexOf(':') >= 0) {
                var pattern = route.replace(/:(\w+)/g, '([^/]+)');
                if (path.match(new RegExp('^' + pattern + '$'))) return route;
            }
        }
        return path;
    },

    _render: function(path) {
        var pageContent = document.getElementById('page-content');
        var previousPath = this.currentPath;

        // 同一路径不重复渲染（例如重复点击同一菜单）
        if (path === previousPath) {
            this._updateNav();
            return;
        }
        if (path === '/hosts' || path.indexOf('/hosts/') === 0) this.lastHostsPath = path;

        // 1) 离开当前页：将当前主区内容按 path 缓存（仅当主布局存在且有内容时）；若为「关闭」则跳过缓存
        if (previousPath && pageContent && pageContent.children.length > 0 && previousPath !== this._skipCacheForPath) {
            try {
                document.dispatchEvent(new CustomEvent('edgeops-page-leave', { detail: { path: previousPath, nextPath: path } }));
            } catch (e) {}
            if (previousPath === '/hosts') {
                var hostsWrap = pageContent.querySelector('#hostsTableWrap');
                if (hostsWrap && hostsWrap.getAttribute) {
                    var p = hostsWrap.getAttribute('data-current-page');
                    if (p) try { sessionStorage.setItem('hostsListPage', p); } catch (e) {}
                }
            }
            this._cachePage(previousPath, pageContent);
        }
        if (this._skipCacheForPath) this._skipCacheForPath = null;

        // 2) 进入目标页：若该 path 有缓存则直接恢复，不调用 handler，保留之前操作状态（主机列表 /hosts 不恢复缓存，每次进入都重新拉取并保持页码）
        if (path !== '/hosts' && this._restoreFromCache(path, pageContent)) {
            this.currentPath = path;
            this.currentPage = this._getRouteForPath(path);
            this._updateNav();
            try {
                document.dispatchEvent(new CustomEvent('edgeops-page-restored', { detail: { path: path } }));
            } catch (e) {}
            return;
        }

        // 3) 无缓存：静态路由
        if (this.routes[path]) {
            this.currentPath = path;
            this.currentPage = path;
            this.routes[path]();
            this._updateNav();
            return;
        }

        // 4) 无缓存：动态路由
        for (var route in this.routes) {
            if (this.routes.hasOwnProperty(route) && route.indexOf(':') >= 0) {
                var pattern = route.replace(/:(\w+)/g, '([^/]+)');
                var match = path.match(new RegExp('^' + pattern + '$'));
                if (match) {
                    this.currentPath = path;
                    this.currentPage = route;
                    this.routes[route].apply(null, match.slice(1));
                    this._updateNav();
                    return;
                }
            }
        }

        // 5) 404
        this.currentPath = path;
        this.currentPage = null;
        if (pageContent) pageContent.innerHTML = '<div class="empty-state"><div class="icon">404</div><h3>' + t('meta.notFoundTitle') + '</h3></div>';
        this._updateNav();
    },

    _updateNav: function() {
        // 选出"最长前缀匹配"的唯一 nav-item 作为当前激活项，避免父路径与子路径同时高亮。
        // 例如 pathname=/feedback/admin/login-board 时，/feedback/admin 与 /feedback/admin/login-board
        // 都"前缀命中"，但只有最长那个才是真正的当前页。
        // 注意：仅以 location.pathname 为准（不再混入 Router.currentPage），防止跨页跳转过程中
        // currentPage 与实际 URL 不一致导致父项被错误高亮。
        var pathname = location.pathname;
        var items = document.querySelectorAll('.nav-item');
        var bestEl = null;
        var bestLen = -1;
        items.forEach(function(item) {
            var href = item.getAttribute('data-href');
            if (!href) return;
            // 严格按"路径段"匹配：完全相等，或者是 pathname 的真前缀且后面紧跟 '/'
            var isExact = (href === pathname);
            var isPrefix = (pathname.length > href.length)
                && (pathname.charAt(href.length) === '/')
                && (pathname.indexOf(href) === 0);
            if (isExact || isPrefix) {
                if (href.length > bestLen) { bestLen = href.length; bestEl = item; }
            }
        });
        items.forEach(function(item) {
            if (item === bestEl) item.classList.add('active');
            else item.classList.remove('active');
        });
    },

    init: function() {
        var self = this;
        window.addEventListener('popstate', function() { self.resolve(); });
        document.addEventListener('click', function(e) {
            var a = e.target.closest('[data-href]');
            if (a) {
                e.preventDefault();
                var href = a.getAttribute('data-href');
                if (!href) return;
                if (href === '/hosts' && self.lastHostsPath) href = self.lastHostsPath;
                self.navigate(href);
            }
        });
        // 启动时拉取服务端版本，保证侧栏等处显示与 config.VERSION 一致
        fetch('/api/version').then(function(r) { return r.json(); }).then(function(d) {
            if (d && d.version != null) API.version = d.version;
        }).catch(function() {}).then(function() {
            if (API.isLoggedIn() && typeof API.refreshSiteTimezone === 'function') {
                return API.refreshSiteTimezone().catch(function() {});
            }
        }).then(function() {
            if (API.isLoggedIn()) {
                API.startAuthRefreshTimer();
                return API.refreshAuth().catch(function() {});
            }
        }).then(function() { self.resolve(); });
    }
};
