/**
 * 毛竹 API 客户端
 */
var API = {
    token: localStorage.getItem('edgeops_token') || '',
    user: JSON.parse(localStorage.getItem('edgeops_user') || 'null'),
    _refreshPromise: null,
    _authRefreshTimer: null,
    _sessionExpiredHandled: false,

    setAuth: function(token, user) {
        this.token = token;
        this.user = user;
        localStorage.setItem('edgeops_token', token);
        localStorage.setItem('edgeops_user', JSON.stringify(user));
        this.startAuthRefreshTimer();
    },

    clearAuth: function() {
        this.stopAuthRefreshTimer();
        this.token = '';
        this.user = null;
        localStorage.removeItem('edgeops_token');
        localStorage.removeItem('edgeops_user');
    },

    isLoggedIn: function() { return !!this.token; },

    startAuthRefreshTimer: function() {
        this.stopAuthRefreshTimer();
        if (!this.token) return;
        var intervalMin = 30;
        var self = this;
        this._authRefreshTimer = setInterval(function() {
            if (!self.token) { self.stopAuthRefreshTimer(); return; }
            self.refreshAuth().catch(function() {});
        }, intervalMin * 60 * 1000);
    },

    stopAuthRefreshTimer: function() {
        if (this._authRefreshTimer) {
            clearInterval(this._authRefreshTimer);
            this._authRefreshTimer = null;
        }
    },

    refreshAuth: function() {
        if (!this.token) return Promise.resolve(false);
        var self = this;
        if (this._refreshPromise) return this._refreshPromise;
        this._refreshPromise = fetch('/api/auth/refresh', {
            method: 'POST',
            headers: { 'Authorization': 'Bearer ' + this.token, 'Content-Type': 'application/json' }
        }).then(function(r) {
            return r.json().then(function(d) { return { ok: r.ok, data: d }; });
        }).then(function(res) {
            if (res.ok && res.data && res.data.access_token) {
                self.setAuth(res.data.access_token, res.data.user || self.user);
                return true;
            }
            return false;
        }).catch(function() { return false; }).finally(function() {
            self._refreshPromise = null;
        });
        return this._refreshPromise;
    },

    handleSessionExpired: function() {
        if (this._sessionExpiredHandled) return;
        this._sessionExpiredHandled = true;
        this.clearAuth();
        var msg = (typeof t === 'function' ? t('api.sessionExpired') : 'Your session has expired. Please sign in again.');
        if (typeof showToast === 'function') showToast(msg, 'error');
        var pub = ['/login', '/register', '/forgot-password', '/reset-password', '/unlock'];
        if (typeof Router !== 'undefined' && pub.indexOf(location.pathname) === -1) {
            setTimeout(function() { Router.navigate('/login'); }, 500);
        }
        var self = this;
        setTimeout(function() { self._sessionExpiredHandled = false; }, 4000);
    },

    _onHttp401: function(retryFn, options) {
        options = options || {};
        var self = this;
        if (!options._authRetried && this.token) {
            return this.refreshAuth().then(function(ok) {
                if (ok) {
                    options._authRetried = true;
                    return retryFn(options);
                }
                self.handleSessionExpired();
                throw new Error((typeof t === 'function' ? t('api.notLoggedIn') : 'Not signed in, or the session has expired'));
            });
        }
        this.handleSessionExpired();
        throw new Error((typeof t === 'function' ? t('api.notLoggedIn') : 'Not signed in, or the session has expired'));
    },

    request: function(method, path, body, options) {
        options = options || {};
        var self = this;
        function run(reqOpts) {
            reqOpts = reqOpts || options;
            var headers = { 'Content-Type': 'application/json' };
            if (self.token) headers['Authorization'] = 'Bearer ' + self.token;
            var opts = { method: method, headers: headers };
            if (body && method !== 'GET') opts.body = JSON.stringify(body);

            return fetch('/api' + path, opts).then(function(resp) {
                return resp.json().then(function(data) {
                    if (resp.status === 401) {
                        var skipRenewal = (
                            path.indexOf('/auth/login') === 0
                            || path.indexOf('/auth/register') === 0
                            || path.indexOf('/auth/change-password') === 0
                        );
                        if (skipRenewal) {
                            var authDetail = data.detail;
                            var authFail = (typeof t === 'function' ? t('api.requestFailed') : 'Request failed');
                            var authMsg = typeof authDetail === 'string' ? authDetail : authFail;
                            throw new Error(authMsg);
                        }
                        return self._onHttp401(function(o) { return run(o); }, reqOpts);
                    }
                    if (!resp.ok) {
                        var detail = data.detail;
                        var defFail = (typeof t === 'function' ? t('api.requestFailed') : 'Request failed');
                        var msg = Array.isArray(detail)
                            ? (detail.map(function(d) { return (d && d.msg) || ''; }).filter(Boolean).join('；') || defFail)
                            : (typeof detail === 'string' ? detail : (detail && typeof detail === 'object' && detail.message ? detail.message : defFail));
                        var err = new Error(msg);
                        if (detail && typeof detail === 'object' && !Array.isArray(detail)) {
                            err.apiDetail = detail;
                            err.status = resp.status;
                        }
                        throw err;
                    }
                    return data;
                }).catch(function(e) {
                    if (e && e.message && e.message.indexOf('JSON') !== -1) {
                        throw new Error((typeof t === 'function' ? t('api.badJson') : 'The server response was not valid JSON. Please try again later.'));
                    }
                    throw e;
                });
            });
        }
        return run(options);
    },

    get: function(path) { return this.request('GET', path); },
    post: function(path, body) { return this.request('POST', path, body); },
    put: function(path, body) { return this.request('PUT', path, body); },
    delete: function(path, body) { return this.request('DELETE', path, body); },
    patch: function(path, body) { return this.request('PATCH', path, body); },
    del: function(path) { return this.request('DELETE', path); },

    getCaptcha: function() { return this.get('/auth/captcha'); },
    changePassword: function(oldPassword, newPassword) {
        return this.post('/auth/change-password', { old_password: oldPassword, new_password: newPassword });
    },
    login: function(username, password, captcha_token, captcha_answer) {
        return this.post('/auth/login', {
            username: username,
            password: password,
            captcha_token: captcha_token || '',
            captcha_answer: captcha_answer || ''
        });
    },
    register: function(username, password, display_name, captcha_token, captcha_answer) {
        return this.post('/auth/register', {
            username: username,
            password: password,
            display_name: display_name || '',
            captcha_token: captcha_token || '',
            captcha_answer: captcha_answer || ''
        });
    },
    getMe: function() { return this.get('/auth/me'); },
    /** 同步站点显示时区（window.EDGEOPS_SITE_TIMEZONE），登录后或修改 site_timezone 后应调用 */
    refreshSiteTimezone: function() {
        if (!this.token) return Promise.resolve(null);
        return this.get('/auth/me').then(function(r) {
            var tz = r.site_timezone || 'Asia/Shanghai';
            if (typeof window !== 'undefined') window.EDGEOPS_SITE_TIMEZONE = tz;
            return r;
        });
    },
    /** 同步当前用户 profile（含 skills_enabled），登录后或管理员改开关后调用 */
    refreshUserProfile: function() {
        if (!this.token) return Promise.resolve(null);
        return this.get('/auth/me').then(function(r) {
            if (r && r.user) {
                API.user = Object.assign({}, API.user || {}, r.user);
            }
            return r;
        });
    },

    listUserApiTokens: function() { return this.get('/user-api-tokens'); },
    createUserApiToken: function(name) {
        return this.post('/user-api-tokens', { name: name || '' });
    },
    deleteUserApiToken: function(id) { return this.del('/user-api-tokens/' + id); },
    forgotPassword: function(username) { return this.post('/auth/forgot-password', { username: username }); },
    resetPasswordByToken: function(token, newPassword) { return this.post('/auth/reset-password', { token: token, new_password: newPassword }); },
    requestUnlock: function(username) { return this.post('/auth/request-unlock', { username: username }); },
    unlockByToken: function(token) { return this.post('/auth/unlock-by-token', { token: token }); },
    requestRecover: function(username, email) { return this.post('/auth/request-recover', { username: username, email: email }); },
    verifyRecover: function(username, email, code) { return this.post('/auth/verify-recover', { username: username, email: email, code: code }); },
    recoverComplete: function(tempToken, action, newPassword) { return this.post('/auth/recover-complete', { temp_token: tempToken, action: action, new_password: newPassword || '' }); },
    sendBindEmailCode: function(email) { return this.post('/auth/send-bind-email-code', { email: email }); },
    verifyBindEmail: function(email, code) { return this.post('/auth/verify-bind-email', { email: email, code: code }); },
    unbindEmail: function() { return this.post('/auth/unbind-email', {}); },

    listUsers: function(params) { var q = params ? '?' + new URLSearchParams(params).toString() : ''; return this.get('/users' + q); },
    searchUsers: function(params) { var q = params && Object.keys(params).length ? '?' + new URLSearchParams(params).toString() : ''; return this.get('/users/search' + q); },
    createUser: function(data) { return this.post('/users', data); },
    updateUser: function(id, data) { return this.put('/users/' + id, data); },
    deleteUser: function(id) { return this.del('/users/' + id); },
    resetPassword: function(id, password) { return this.post('/users/' + id + '/reset-password', { password: password }); },
    unlockUser: function(id) { return this.post('/users/' + id + '/unlock'); },

    listHosts: function(params) { var q = params && Object.keys(params).length ? '?' + new URLSearchParams(params).toString() : ''; return this.get('/hosts' + q); },
    searchHosts: function(params) { var q = params && Object.keys(params).length ? '?' + new URLSearchParams(params).toString() : ''; return this.get('/hosts/search' + q); },
    getHost: function(id) { return this.get('/hosts/' + id); },
    hostStats: function() { return this.get('/hosts/stats'); },
    getDashboardStats: function() { return this.get('/dashboard/stats'); },
    checkHostDuplicate: function(host, port) {
        var q = new URLSearchParams({ host: host || '' });
        if (port != null && port !== '') q.set('port', String(port));
        return this.get('/hosts/check-duplicate?' + q.toString());
    },
    createHost: function(data) { return this.post('/hosts', data); },
    updateHost: function(id, data) { return this.put('/hosts/' + id, data); },
    deleteHost: function(id) { return this.del('/hosts/' + id); },
    shareHost: function(hostId, data) { return this.post('/hosts/' + parseInt(hostId, 10) + '/shares', data || {}); },
    listHostShares: function(hostId) { return this.get('/hosts/' + parseInt(hostId, 10) + '/shares'); },
    revokeHostShare: function(hostId, userId) { return this.del('/hosts/' + parseInt(hostId, 10) + '/shares/' + parseInt(userId, 10)); },
    listReceivedHostShares: function() { return this.get('/hosts/shares/received'); },
    listSentHostShares: function() { return this.get('/hosts/shares/sent'); },
    executeHost: function(id, command, timeout) { return this.post('/hosts/' + id + '/execute', { command: command || '', timeout: timeout || 30 }); },
    checkHostType: function(id) { return this.post('/hosts/' + id + '/check-type'); },
    listHostTags: function() { return this.get('/host-tags'); },
    createHostTag: function(data) { return this.post('/host-tags', data || {}); },
    updateHostTag: function(id, data) { return this.put('/host-tags/' + parseInt(id, 10), data || {}); },
    deleteHostTag: function(id) { return this.del('/host-tags/' + parseInt(id, 10)); },
    getHostTagsForHost: function(hostId) { return this.get('/host-tags/hosts/' + parseInt(hostId, 10)); },
    setHostTagsForHost: function(hostId, tagIds) { return this.put('/host-tags/hosts/' + parseInt(hostId, 10), { tag_ids: tagIds || [] }); },

    listCredentials: function(params) { var q = params && Object.keys(params).length ? '?' + new URLSearchParams(params).toString() : ''; return this.get('/credentials' + q); },
    getCredential: function(id) { return this.get('/credentials/' + id); },
    createCredential: function(data) { return this.post('/credentials', data); },
    updateCredential: function(id, data) { return this.put('/credentials/' + id, data); },
    deleteCredential: function(id) { return this.del('/credentials/' + id); },
    generateKey: function(keyType, keyBits) { return this.post('/credentials/generate-key', { key_type: keyType || 'RSA', key_bits: keyBits || 2048 }); },

    listHostGroups: function() { return this.get('/host-groups'); },
    hostGroupsTree: function() { return this.get('/host-groups/tree'); },
    getHostGroup: function(id) { return this.get('/host-groups/' + id); },
    createHostGroup: function(data) { return this.post('/host-groups', data); },
    updateHostGroup: function(id, data) { return this.put('/host-groups/' + id, data); },
    deleteHostGroup: function(id) { return this.del('/host-groups/' + id); },
    getGroupHosts: function(id) { return this.get('/host-groups/' + id + '/hosts'); },
    addHostsToGroup: function(groupId, hostIds) { return this.post('/host-groups/' + groupId + '/hosts', { host_ids: hostIds }); },
    removeHostFromGroup: function(groupId, hostId) { return this.del('/host-groups/' + groupId + '/hosts/' + hostId); },

    listMaintenanceHistory: function(params) { var q = params && Object.keys(params).length ? '?' + new URLSearchParams(params).toString() : ''; return this.get('/maintenance-history' + q); },
    getMaintenanceItem: function(id) { return this.get('/maintenance-history/' + id); },
    createMaintenance: function(data) { return this.post('/maintenance-history', data); },
    updateMaintenance: function(id, data) { return this.put('/maintenance-history/' + id, data); },
    deleteMaintenance: function(id) { return this.del('/maintenance-history/' + id); },

    listBestPractices: function(params) { var q = params && Object.keys(params).length ? '?' + new URLSearchParams(params).toString() : ''; return this.get('/best-practices' + q); },
    getBestPracticeCategories: function() { return this.get('/best-practices/categories'); },
    getBestPractice: function(id) { return this.get('/best-practices/' + id); },
    createBestPractice: function(data) { return this.post('/best-practices', data); },
    updateBestPractice: function(id, data) { return this.put('/best-practices/' + id, data); },
    deleteBestPractice: function(id) { return this.del('/best-practices/' + id); },

    listSkills: function() { return this.get('/skills'); },

    listBatches: function(params) { var q = params && Object.keys(params).length ? '?' + new URLSearchParams(params).toString() : ''; return this.get('/batch' + q); },
    getBatch: function(id) { return this.get('/batch/' + id); },
    createBatch: function(data) { return this.post('/batch', data); },
    cancelBatch: function(id) { return this.post('/batch/' + id + '/cancel'); },
    retryBatch: function(id) { return this.post('/batch/' + id + '/retry'); },
    clearBatches: function() { return this.del('/batch/clear'); },
    getBatchExport: function(limit) { var q = limit != null ? '?limit=' + parseInt(limit, 10) : ''; return this.get('/batch/export' + q); },

    listTriggeredTasks: function() { return this.get('/triggered-tasks'); },
    createTriggeredTask: function(data) { return this.post('/triggered-tasks', data); },
    getTriggeredTask: function(id) { return this.get('/triggered-tasks/' + id); },
    updateTriggeredTask: function(id, data) { return this.patch('/triggered-tasks/' + id, data); },
    deleteTriggeredTask: function(id) { return this.del('/triggered-tasks/' + id); },
    listTriggeredTaskExposed: function() { return this.get('/triggered-tasks/exposed'); },
    listTriggeredTaskRuns: function(taskId, params) { var q = params && Object.keys(params).length ? '?' + new URLSearchParams(params).toString() : ''; return this.get('/triggered-tasks/' + taskId + '/runs' + q); },
    exportTriggeredTaskRuns: function(taskId, params) { var q = params && Object.keys(params).length ? '?' + new URLSearchParams(params).toString() : ''; return this.get('/triggered-tasks/' + taskId + '/runs/export' + q); },
    clearTriggeredTaskRuns: function(taskId) { return this.request('DELETE', '/triggered-tasks/' + taskId + '/runs'); },
    getTriggeredTaskRunMessages: function(taskId, runId) { return this.get('/triggered-tasks/' + taskId + '/runs/' + runId + '/messages'); },
    listTriggeredTaskAllRuns: function(params) { var q = params && Object.keys(params).length ? '?' + new URLSearchParams(params).toString() : ''; return this.get('/triggered-tasks/all-runs' + q); },
    addTriggeredTaskExpose: function(taskId, data) { return this.post('/triggered-tasks/' + taskId + '/expose', data); },
    removeTriggeredTaskExpose: function(taskId, code) { return this.del('/triggered-tasks/' + taskId + '/expose?code=' + encodeURIComponent(code)); },

    listScheduledTasks: function() { return this.get('/scheduled-tasks'); },
    createScheduledTask: function(data) { return this.post('/scheduled-tasks', data); },
    getScheduledTask: function(id) { return this.get('/scheduled-tasks/' + id); },
    updateScheduledTask: function(id, data) { return this.patch('/scheduled-tasks/' + id, data); },
    deleteScheduledTask: function(id) { return this.del('/scheduled-tasks/' + id); },
    listScheduledTaskRuns: function(taskId, params) { var q = params && Object.keys(params).length ? '?' + new URLSearchParams(params).toString() : ''; return this.get('/scheduled-tasks/' + taskId + '/runs' + q); },
    exportScheduledTaskRuns: function(taskId, params) { var q = params && Object.keys(params).length ? '?' + new URLSearchParams(params).toString() : ''; return this.get('/scheduled-tasks/' + taskId + '/runs/export' + q); },
    clearScheduledTaskRuns: function(taskId) { return this.request('DELETE', '/scheduled-tasks/' + taskId + '/runs'); },
    getScheduledTaskRunMessages: function(taskId, runId) { return this.get('/scheduled-tasks/' + taskId + '/runs/' + runId + '/messages'); },
    listScheduledTaskAllRuns: function(params) { var q = params && Object.keys(params).length ? '?' + new URLSearchParams(params).toString() : ''; return this.get('/scheduled-tasks/all-runs' + q); },
    runScheduledTaskNow: function(taskId) { return this.post('/scheduled-tasks/' + taskId + '/run-now'); },

    listLocalSessions: function() { return this.get('/local/sessions'); },
    createLocalSession: function(title) {
        var defTitle = (typeof t === 'function' ? t('ai.local.sessionNew') : 'Local session');
        return this.post('/local/sessions?title=' + encodeURIComponent(title != null && title !== '' ? title : defTitle));
    },
    getLocalSessionLogs: function(sessionId) { return this.get('/local/sessions/' + sessionId + '/logs'); },
    localExecute: function(command, timeout, sessionId, cwd) {
        var b = { command: command || '', timeout: timeout || 60 };
        if (sessionId != null) b.session_id = sessionId;
        if (cwd) b.cwd = cwd;
        return this.post('/local/execute', b);
    },
    localRunScript: function(code, scriptPath, timeout, sessionId) {
        var b = { code: code || '', script_path: scriptPath || '', timeout: timeout || 120 };
        if (sessionId != null) b.session_id = sessionId;
        return this.post('/local/run-script', b);
    },
    localFsList: function(path) { return this.get('/local/fs/list?path=' + encodeURIComponent(path || '')); },
    localFsRead: function(path) { return this.post('/local/fs/read', { path: path || '', encoding: 'utf-8' }); },
    localFsWrite: function(path, content) { return this.post('/local/fs/write', { path: path || '', content: content || '' }); },
    localFsMkdir: function(path) { return this.post('/local/fs/mkdir', { path: path || '' }); },
    localFsDelete: function(path) {
        var url = '/api/local/fs/delete?path=' + encodeURIComponent(path || '');
        var headers = {};
        if (this.token) headers['Authorization'] = 'Bearer ' + this.token;
        return fetch(url, { method: 'DELETE', headers: headers }).then(function(r) {
            return r.json().then(function(d) {
                if (r.status === 401) return API._onHttp401(function() { return API.localFsDelete(path); }, {});
                if (!r.ok) throw new Error(d.detail || (typeof t === 'function' ? t('api.deleteFailed') : 'Delete failed'));
                return d;
            });
        });
    },
    localFsRename: function(path, newPath) {
        return this.post('/local/fs/rename', { path: path || '', new_path: newPath || '' });
    },
    localFsUpload: function(path, file) {
        var form = new FormData();
        form.append('file', file);
        if (path != null && path !== '') form.append('path', path);
        var headers = {};
        if (this.token) headers['Authorization'] = 'Bearer ' + this.token;
        return fetch('/api/local/fs/upload', { method: 'POST', headers: headers, body: form }).then(function(r) {
            return r.json().then(function(d) {
                if (r.status === 401) return API._onHttp401(function() { return API.localFsUpload(path, file); }, {});
                if (!r.ok) throw new Error(d.detail || (typeof t === 'function' ? t('api.uploadFailed') : 'Upload failed'));
                return d;
            });
        });
    },
    localFsDownload: function(path, filename) {
        var url = '/api/local/fs/download?path=' + encodeURIComponent(path || '');
        var headers = {};
        if (this.token) headers['Authorization'] = 'Bearer ' + this.token;
        return fetch(url, { headers: headers }).then(function(r) {
            if (!r.ok) return r.json().then(function(d) { throw new Error(d.detail || (typeof t === 'function' ? t('api.downloadFailed') : 'Download failed')); });
            return r.blob().then(function(blob) {
                var a = document.createElement('a');
                a.href = URL.createObjectURL(blob);
                a.download = filename || (typeof path === 'string' ? path.split(/[/\\]/).pop() : 'download') || 'download';
                a.click();
                URL.revokeObjectURL(a.href);
            });
        });
    },
    getLocalBuffer: function(slot) { return this.get('/local/buffer' + (slot != null ? '?slot=' + parseInt(slot, 10) : '')); },

    getTerminalBuffer: function(slot) {
        var q = '/terminal/buffer';
        if (slot != null) q += '?slot=' + parseInt(slot, 10);
        return this.get(q);
    },
    terminalSend: function(text, slot) {
        var body = { text: text };
        if (slot != null) body.slot = slot;
        return this.post('/terminal/send', body);
    },
    terminalList: function() { return this.get('/terminal/list'); },
    terminalPendingConsoleCreations: function(scopeId) {
        var q = scopeId ? ('?scope_id=' + encodeURIComponent(scopeId)) : '';
        return this.get('/terminal/pending-console-creations' + q);
    },

    getAIConfig: function(userId) { return this.get('/ai/config' + (userId != null ? '?user_id=' + parseInt(userId, 10) : '')); },
    updateAIConfig: function(data) { return this.post('/ai/config', data); },
    listAIProfiles: function(userId) { return this.get('/ai/profiles' + (userId != null ? '?user_id=' + parseInt(userId, 10) : '')); },
    getAIProfile: function(id, userId) {
        var q = userId != null ? ('?user_id=' + parseInt(userId, 10)) : '';
        return this.get('/ai/profiles/' + parseInt(id, 10) + q);
    },
    createAIProfile: function(data) { return this.post('/ai/profiles', data); },
    updateAIProfile: function(id, data) { return this.put('/ai/profiles/' + parseInt(id, 10), data); },
    deleteAIProfile: function(id, userId) {
        var q = userId != null ? ('?user_id=' + parseInt(userId, 10)) : '';
        return this.delete('/ai/profiles/' + parseInt(id, 10) + q);
    },
    activateAIProfile: function(id, userId) {
        var q = userId != null ? ('?user_id=' + parseInt(userId, 10)) : '';
        return this.post('/ai/profiles/' + parseInt(id, 10) + '/activate' + q, {});
    },
    exportAIProfiles: function(profileId, userId) {
        var q = [];
        if (profileId != null) q.push('profile_id=' + parseInt(profileId, 10));
        if (userId != null) q.push('user_id=' + parseInt(userId, 10));
        return this.get('/ai/profiles/export' + (q.length ? '?' + q.join('&') : ''));
    },
    importAIProfiles: function(data) { return this.post('/ai/profiles/import', data); },
    getUserMailConfig: function() { return this.get('/user-mail-config'); },
    updateUserMailConfig: function(data) { return this.put('/user-mail-config', data); },

    // 搜索服务（GitHub / Aliyun IQS / ...）配置
    listSearchProviders: function() { return this.get('/search-config/providers'); },
    getMySearchConfigs: function() { return this.get('/search-config'); },
    updateMySearchConfig: function(provider, data) { return this.put('/search-config/' + encodeURIComponent(provider), data); },
    deleteMySearchConfig: function(provider) { return this.delete('/search-config/' + encodeURIComponent(provider)); },
    testMySearchConfig: function(provider) { return this.post('/search-config/' + encodeURIComponent(provider) + '/test', {}); },
    runSearchProbe: function(provider, data) { return this.post('/search-config/' + encodeURIComponent(provider) + '/search', data); },

    listUserMcpServers: function() { return this.get('/user-mcp-servers'); },
    createUserMcpServer: function(data) { return this.post('/user-mcp-servers', data); },
    updateUserMcpServer: function(id, data) { return this.put('/user-mcp-servers/' + id, data); },
    deleteUserMcpServer: function(id) { return this.delete('/user-mcp-servers/' + id); },
    testUserMcpServer: function(id) { return this.post('/user-mcp-servers/' + id + '/test', {}); },
    importUserMcpServers: function(data) { return this.post('/user-mcp-servers/import', data); },
    exportUserMcpServers: function(opts) {
        var q = [];
        opts = opts || {};
        if (opts.include_disabled === false) q.push('include_disabled=false');
        if (opts.include_edgeops_meta === false) q.push('include_edgeops_meta=false');
        var qs = q.length ? '?' + q.join('&') : '';
        return this.get('/user-mcp-servers/export' + qs);
    },
    refreshUserMcpServerTools: function(id) { return this.post('/user-mcp-servers/' + id + '/refresh-tools', {}); },

    getUserSkillsStatus: function() { return this.get('/user-skills/status'); },
    listUserSkills: function() { return this.get('/user-skills'); },
    createUserSkill: function(data) { return this.post('/user-skills', data); },
    getUserSkill: function(id) { return this.get('/user-skills/' + id); },
    updateUserSkill: function(id, data) { return this.put('/user-skills/' + id, data); },
    deleteUserSkill: function(id, removeFiles) {
        var qs = removeFiles ? '?remove_files=true' : '';
        return this.delete('/user-skills/' + id + qs);
    },
    scanUserSkills: function() { return this.post('/user-skills/scan', {}); },
    getUserSkillsTemplate: function(name, description) {
        var q = [];
        if (name) q.push('name=' + encodeURIComponent(name));
        if (description) q.push('description=' + encodeURIComponent(description || ''));
        var qs = q.length ? '?' + q.join('&') : '';
        return this.get('/user-skills/template' + qs);
    },
    exportUserSkills: function(opts) {
        opts = opts || {};
        var q = opts.include_disabled === false ? '?include_disabled=false' : '';
        return this.get('/user-skills/export' + q);
    },
    importUserSkills: function(data, overwrite) {
        return this.post('/user-skills/import', { data: data, overwrite: !!overwrite });
    },

    // 登录页公开开关（不带鉴权）：决定登录页是否展示「留言板」「公开留言区」
    getPublicLoginWidgets: function() { return this.get('/public/login-widgets'); },
    // 登录页匿名留言板（公开接口，不带鉴权）
    getLoginBoardCaptcha: function() { return this.get('/login-board/captcha'); },
    getLoginBoardPublic: function(limit) { return this.get('/login-board?limit=' + (limit || 30)); },
    postLoginBoard: function(data) { return this.post('/login-board', data); },
    // 管理员后台
    adminListLoginBoard: function(params) {
        var qs = '';
        if (params) {
            var arr = [];
            if (params.status) arr.push('status=' + encodeURIComponent(params.status));
            if (params.limit != null) arr.push('limit=' + params.limit);
            if (params.offset != null) arr.push('offset=' + params.offset);
            if (arr.length) qs = '?' + arr.join('&');
        }
        return this.get('/admin/login-board' + qs);
    },
    adminReplyLoginBoard: function(id, data) { return this.post('/admin/login-board/' + id + '/reply', data); },
    adminPatchLoginBoardRaw: function(id, data) { return this.request('PATCH', '/admin/login-board/' + id, data); },
    adminDeleteLoginBoard: function(id) { return this.delete('/admin/login-board/' + id); },

    // 用户反馈
    listMyFeedback: function() { return this.get('/feedback'); },
    submitFeedback: function(data) { return this.post('/feedback', data); },
    getFeedback: function(id) { return this.get('/feedback/' + id); },
    patchFeedback: function(id, data) { return this.request('PATCH', '/feedback/' + id, data); },
    withdrawFeedback: function(id) { return this.delete('/feedback/' + id); },
    // 管理员
    adminListFeedback: function(params) {
        var qs = '';
        if (params) {
            var arr = [];
            if (params.filter) arr.push('filter=' + encodeURIComponent(params.filter));
            if (params.limit != null) arr.push('limit=' + params.limit);
            if (params.offset != null) arr.push('offset=' + params.offset);
            if (arr.length) qs = '?' + arr.join('&');
        }
        return this.get('/admin/feedback' + qs);
    },
    adminGetFeedback: function(id) { return this.get('/admin/feedback/' + id); },
    adminReplyFeedback: function(id, data) { return this.post('/admin/feedback/' + id + '/reply', data); },
    adminIgnoreFeedback: function(id) { return this.post('/admin/feedback/' + id + '/ignore', {}); },
    adminReopenFeedback: function(id) { return this.post('/admin/feedback/' + id + '/reopen', {}); },
    adminMarkReadFeedback: function(id) { return this.post('/admin/feedback/' + id + '/mark-read', {}); },
    adminMarkAllReadFeedback: function() { return this.post('/admin/feedback/mark-all-read', {}); },
    adminUpdateFeedbackReply: function(rid, data) { return this.request('PATCH', '/admin/feedback/replies/' + rid, data); },
    adminDeleteFeedbackReply: function(rid) { return this.delete('/admin/feedback/replies/' + rid); },

    getSystemAIConfig: function() { return this.get('/ai/config/system'); },
    applySystemAIConfigToUser: function(userId) { return this.post('/ai/config/apply-system?user_id=' + parseInt(userId, 10)); },
    getTrialStatus: function(userId) { var q = (userId != null) ? ('?user_id=' + parseInt(userId, 10)) : ''; return this.get('/ai/config/trial' + q); },
    deleteMyAccount: function(password, confirm) { return this.post('/users/me/delete', { password: password, confirm: confirm }); },
    resetTrialUsage: function(userId) { return this.post('/ai/config/trial/reset?user_id=' + parseInt(userId, 10)); },
    unlockTrialMode: function(userId) { return this.post('/ai/config/trial/unlock?user_id=' + parseInt(userId, 10)); },
    listAISessions: function(hostId, scope) { var q = []; if (hostId != null) q.push('host_id=' + hostId); if (scope) q.push('scope=' + encodeURIComponent(scope)); return this.get('/ai/sessions' + (q.length ? '?' + q.join('&') : '')); },
    createAISession: function(title, hostId, scope) { var q = 'title=' + encodeURIComponent(title || 'default'); if (hostId != null) q += '&host_id=' + hostId; if (scope) q += '&scope=' + encodeURIComponent(scope); return this.post('/ai/sessions?' + q); },
    getAISession: function(id) { return this.get('/ai/sessions/' + id); },
    updateAISession: function(id, data) { return this.request('PATCH', '/ai/sessions/' + id, data); },
    updateAISessionMessage: function(sessionId, messageId, data) { return this.request('PATCH', '/ai/sessions/' + sessionId + '/messages/' + messageId, data || {}); },
    summarizeAISessionTitle: function(id) {
        id = parseInt(id, 10);
        if (!id || isNaN(id)) return Promise.reject(new Error((typeof t === 'function' ? t('api.invalidSession') : 'Invalid session')));
        return this.post('/ai/sessions/' + id + '/summarize-title');
    },
    deleteAISession: function(id) { return this.del('/ai/sessions/' + id); },
    clearAISessions: function(hostId) { return this.post('/ai/sessions/clear' + (hostId != null ? '?host_id=' + hostId : '')); },
    getSessionPrompt: function(sessionId) { return this.get('/ai/sessions/' + sessionId + '/prompt'); },
    updateSessionPrompt: function(sessionId, prompt) { return this.request('PUT', '/ai/sessions/' + sessionId + '/prompt', { prompt: prompt || '' }); },
    summarizeSessionPrompt: function(sessionId, action) { action = action || 'replace'; return this.post('/ai/sessions/' + sessionId + '/prompt/summarize?action=' + encodeURIComponent(action)); },
    getHostPrompt: function(hostId) { return this.get('/ai/hosts/' + hostId + '/prompt'); },
    updateHostPrompt: function(hostId, prompt) { return this.request('PUT', '/ai/hosts/' + hostId + '/prompt', { prompt: prompt || '' }); },
    summarizeHostPrompt: function(hostId, action) { action = action || 'replace'; return this.post('/ai/hosts/' + hostId + '/prompt/summarize?action=' + encodeURIComponent(action)); },
    listSessionToolResultCaches: function(sessionId, params) {
        var q = params && Object.keys(params).length ? ('?' + new URLSearchParams(params).toString()) : '';
        return this.get('/ai/sessions/' + sessionId + '/tool-result-caches' + q);
    },
    getSessionToolResultCache: function(sessionId, cacheId) {
        return this.get('/ai/sessions/' + sessionId + '/tool-result-caches/' + cacheId);
    },
    clearSessionMessages: function(sessionId, opts) { return this.request('DELETE', '/ai/sessions/' + sessionId + '/messages', opts || { clear: 'all' }); },
    chat: function(message, sessionId, hostId, scope, terminalScopeId) { var b = { message: message, session_id: sessionId || null }; if (hostId != null) b.host_id = hostId; if (scope) b.scope = scope; if (terminalScopeId) b.terminal_scope_id = terminalScopeId; if (typeof I18n !== 'undefined' && I18n.getEffectiveLocale) { var _ul = I18n.getEffectiveLocale(); if (_ul) b.ui_locale = _ul; } return this.post('/ai/chat', b); },

    /** 上传一个聊天附件（图片/文本/Markdown 等），返回 { success, attachment: { uuid, name, mime, size, kind, url } } */
    uploadChatAttachment: function(file, sessionId) {
        var fd = new FormData();
        fd.append('file', file);
        if (sessionId != null) fd.append('session_id', String(sessionId));
        var headers = {};
        if (this.token) headers['Authorization'] = 'Bearer ' + this.token;
        return fetch('/api/ai/attachments', { method: 'POST', headers: headers, body: fd }).then(function(resp) {
            return resp.text().then(function(t) {
                var data = {};
                try { data = t ? JSON.parse(t) : {}; } catch (e) {}
                if (!resp.ok) {
                    var defUp = (typeof t === 'function' ? t('api.uploadFailedWithStatus', { status: resp.status }) : 'Upload failed (' + resp.status + ')');
                    var msg = (data && data.detail) ? data.detail : defUp;
                    throw new Error(typeof msg === 'string' ? msg : (typeof t === 'function' ? t('api.uploadFailed') : 'Upload failed'));
                }
                return data;
            });
        });
    },
    /** 把已上传的附件绑定到会话（及可选消息 id）。 */
    bindChatAttachments: function(uuids, sessionId, messageId) {
        return this.post('/ai/attachments/bind', { uuids: uuids || [], session_id: sessionId, message_id: messageId || null });
    },
    deleteChatAttachment: function(uuid) { return this.request('DELETE', '/ai/attachments/' + encodeURIComponent(uuid)); },
    listChatAttachments: function(sessionId) { var q = sessionId != null ? ('?session_id=' + encodeURIComponent(sessionId)) : ''; return this.get('/ai/attachments' + q); },
    /**
     * 构造可直接用于 <img src>/<a href> 的附件 URL，自动附带当前登录 token 作为 query 参数。
     * 后端 /api/ai/attachments/{uuid} 同时支持 Authorization header 和 ?token=xxx 鉴权。
     */
    buildChatAttachmentUrl: function(uuid) {
        if (!uuid) return '';
        var url = '/api/ai/attachments/' + encodeURIComponent(uuid);
        if (this.token) url += '?token=' + encodeURIComponent(this.token);
        return url;
    },

    // ── AI 成果物（artifacts）──
    buildArtifactDownloadUrl: function(uuid) {
        if (!uuid) return '';
        var url = '/api/ai/artifacts/' + encodeURIComponent(uuid) + '/download';
        if (this.token) url += '?token=' + encodeURIComponent(this.token);
        return url;
    },
    buildArtifactFileUrl: function(uuid, relPath) {
        if (!uuid) return '';
        var url = '/api/ai/artifacts/' + encodeURIComponent(uuid) + '/file?path=' + encodeURIComponent(relPath || '');
        if (this.token) url += '&token=' + encodeURIComponent(this.token);
        return url;
    },
    /**
     * 路径式 file URL：`/api/ai/artifacts/<uuid>/files/<rel/path>`。
     * 当 URL 本身需要被浏览器当作 base 解析子资源时（iframe src 直链、`<a target=_blank>`、
     * 新窗口打开），用这个；HTML 内的 `./libs/x.js` 会被浏览器自动解析为
     * `/api/ai/artifacts/<uuid>/files/libs/x.js`，命中后端 catchall 路由。
     * 反过来：`buildArtifactFileUrl`（query 形式）适合 fetch / 编程读取场景。
     */
    buildArtifactPathUrl: function(uuid, relPath) {
        if (!uuid) return '';
        var p = String(relPath || '').replace(/\\/g, '/').replace(/^\/+/, '');
        // 把每一段单独 encodeURIComponent，再用 / 拼回 —— 避免把斜杠也 encode 掉
        var encoded = p.split('/').map(function(seg) { return encodeURIComponent(seg); }).join('/');
        var url = '/api/ai/artifacts/' + encodeURIComponent(uuid) + '/files/' + encoded;
        if (this.token) url += '?token=' + encodeURIComponent(this.token);
        return url;
    },
    getArtifactMeta: function(uuid) {
        return this.request('GET', '/ai/artifacts/' + encodeURIComponent(uuid) + '/meta');
    },
    listArtifacts: function(sessionId) {
        var q = (sessionId != null) ? ('?session_id=' + encodeURIComponent(sessionId)) : '';
        return this.request('GET', '/ai/artifacts' + q);
    },
    deleteArtifact: function(uuid) {
        return this.request('DELETE', '/ai/artifacts/' + encodeURIComponent(uuid));
    },
    pushAIRuntimeControl: function(sessionId, payload) {
        return this.request('POST', '/ai/sessions/' + encodeURIComponent(sessionId) + '/runtime-control', {
            action: (payload && payload.action) || 'supplement',
            message: (payload && payload.message) || ''
        });
    },

    /** 流式聊天（SSE）：通过 onEvent 接收 content/action/error；可选 options.signal 用于中断；options.hostId 用于主机维度会话。*/
    chatStream: function(message, sessionId, onEvent, options) {
        var self = this;
        options = options || {};
        function runStream(reqOpts) {
            reqOpts = reqOpts || options;
            var headers = { 'Content-Type': 'application/json' };
            if (self.token) headers['Authorization'] = 'Bearer ' + self.token;
            var body = { message: message, session_id: sessionId || null };
            if (reqOpts.hostId != null) body.host_id = reqOpts.hostId;
            if (reqOpts.scope) body.scope = reqOpts.scope;
            if (reqOpts.terminalScopeId) body.terminal_scope_id = reqOpts.terminalScopeId;
            if (reqOpts.preferredTerminalSlot != null) body.preferred_terminal_slot = reqOpts.preferredTerminalSlot;
            if (reqOpts.contextHostId != null) body.context_host_id = reqOpts.contextHostId;
            if (reqOpts.attachmentUuids && reqOpts.attachmentUuids.length) body.attachment_uuids = reqOpts.attachmentUuids.slice();
            if (typeof I18n !== 'undefined' && I18n.getEffectiveLocale) { var _ul2 = I18n.getEffectiveLocale(); if (_ul2) body.ui_locale = _ul2; }
            var fetchOpts = { method: 'POST', headers: headers, body: JSON.stringify(body) };
            if (reqOpts.signal) fetchOpts.signal = reqOpts.signal;
            return fetch('/api/ai/chat', fetchOpts).then(function(resp) {
                if (resp.status === 401) {
                    return self._onHttp401(function(o) { return runStream(o); }, reqOpts);
                }
            if (!resp.ok) {
                return resp.text().then(function(txt) {
                    var msg = (typeof t === 'function' ? t('api.requestFailedWithStatus', { status: resp.status }) : 'Request failed (' + resp.status + ')');
                    try {
                        var d = JSON.parse(txt);
                        if (d.detail) {
                            if (typeof d.detail === 'string') msg = d.detail;
                            else if (Array.isArray(d.detail) && d.detail[0] && d.detail[0].msg) msg = d.detail[0].msg;
                        }
                    } catch (e) {}
                    throw new Error(msg);
                });
            }
            var reader = resp.body.getReader();
            var decoder = new TextDecoder();
            var buf = '';
            var streamSessionId = sessionId;
            var sawDone = false;
            var dispatchQueue = Promise.resolve();

            function dispatchSseData(data) {
                if (!onEvent) return;
                if (data.session_id != null) {
                    streamSessionId = data.session_id;
                    onEvent({ session_id: data.session_id });
                } else if (data.error) onEvent({ error: data.error });
                else if (data.content) onEvent({ content: data.content });
                else if (data.tool_stream != null) onEvent({ tool_stream: data.tool_stream, tool: data.tool });
                else if (data.cot) onEvent({ cot: data.cot });
                else if (data.action) onEvent({
                    action: data.action,
                    tool: data.tool,
                    args: data.args,
                    result_preview: data.result_preview,
                    seconds: data.seconds,
                    wait_elapsed: data.wait_elapsed,
                    wait_remaining: data.wait_remaining,
                    reason: data.reason
                });
                else if (data.ui_action) onEvent({ ui_action: data.ui_action });
                else if (data.assistant_continue) onEvent({
                    assistant_continue: data.assistant_continue,
                    requires_user_confirm: !!data.requires_user_confirm
                });
                else if (data.runtime_control) onEvent({ runtime_control: data.runtime_control });
                else if (data.stream_status) onEvent({ stream_status: data.stream_status });
            }

            /** content / cot 若在同一 read 回调里连续派发，主线程来不及重绘会像「整段闪现」；插入 setTimeout(0) 保持顺序并让出渲染时机 */
            function enqueueDispatch(data) {
                var defer = !!(data && (data.content || data.cot));
                dispatchQueue = dispatchQueue.then(function() {
                    if (defer) {
                        return new Promise(function(resolve) {
                            setTimeout(function() {
                                try { dispatchSseData(data); } catch (e) {}
                                resolve();
                            }, 0);
                        });
                    }
                    try { dispatchSseData(data); } catch (e) {}
                    return Promise.resolve();
                });
            }

            function pump() {
                return reader.read().then(function(result) {
                    if (result.done) {
                        if (!sawDone) {
                            throw new Error((typeof t === 'function' ? t('api.streamEndedUnexpectedly') : 'AI stream ended before completion.'));
                        }
                        return dispatchQueue.then(function() { return streamSessionId; });
                    }
                    buf += decoder.decode(result.value, { stream: true });
                    var lines = buf.split('\n');
                    buf = lines.pop() || '';
                    var hitDone = false;
                    for (var li = 0; li < lines.length; li++) {
                        var line = lines[li];
                        if (line.indexOf('data: ') !== 0) continue;
                        var payload = line.slice(6);
                        if (payload === '[DONE]') {
                            sawDone = true;
                            hitDone = true;
                            continue;
                        }
                        try {
                            enqueueDispatch(JSON.parse(payload));
                        } catch (e) {}
                    }
                    if (hitDone) {
                        return dispatchQueue.then(function() { return streamSessionId; });
                    }
                    return dispatchQueue.then(function() { return pump(); });
                });
            }
            return pump();
            });
        }
        return runStream(options);
    },

    getSettings: function() { return this.get('/settings'); },
    updateSetting: function(key, value) { return this.post('/settings', { key: key, value: value }); },

    listLogs: function(params) { var q = params && Object.keys(params).length ? '?' + new URLSearchParams(params).toString() : ''; return this.get('/logs' + q); },
    clearLogs: function(userId) { var q = userId != null ? '?user_id=' + parseInt(userId, 10) : ''; return this.del('/logs' + q); },
    getLogsExport: function(limit) { var q = limit != null ? '?limit=' + parseInt(limit, 10) : ''; return this.get('/logs/export' + q); },

    /* 文件系统（web/fs） */
    fsList: function(path) { return this.get('/fs/list' + (path ? '?path=' + encodeURIComponent(path) : '')); },
    fsRead: function(path) { return this.get('/fs/read?path=' + encodeURIComponent(path)); },
    /**
     * 路径式 inline 直链：`/api/fs/file/<rel/path>?token=<jwt>`。
     *  - HTML 预览改写相对引用 → 让 iframe srcdoc 内 `./libs/x.js` 走得通；
     *  - "下载"按钮：传 download=1 → 强制 attachment。
     *  - 普通用户限自己的 web/fs/<username>；管理员可传 username 查看他人。
     */
    buildFsFileUrl: function(relPath, opts) {
        var p = String(relPath || '').replace(/\\/g, '/').replace(/^\/+/, '');
        var encoded = p.split('/').map(function(seg) { return encodeURIComponent(seg); }).join('/');
        var url = '/api/fs/file/' + encoded;
        var qs = [];
        if (this.token) qs.push('token=' + encodeURIComponent(this.token));
        if (opts && opts.download) qs.push('download=1');
        if (opts && opts.username) qs.push('username=' + encodeURIComponent(opts.username));
        if (qs.length) url += '?' + qs.join('&');
        return url;
    },
    fsWrite: function(path, content) { return this.request('PUT', '/fs/write', { path: path, content: content || '' }); },
    fsMkdir: function(path) { return this.post('/fs/mkdir?path=' + encodeURIComponent(path)); },
    fsUpload: function(path, file, multipartFilename) {
        var form = new FormData();
        form.append('file', file, multipartFilename != null && multipartFilename !== '' ? multipartFilename : file.name);
        if (path) form.append('path', path);
        var headers = {};
        if (this.token) headers['Authorization'] = 'Bearer ' + this.token;
        return fetch('/api/fs/upload', { method: 'POST', headers: headers, body: form }).then(function(r) {
            return r.json().then(function(d) {
                if (r.status === 401) return API._onHttp401(function() { return API.fsUpload(path, file, multipartFilename); }, {});
                if (!r.ok) throw new Error(d.detail || (typeof t === 'function' ? t('api.uploadFailed') : 'Upload failed'));
                return d;
            });
        });
    },
    fsUploadWithProgress: function(path, file, multipartFilename, onProgress) {
        var self = this;
        return new Promise(function(resolve, reject) {
            var form = new FormData();
            form.append('file', file, multipartFilename != null && multipartFilename !== '' ? multipartFilename : file.name);
            if (path) form.append('path', path);
            var xhr = new XMLHttpRequest();
            xhr.open('POST', '/api/fs/upload');
            if (self.token) xhr.setRequestHeader('Authorization', 'Bearer ' + self.token);
            xhr.upload.onprogress = function(e) {
                if (typeof onProgress === 'function' && e.lengthComputable) {
                    onProgress({ loaded: e.loaded, total: e.total });
                }
            };
            xhr.onload = function() {
                var d;
                try {
                    d = JSON.parse(xhr.responseText || '{}');
                } catch (e2) {
                    reject(new Error(xhr.responseText || (typeof t === 'function' ? t('api.uploadFailed') : 'Upload failed')));
                    return;
                }
                if (xhr.status === 401) {
                    self._onHttp401(function() {
                        return self.fsUploadWithProgress(path, file, multipartFilename, onProgress);
                    }, {}).then(resolve).catch(reject);
                    return;
                }
                if (xhr.status < 200 || xhr.status >= 300) {
                    reject(new Error(d.detail || (typeof t === 'function' ? t('api.uploadFailed') : 'Upload failed')));
                    return;
                }
                resolve(d);
            };
            xhr.onerror = function() {
                reject(new Error(typeof t === 'function' ? t('api.uploadFailed') : 'Upload failed'));
            };
            xhr.send(form);
        });
    },
    fsDownloadUrl: function(path) {
        var q = '/api/fs/download?path=' + encodeURIComponent(path);
        return (this.token ? q + '&token=' + encodeURIComponent(this.token) : q);
    },
    fsDownload: function(path, filename) {
        var self = this;
        var headers = {};
        if (this.token) headers['Authorization'] = 'Bearer ' + this.token;
        return fetch('/api/fs/download?path=' + encodeURIComponent(path), { headers: headers }).then(function(r) {
            if (!r.ok) return r.json().then(function(d) { throw new Error(d.detail || (typeof t === 'function' ? t('api.downloadFailed') : 'Download failed')); });
            return r.blob().then(function(blob) {
                var a = document.createElement('a');
                a.href = URL.createObjectURL(blob);
                a.download = filename || path.split('/').pop() || 'download';
                a.click();
                URL.revokeObjectURL(a.href);
            });
        });
    },
    fsPackTgz: function(path) { return this.post('/fs/pack-tgz?path=' + encodeURIComponent(path)); },
    fsUnpackTgz: function(path, dest) {
        var q = 'path=' + encodeURIComponent(path);
        if (dest) q += '&dest=' + encodeURIComponent(dest);
        return this.post('/fs/unpack-tgz?' + q);
    },
    fsDelete: function(path) { return this.del('/fs/delete?path=' + encodeURIComponent(path)); },
    fsCopy: function(path, destDir, move) {
        return this.post('/fs/copy', { path: path, dest_dir: destDir || '', move: !!move });
    },

    /* 远程文件系统（SSH 主机） */
    remoteFsList: function(hostId, path) { return this.get('/remote-fs/list?host_id=' + parseInt(hostId, 10) + '&path=' + encodeURIComponent(path || '/')); },
    remoteFsRead: function(hostId, path) { return this.get('/remote-fs/read?host_id=' + parseInt(hostId, 10) + '&path=' + encodeURIComponent(path)); },
    remoteFsDownload: function(hostId, path, filename) {
        var url = '/api/remote-fs/download?host_id=' + parseInt(hostId, 10) + '&path=' + encodeURIComponent(path);
        var headers = {};
        if (this.token) headers['Authorization'] = 'Bearer ' + this.token;
        return fetch(url, { headers: headers }).then(function(r) {
            if (!r.ok) return r.json().then(function(d) { throw new Error(d.detail || (typeof t === 'function' ? t('api.downloadFailed') : 'Download failed')); });
            return r.blob().then(function(blob) {
                var a = document.createElement('a');
                a.href = URL.createObjectURL(blob);
                a.download = filename || path.split('/').pop() || 'download';
                a.click();
                URL.revokeObjectURL(a.href);
            });
        });
    },
    remoteFsUpload: function(hostId, path, file) {
        var form = new FormData();
        form.append('host_id', parseInt(hostId, 10));
        form.append('path', path || '/');
        form.append('file', file);
        var headers = {};
        if (this.token) headers['Authorization'] = 'Bearer ' + this.token;
        return fetch('/api/remote-fs/upload', { method: 'POST', headers: headers, body: form }).then(function(r) {
            return r.json().then(function(d) {
                if (r.status === 401) return API._onHttp401(function() { return API.remoteFsUpload(hostId, path, file); }, {});
                if (!r.ok) throw new Error(Array.isArray(d.detail) ? (d.detail[0] && d.detail[0].msg) || (typeof t === 'function' ? t('api.uploadFailed') : 'Upload failed') : (d.detail || (typeof t === 'function' ? t('api.uploadFailed') : 'Upload failed')));
                return d;
            });
        });
    },
    remoteFsWrite: function(hostId, path, content) {
        var url = '/api/remote-fs/write?host_id=' + parseInt(hostId, 10) + '&path=' + encodeURIComponent(path);
        var headers = { 'Content-Type': 'application/json' };
        if (this.token) headers['Authorization'] = 'Bearer ' + this.token;
        return fetch(url, { method: 'POST', headers: headers, body: JSON.stringify({ content: content }) }).then(function(r) {
            return r.json().then(function(d) {
                if (r.status === 401) return API._onHttp401(function() { return API.remoteFsWrite(hostId, path, content); }, {});
                if (!r.ok) throw new Error(Array.isArray(d.detail) ? (d.detail[0] && d.detail[0].msg) || (typeof t === 'function' ? t('api.saveFailed') : 'Save failed') : (d.detail || (typeof t === 'function' ? t('api.saveFailed') : 'Save failed')));
                return d;
            });
        });
    },
    remoteFsMkdir: function(hostId, path) {
        return this.post('/remote-fs/mkdir?host_id=' + parseInt(hostId, 10) + '&path=' + encodeURIComponent(path));
    },
    remoteFsDelete: function(hostId, path) {
        var url = '/api/remote-fs/delete?host_id=' + parseInt(hostId, 10) + '&path=' + encodeURIComponent(path);
        var headers = {};
        if (this.token) headers['Authorization'] = 'Bearer ' + this.token;
        return fetch(url, { method: 'DELETE', headers: headers }).then(function(r) {
            return r.json().then(function(d) {
                if (r.status === 401) return API._onHttp401(function() { return API.remoteFsDelete(hostId, path); }, {});
                if (!r.ok) throw new Error(Array.isArray(d.detail) ? (d.detail[0] && d.detail[0].msg) || (typeof t === 'function' ? t('api.deleteFailed') : 'Delete failed') : (d.detail || (typeof t === 'function' ? t('api.deleteFailed') : 'Delete failed')));
                return d;
            });
        });
    },
    remoteFsRename: function(hostId, path, newPath) {
        var url = '/api/remote-fs/rename?host_id=' + parseInt(hostId, 10) + '&path=' + encodeURIComponent(path);
        var headers = { 'Content-Type': 'application/json' };
        if (this.token) headers['Authorization'] = 'Bearer ' + this.token;
        return fetch(url, { method: 'POST', headers: headers, body: JSON.stringify({ new_path: newPath }) }).then(function(r) {
            return r.json().then(function(d) {
                if (r.status === 401) return API._onHttp401(function() { return API.remoteFsRename(hostId, path, newPath); }, {});
                if (!r.ok) throw new Error(Array.isArray(d.detail) ? (d.detail[0] && d.detail[0].msg) || (typeof t === 'function' ? t('api.renameFailed') : 'Rename failed') : (d.detail || (typeof t === 'function' ? t('api.renameFailed') : 'Rename failed')));
                return d;
            });
        });
    },
    remoteFsCopy: function(hostId, srcPath, destDir, move) {
        var url = '/api/remote-fs/copy?host_id=' + parseInt(hostId, 10) + '&path=' + encodeURIComponent(srcPath);
        var headers = { 'Content-Type': 'application/json' };
        if (this.token) headers['Authorization'] = 'Bearer ' + this.token;
        return fetch(url, { method: 'POST', headers: headers, body: JSON.stringify({ dest_dir: destDir, move: !!move }) }).then(function(r) {
            return r.json().then(function(d) {
                if (r.status === 401) return API._onHttp401(function() { return API.remoteFsCopy(hostId, srcPath, destDir, move); }, {});
                if (!r.ok) throw new Error(Array.isArray(d.detail) ? (d.detail[0] && d.detail[0].msg) || (typeof t === 'function' ? t('api.operationFailed') : 'Operation failed') : (d.detail || (typeof t === 'function' ? t('api.operationFailed') : 'Operation failed')));
                return d;
            });
        });
    }
};
