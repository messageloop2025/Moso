# -*- coding: utf-8 -*-
import pathlib
ROOT = pathlib.Path(__file__).resolve().parents[1]
APP = ROOT / "web" / "js" / "app.js"
text = APP.read_text(encoding="utf-8")
orig = text

BLOCKS = [
    (
        "function deleteHost(id, name, isShared) {\n    var title = isShared ? '确认解除分享' : '确认删除';\n    var msg = isShared\n        ? ('确定要解除主机「' + name + '」的分享吗？这不会删除真实主机。')\n        : ('确定要删除主机「' + name + '」吗？');\n    showConfirm(title, msg).then(function(ok) {",
        "function deleteHost(id, name, isShared) {\n    var dn = (name != null && String(name).trim() !== '') ? String(name).trim() : ('#' + id);\n    var title = isShared ? t('confirm.ungroupShareTitle') : t('confirm.deletePathTitle');\n    var msg = isShared\n        ? t('confirm.ungroupShareBody', { name: dn })\n        : t('confirm.deleteHostNameBody', { name: dn });\n    showConfirm(title, msg).then(function(ok) {",
    ),
    (
        "function deleteHostInTree(id, name, isShared) {\n    var title = isShared ? '确认解除分享' : '确认删除';\n    var msg = isShared\n        ? ('确定要解除主机「' + (name || id) + '」的分享吗？这不会删除真实主机。')\n        : ('确定要删除主机「' + (name || id) + '」吗？');\n    showConfirm(title, msg).then(function(ok) {",
        "function deleteHostInTree(id, name, isShared) {\n    var dn = (name != null && String(name).trim() !== '') ? String(name).trim() : (String(id) || ('#' + id));\n    var title = isShared ? t('confirm.ungroupShareTitle') : t('confirm.deletePathTitle');\n    var msg = isShared\n        ? t('confirm.ungroupShareBody', { name: dn })\n        : t('confirm.deleteHostNameBody', { name: dn });\n    showConfirm(title, msg).then(function(ok) {",
    ),
]

SUBS = [
    ("if (!confirm('确定清空全部活动主机？将清理这些主机的页面缓存，下次打开会重新加载。')) return;",
     "if (!confirm(t('confirm.clearAllActiveHosts'))) return;"),
    ("showToast('正在由 AI 总结主机级提示词… 完成后自动回填');", "showToast(t('toast.summarizingHostPrompt'));"),
    ("showToast('正在总结主机级提示词… 可关闭窗口，完成后自动保存');", "showToast(t('toast.summarizingHostModal'));"),
    ("showToast('正在总结… 可随时关闭窗口，完成后自动保存');", "showToast(t('toast.summarizingSession'));"),
    ("showToast(modal.style.display !== 'none' ? '已由 AI 总结并替换' : '会话提示词已更新');",
     "showToast(modal.style.display !== 'none' ? t('toast.sessionReplacedByAi') : t('toast.sessionUpdatedShort'));"),
    ("showToast(modal.style.display !== 'none' ? '已由 AI 总结并追加' : '会话提示词已更新');",
     "showToast(modal.style.display !== 'none' ? t('toast.sessionAppendedByAi') : t('toast.sessionUpdatedShort'));"),
    ("showToast(action === 'append' ? '已由 AI 总结并追加' : '已由 AI 总结并替换');",
     "showToast(action === 'append' ? t('toast.sessionAppendedByAi') : t('toast.sessionReplacedByAi'));"),
    ("showConfirm('解绑邮箱', '确定要解绑当前邮箱吗？解绑后无法通过该邮箱找回密码。')",
     "showConfirm(t('confirm.unbindEmailTitle'), t('confirm.unbindEmailBody'))"),
    ("showConfirm('确认删除', '删除后会移除该标签在所有主机上的绑定，是否继续？')",
     "showConfirm(t('confirm.deleteTagTitle'), t('confirm.deleteTagBody'))"),
    ("showConfirm('确认删除', '确定要删除【' + (isDir ? '目录' : '文件') + '】' + path + ' 吗？此操作不可恢复。')",
     "showConfirm(t('confirm.deletePathTitle'), t('confirm.deleteFileBody', { type: isDir ? t('confirm.dir') : t('confirm.file'), path: path }))"),
    ("showConfirm('确认删除', '确定要删除【' + typeLabel + '】' + path + ' 吗？此操作不可恢复。')",
     "showConfirm(t('confirm.deletePathTitle'), t('confirm.deleteFileBody', { type: typeLabel, path: path }))"),
    ("showConfirm('确认删除', '确定删除该凭证？被引用的主机将无法使用该凭证登录。')",
     "showConfirm(t('confirm.deleteCredentialTitle'), t('confirm.deleteCredentialBody'))"),
    ("showConfirm('确认删除', '确定删除该分组？分组内的主机会变为未分组。')",
     "showConfirm(t('confirm.deleteGroupTitle'), t('confirm.deleteGroupBody'))"),
    ("showConfirm('确认删除', '确定删除该条最佳实践？')",
     "showConfirm(t('confirm.deleteBestPracticeTitle'), t('confirm.deleteBestPracticeBody'))"),
    ("showConfirm('确认删除', '确定删除该记录？')",
     "showConfirm(t('confirm.deleteMaintenanceTitle'), t('confirm.deleteMaintenanceBody'))"),
    ("showConfirm('撤销令牌', '撤销后该令牌立即失效，使用它的集成将无法再访问 API。确定撤销？')",
     "showConfirm(t('confirm.revokeTokenTitle'), t('confirm.revokeTokenBody'))"),
    ("showConfirm('确认撤销', '确定撤销该分享吗？')",
     "showConfirm(t('confirm.revokeShareTitle'), t('confirm.revokeShareBody'))"),
    ("if (!confirm('确定清空本主机全部会话？')) return;", "if (!confirm(t('confirm.clearHostAllSessions'))) return;"),
    ("if (!confirm('确定清空本会话全部聊天内容？')) return;", "if (!confirm(t('confirm.clearSessionChat'))) return;"),
    ("if (!confirm('确定保留前 ' + n + ' 条消息，删除其后的全部？')) return;",
     "if (!confirm(t('confirm.keepNMessages', {n: n}))) return;"),
    ("if (!confirm('是否移动？')) return;", "if (!confirm(t('confirm.moveConfirm'))) return;"),
    ("if (!confirm('确定删除该会话？')) return;", "if (!confirm(t('confirm.deleteThisSession'))) return;"),
    ("if (!confirm('确定清空全部会话？此操作不可恢复。')) return;",
     "if (!confirm(t('confirm.clearAllSessionsIrreversible'))) return;"),
    ("showToast('已复制全部 ' + hostAiLogBuffer.length + ' 条记录');",
     "showToast(t('toast.copiedLogLines', {n: hostAiLogBuffer.length}));"),
    ("showToast('已复制全部 ' + aiLogBuffer.length + ' 条记录');",
     "showToast(t('toast.copiedLogLines', {n: aiLogBuffer.length}));"),
    ("showToast('已清空第 ' + n + ' 条以后的消息');", "showToast(t('toast.clearedAfterN', {n: n}));"),
    ("showToast('已更新：' + (res.host_type || '') + ' / ' + (res.host_version || '') + (res.host_shell ? ' Shell=' + res.host_shell : '') + (res.host_package_manager ? ' 包管理=' + res.host_package_manager : ''));",
     "showToast(t('toast.hostTypeUpdated') + (res.host_type || '') + ' / ' + (res.host_version || '') + (res.host_shell ? ' ' + t('toast.shell') + res.host_shell : '') + (res.host_package_manager ? ' ' + t('toast.pkgMgr') + res.host_package_manager : ''));"),
    ("r.message || '留言已提交，待管理员审核后公开展示'", "r.message || t('toast.messageSubmitted')"),
    ("r.message || '申请已提交，我们会通过邮件回复您'", "r.message || t('toast.applySubmitted')"),
    ("res.message || '验证码已发送'", "res.message || t('toast.codeSent')"),
    ("res.message || '密码已重置'", "res.message || t('toast.passwordReset')"),
    ("res.message || '已解锁'", "res.message || t('toast.unlocked')"),
    ("res.message || '账户已解锁'", "res.message || t('toast.accountUnlocked')"),
    ("err.message || '修改失败'", "err.message || t('toast.modifyFailed')"),
    ("err.message || '发送失败'", "err.message || t('toast.sendFailed')"),
    ("err.message || '验证失败'", "err.message || t('toast.verifyFailed')"),
    ("err.message || '解绑失败'", "err.message || t('toast.unbindFailed')"),
    ("err.message || '提交失败'", "err.message || t('toast.submitFailed')"),
    ("err.message || '分享失败'", "err.message || t('toast.shareFailed')"),
    ("err.message || '撤销失败'", "err.message || t('toast.revokeFailed')"),
    ("err.message || '创建失败'", "err.message || t('toast.createFailed')"),
    ("err.message || '更新失败'", "err.message || t('toast.updateFailed')"),
    ("err.message || '删除失败'", "err.message || t('toast.deleteFailed')"),
    ("err.message || '保存标签失败'", "err.message || t('toast.saveTagFailed')"),
    ("err.message || '保存失败'", "err.message || t('toast.saveFailed')"),
    ("err.message || '总结失败'", "err.message || t('toast.summarizeFailed')"),
    ("err.message || '导出失败'", "err.message || t('toast.exportFailed')"),
    ("err.message || '清空失败'", "err.message || t('toast.clearFailed')"),
    ("err.message || '操作失败'", "err.message || t('toast.opFailed')"),
    ("err.message || '生成失败'", "err.message || t('toast.generateTitleFailed')"),
    ("err.message || '移动失败'", "err.message || t('toast.moveFailed')"),
    ("err.message || '上传失败'", "err.message || t('toast.uploadFailed')"),
    ("err.message || '加载失败'", "err.message || t('toast.loadFailed')"),
    ("err.message || '加载主机级提示词失败'", "err.message || t('toast.loadHostPromptFailed')"),
    ("(err && err.message) || '导出失败'", "(err && err.message) || t('toast.exportFailed')"),
    ("(err && err.message ? err.message : '请求失败')", "(err && err.message ? err.message : t('toast.requestFailed'))"),
    ("err.message || '请求失败'", "err.message || t('toast.requestFailed')"),
    ("successMessage || '已复制'", "successMessage || t('toast.copied')"),
    ("showToast('无数据可导出', 'warning')", "showToast(t('toast.noDataExport'), 'warning')"),
    ("showToast('没有可复制的内容', 'warning')", "showToast(t('toast.nothingToCopy'), 'warning')"),
    ("showToast('复制失败', 'error')", "showToast(t('toast.copyFailed'), 'error')"),
    ("showToast('图片导出资源未加载', 'error')", "showToast(t('toast.imageExportUnavailable'), 'error')"),
    ("showToast('请输入当前密码', 'error')", "showToast(t('toast.enterCurrentPassword'), 'error')"),
    ("showToast('新密码至少 6 个字符', 'error')", "showToast(t('toast.newPasswordMin6'), 'error')"),
    ("if (pwd.length < 6) { showToast('新密码至少 6 个字符', 'error'); return; }", "if (pwd.length < 6) { showToast(t('toast.newPasswordMin6'), 'error'); return; }"),
    ("showToast('两次输入的新密码不一致', 'error')", "showToast(t('toast.passwordMismatch'), 'error')"),
    ("showToast('密码已修改，请妥善保管')", "showToast(t('toast.passwordChanged'))"),
    ("showToast('请输入邮箱', 'error')", "showToast(t('toast.enterEmail'), 'error')"),
    ("showToast('验证码已发送')", "showToast(t('toast.codeSent'))"),
    ("showToast('请输入 6 位验证码', 'error')", "showToast(t('toast.code6Required'), 'error')"),
    ("showToast('邮箱已绑定')", "showToast(t('toast.emailBound'))"),
    ("showToast('已解绑')", "showToast(t('toast.unbound'))"),
    ("showToast('请填写留言内容', 'error')", "showToast(t('toast.fillMessage'), 'error')"),
    ("showToast('请先完成验证码', 'error')", "showToast(t('toast.completeCaptcha'), 'error')"),
    ("showToast('请填写姓名 / 用户名', 'error')", "showToast(t('toast.fillNameOrUsername'), 'error')"),
    ("showToast('请填写手机号', 'error')", "showToast(t('toast.fillPhone'), 'error')"),
    ("showToast('请填写邮箱', 'error')", "showToast(t('toast.fillEmail'), 'error')"),
    ("showToast('请输入用户名和邮箱', 'error')", "showToast(t('toast.enterUsernameAndEmail'), 'error')"),
    ("showToast('请使用邮件中的完整链接，或粘贴 token', 'error')", "showToast(t('toast.useEmailLinkOrToken'), 'error')"),
    ("showToast('已关闭该活动主机')", "showToast(t('toast.hostClosed'))"),
    ("showToast('已全部清除')", "showToast(t('toast.allCleared'))"),
    ("showToast('请选择凭证', 'error')", "showToast(t('toast.selectCredential'), 'error')"),
    ("showToast('请填写密码', 'error')", "showToast(t('toast.fillPassword'), 'error')"),
    ("showToast('请填写私钥内容', 'error')", "showToast(t('toast.fillPrivateKey'), 'error')"),
    ("showToast('请填写名称和主机地址', 'error')", "showToast(t('toast.fillNameAndHost'), 'error')"),
    ("showToast('请选择凭证或新建凭证', 'error')", "showToast(t('toast.selectOrNewCredential'), 'error')"),
    ("showToast('已添加并加入分组')", "showToast(t('toast.addedWithGroup'))"),
    ("showToast('已解除分享')", "showToast(t('toast.shareDetached'))"),
    ("if (!uname) { showToast('请输入用户名', 'error'); return; }", "if (!uname) { showToast(t('toast.enterUsername'), 'error'); return; }"),
    ("showToast('分享成功')", "showToast(t('toast.shareOk'))"),
    ("showToast('已撤销分享')", "showToast(t('toast.shareRevoked'))"),
    ("showToast('请输入标签名', 'error')", "showToast(t('toast.enterTagName'), 'error')"),
    ("showToast('标签名不能为空', 'error')", "showToast(t('toast.tagNameEmpty'), 'error')"),
    ("showToast('分享主机仅可查看，不能修改', 'error')", "showToast(t('toast.sharedHostReadonly'), 'error')"),
    ("showToast('已保存别名与用途说明')", "showToast(t('toast.aliasSaved'))"),
    ("showToast('已保存主机级提示词')", "showToast(t('toast.hostPromptSaved'))"),
    ("showToast('未能从对话中归纳出有效内容')", "showToast(t('toast.noValidSummary'))"),
    ("showToast(action === 'append' ? '已追加总结结果' : '已用总结结果替换')", "showToast(action === 'append' ? t('toast.summaryAppended') : t('toast.summaryReplaced'))"),
    ("showToast('正在检测主机类型…', 'info')", "showToast(t('toast.detectingHostType'), 'info')"),
    ("showToast('当前会话暂无消息')", "showToast(t('toast.noMessagesInSession'))"),
    ("showToast('已导出 Markdown')", "showToast(t('toast.exportedMd'))"),
    ("showToast('正在生成名称…')", "showToast(t('toast.generatingTitle'))"),
    ("showToast('已生成名称')", "showToast(t('toast.titleGenerated'))"),
    ("showToast('已清空聊天内容')", "showToast(t('toast.chatCleared'))"),
    ("showToast('界面未就绪，请刷新后重试')", "showToast(t('toast.uiNotReady'))"),
    ("showToast('未能归纳出有效提示词，未修改')", "showToast(t('toast.sessionPromptNoChange'))"),
    ("showToast('未能归纳出有效提示词，未追加')", "showToast(t('toast.sessionPromptNoAppend'))"),
    ("showToast('未识别到主机 ID')", "showToast(t('toast.hostIdUnknown'))"),
    ("showToast('未能从对话中归纳出有效的主机级提示词')", "showToast(t('toast.hostPromptNoSummary'))"),
    ("showToast('已创建新会话')", "showToast(t('toast.newSessionCreated'))"),
    ("showToast('附件正在上传，请稍候再发送', 'warning')", "showToast(t('toast.attachUploading'), 'warning')"),
    ("showToast('已中断', 'info')", "showToast(t('toast.aborted'), 'info')"),
    ("showToast('不能移动到自身或子目录', 'error')", "showToast(t('toast.cannotMoveIntoSelf'), 'error')"),
    ("showToast('请先选择要下载的文件', 'error')", "showToast(t('toast.selectDownloadFile'), 'error')"),
    ("showToast('请先选择要保存的文件', 'error')", "showToast(t('toast.selectSaveFile'), 'error')"),
    ("showToast('无可编辑内容', 'error')", "showToast(t('toast.nothingToEdit'), 'error')"),
    ("showToast('无粘贴内容或主机不一致', 'error')", "showToast(t('toast.noPaste'), 'error')"),
    ("showToast('不支持该格式解压', 'error')", "showToast(t('toast.extractUnsupported'), 'error')"),
    ("showToast('请输入命令', 'error')", "showToast(t('toast.enterCommand'), 'error')"),
    ("showToast('请在上方选择主机', 'error')", "showToast(t('toast.selectHostFirst'), 'error')"),
    ("showToast('已更新主机类型：' + (res.host_type || '未知'))", "showToast(t('toast.updateHostTypeOk') + (res.host_type || t('common.unknown')))"),
    ("showToast(notes && notes.length ? notes[0] : 'Mermaid 语法已自动修正', 'info')",
     "showToast(notes && notes.length ? notes[0] : t('toast.mermaidAutoFixed'), 'info')"),
    ("if (typeof showToast === 'function') showToast(it.name + ' 超过单文件上限（20 MB）', 'warning')",
     "if (typeof showToast === 'function') showToast((it.name || '') + ' ' + t('toast.fileOverLimit'), 'warning')"),
    ("if (typeof showToast === 'function') showToast((it.name || '附件') + ' 上传失败：' + it.error, 'error')",
     "if (typeof showToast === 'function') showToast((it.name || t('toast.attachment')) + t('toast.uploadFailedPrefix') + (it.error != null ? String(it.error) : ''), 'error')"),
    ("if (!confirm('确定删除？')) return;", "if (!confirm(t('toast.confirmDelete'))) return;"),
]
# 短串、多出处 — 在较长替换之后执行
LATE = [
    ("showToast('已更新状态')", "showToast(t('toast.statusUpdated'))"),
    ("showToast('请填写用户名', 'error')", "showToast(t('toast.fillUsername'), 'error')"),
    ("showToast('请输入用户名', 'error')", "showToast(t('toast.enterUsername'), 'error')"),
    ("showToast('已删除')", "showToast(t('toast.deleted'))"),
    ("showToast('已保存')", "showToast(t('toast.saved'))"),
    ("showToast('已改名')", "showToast(t('toast.renamed'))"),
    ("showToast('已更新')", "showToast(t('toast.itemUpdated'))"),
    ("showToast('已添加')", "showToast(t('toast.added'))"),
    ("showToast('已刷新')", "showToast(t('toast.refreshed'))"),
    ("showToast('已上传')", "showToast(t('toast.uploaded'))"),
    ("showToast('已移动')", "showToast(t('toast.moved'))"),
    ("showToast('已清空')", "showToast(t('toast.cleared'))"),
    ("showToast('已更新')", "showToast(t('toast.updatedStatus'))"),  # may wrong - remove
    ("showToast('已解压')", "showToast(t('toast.extracted'))"),
    ("showToast('已打包')", "showToast(t('toast.archived'))"),
    ("showToast('已创建')", "showToast(t('toast.fileCreated'))"),
    ("showToast('已复制')", "showToast(t('toast.copiedToClipboard'))"),
    ("showToast('已剪切')", "showToast(t('toast.cut'))"),
    ("showToast('标签已创建')", "showToast(t('toast.tagCreated'))"),
    ("showToast('标签已更新')", "showToast(t('toast.tagUpdated'))"),
    ("showToast('标签已保存')", "showToast(t('toast.tagSaved'))"),
    ("showToast('请选择或创建会话')", "showToast(t('toast.selectOrCreateSession'))"),  # wrong text
    ("showToast('请先选择或创建会话')", "showToast(t('toast.selectOrCreateSession'))"),
    ("showToast('暂无内容可复制', 'info')", "showToast(t('toast.noCopyYet'), 'info')"),
]

# remove bad LATE lines
LATE = [p for p in LATE if "updatedStatus" not in p[1]]

SUBS = [p for p in SUBS if p[0] != p[1]]
SUBS += [
    ("showToast(hostRemoteFsClipboard.cut ? '已移动' : '已粘贴')", "showToast(hostRemoteFsClipboard.cut ? t('toast.moved') : t('toast.pasted'))"),
    ("showToast(aiRemoteFsClipboard.cut ? '已移动' : '已粘贴')", "showToast(aiRemoteFsClipboard.cut ? t('toast.moved') : t('toast.pasted'))"),
    ("showToast('\\u6b63\\u5728\\u4e3a\\u0020AI\\u0020\\u521b\\u5efa\\u63a7\\u5236\\u53f0\\u5e76\\u8fde\\u63a5\\u2026')",
     "showToast(t('toast.creatingAiConsoleUnicode'))"),
]

for o, n in BLOCKS:
    if o not in text:
        print("BLOCK miss:", o[:60])
    else:
        text = text.replace(o, n, 1)

# merge SUBS and LATE, sort by length
ALL = SUBS + LATE
ALL.sort(key=lambda x: -len(x[0]))
miss = []
for o, n in ALL:
    if o not in text:
        miss.append(o[:120])
        continue
    text = text.replace(o, n)

# creatingAiConsole 另一写法
t2 = "showToast('正在为 AI 创建控制台并连接…')"
if t2 in text:
    text = text.replace(t2, "showToast(t('toast.creatingAiConsole'))")

if miss:
    (ROOT / "scripts" / "_apply_toast_missed.txt").write_text("\n".join(miss), encoding="utf-8")
    print("missed", len(miss), "— scripts/_apply_toast_missed.txt")
else:
    print("all SUBS+matched")

if text != orig:
    APP.write_text(text, encoding="utf-8")
    print("wrote", len(text) - len(orig), "byte delta")
else:
    print("no change")
