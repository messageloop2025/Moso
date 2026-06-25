/** 离线兜底：Moso 不可达时注册的最小扩展工具集（在线以 manifest.extended_tools 为准）。 */
export const CLAW_OPS_PLUGIN_VERSION = "1.1.0";

export type ManifestToolDef = {
  name: string;
  label: string;
  description: string;
  timeout_ms?: number;
  parameters_schema: Record<string, unknown>;
};

export const FALLBACK_EXTENDED_TOOLS: ManifestToolDef[] = [
  {
    name: "edgeops_ssh_execute",
    label: "Moso · 非交互 SSH",
    description: "SSH 命令（detach/poll_log）",
    timeout_ms: 330_000,
    parameters_schema: {
      type: "object",
      properties: {
        host_id: { type: "integer" },
        command: { type: "string" },
        timeout: { type: "integer" },
        detach: { type: "boolean" },
        poll_log: { type: "boolean" },
        log_path: { type: "string" },
        tail_lines: { type: "integer" },
        session_id: { type: "integer" },
      },
      required: ["host_id", "command"],
    },
  },
  {
    name: "edgeops_list_host_groups",
    label: "Moso · 主机分组",
    description: "列出主机分组",
    parameters_schema: { type: "object", properties: {} },
  },
  {
    name: "edgeops_get_host_groups_tree",
    label: "Moso · 分组树",
    description: "分组树",
    parameters_schema: { type: "object", properties: {} },
  },
  {
    name: "edgeops_get_group_hosts",
    label: "Moso · 分组主机",
    description: "分组内主机",
    parameters_schema: {
      type: "object",
      properties: { group_id: { type: "integer" } },
      required: ["group_id"],
    },
  },
  {
    name: "edgeops_probe_host_capabilities",
    label: "Moso · 探测画像",
    description: "探测主机能力画像",
    parameters_schema: {
      type: "object",
      properties: {
        host_id: { type: "integer" },
        refresh: { type: "boolean" },
        max_age_hours: { type: "integer" },
        timeout: { type: "integer" },
      },
      required: ["host_id"],
    },
  },
  {
    name: "edgeops_get_host_capabilities",
    label: "Moso · 读画像",
    description: "读取能力画像",
    parameters_schema: {
      type: "object",
      properties: { host_id: { type: "integer" } },
      required: ["host_id"],
    },
  },
  {
    name: "edgeops_update_host_prompt",
    label: "Moso · 写提示词",
    description: "覆盖主机提示词",
    parameters_schema: {
      type: "object",
      properties: { host_id: { type: "integer" }, content: { type: "string" } },
      required: ["host_id", "content"],
    },
  },
  {
    name: "edgeops_append_host_prompt",
    label: "Moso · 追加提示词",
    description: "追加主机提示词",
    parameters_schema: {
      type: "object",
      properties: { host_id: { type: "integer" }, text: { type: "string" } },
      required: ["host_id", "text"],
    },
  },
  {
    name: "edgeops_list_maintenance_history",
    label: "Moso · 维护历史",
    description: "维护历史只读",
    parameters_schema: {
      type: "object",
      properties: {
        host: { type: "string" },
        category: { type: "string" },
        page: { type: "integer" },
        page_size: { type: "integer" },
      },
    },
  },
  {
    name: "edgeops_list_operation_logs",
    label: "Moso · 操作日志",
    description: "操作审计只读",
    parameters_schema: {
      type: "object",
      properties: {
        page: { type: "integer" },
        page_size: { type: "integer" },
        host_id: { type: "integer" },
      },
    },
  },
  {
    name: "edgeops_list_service_credentials",
    label: "Moso · 搜索服务凭证",
    description:
      "搜索服务凭证元数据（不含密码）。command_hint / service+address / keyword；需 credentials_vault_enabled。",
    parameters_schema: {
      type: "object",
      properties: {
        command_hint: { type: "string" },
        service: { type: "string" },
        address: { type: "string" },
        port: { type: "integer" },
        service_username: { type: "string" },
        keyword: { type: "string" },
        sort_by: { type: "string" },
        sort_order: { type: "string" },
        limit: { type: "integer" },
      },
    },
  },
  {
    name: "edgeops_send_service_password",
    label: "Moso · 注入服务密码",
    description:
      "按 credential_id 向 ssh_channel/terminal 注入密码；target=ssh_channel 时需 channel_id。",
    parameters_schema: {
      type: "object",
      properties: {
        credential_id: { type: "integer" },
        target: { type: "string", enum: ["terminal", "ssh_channel", "local_terminal"] },
        channel_id: { type: "integer" },
        host_id: { type: "integer" },
        slot: { type: "integer" },
        require_password_prompt: { type: "boolean" },
      },
      required: ["credential_id", "target"],
    },
  },
  {
    name: "edgeops_remote_fs_list",
    label: "Moso · 远程目录",
    description: "SFTP list",
    parameters_schema: {
      type: "object",
      properties: { host_id: { type: "integer" }, path: { type: "string" } },
      required: ["host_id"],
    },
  },
  {
    name: "edgeops_remote_fs_read",
    label: "Moso · 远程读",
    description: "SFTP read",
    parameters_schema: {
      type: "object",
      properties: { host_id: { type: "integer" }, path: { type: "string" } },
      required: ["host_id", "path"],
    },
  },
  {
    name: "edgeops_remote_fs_write",
    label: "Moso · 远程写",
    description: "SFTP write",
    parameters_schema: {
      type: "object",
      properties: {
        host_id: { type: "integer" },
        path: { type: "string" },
        content: { type: "string" },
      },
      required: ["host_id", "path"],
    },
  },
  {
    name: "edgeops_list_batch_jobs",
    label: "Moso · 批量任务",
    description: "批量任务列表",
    parameters_schema: {
      type: "object",
      properties: {
        page: { type: "integer" },
        page_size: { type: "integer" },
        operation_type: { type: "string" },
        status: { type: "string" },
      },
    },
  },
  {
    name: "edgeops_get_batch_job",
    label: "Moso · 批量详情",
    description: "批量任务详情",
    parameters_schema: {
      type: "object",
      properties: { batch_id: { type: "integer" } },
      required: ["batch_id"],
    },
  },
  {
    name: "edgeops_list_scheduled_tasks",
    label: "Moso · 定时任务",
    description: "定时任务列表",
    parameters_schema: { type: "object", properties: {} },
  },
  {
    name: "edgeops_get_scheduled_task",
    label: "Moso · 定时详情",
    description: "定时任务详情",
    parameters_schema: {
      type: "object",
      properties: { task_id: { type: "integer" } },
      required: ["task_id"],
    },
  },
  {
    name: "edgeops_list_triggered_tasks",
    label: "Moso · 触发任务",
    description: "触发任务列表",
    parameters_schema: { type: "object", properties: {} },
  },
  {
    name: "edgeops_get_triggered_task",
    label: "Moso · 触发详情",
    description: "触发任务详情",
    parameters_schema: {
      type: "object",
      properties: { task_id: { type: "integer" } },
      required: ["task_id"],
    },
  },
  {
    name: "edgeops_list_session_messages",
    label: "Moso · 会话消息",
    description: "integration 会话历史",
    parameters_schema: {
      type: "object",
      properties: {
        session_id: { type: "integer" },
        limit: { type: "integer" },
      },
      required: ["session_id"],
    },
  },
];

export type ClawOpsManifest = {
  capabilities_version?: string;
  unchanged?: boolean;
  system_prompt?: { prepend_markdown?: string; etag?: string };
  extended_tools?: ManifestToolDef[];
  update_check?: {
    needs_update?: boolean;
    incompatible?: boolean;
    recommended_version?: string;
    message?: string;
  };
};
