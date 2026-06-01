import { Type } from "@sinclair/typebox";
import type { createMosoClient } from "./client.js";

type ClientFactory = ReturnType<typeof createMosoClient>;
type OkResult = (data: unknown) => {
  content: { type: "text"; text: string }[];
  details: unknown;
};

// eslint-disable-next-line @typescript-eslint/no-explicit-any
type PluginApi = { registerTool: (tool: any) => void };

export function registerSshChannelTools(
  api: PluginApi,
  getClient: () => ClientFactory,
  okResult: OkResult,
) {
  const channelDesc =
    "**ClawOps — 无界面交互式 SSH TTY 管道**（REST /api/ssh-channel）。用于 sudo 密码、vi、多步向导、Ctrl+C；默认 600s 无读写自动关。大输出会 spill 落盘。";

  api.registerTool({
    name: "edgeops_ssh_channel_create",
    label: "Moso · SSH 通道创建",
    description: `${channelDesc} 创建后返回 channel_id 与主机 IP/别名/用途/提示词摘要。`,
    parameters: Type.Object({
      host_id: Type.Integer({ description: "Moso host id" }),
      session_id: Type.Optional(
        Type.Integer({
          description: "集成会话 ID（与 edgeops_ops_chat 返回一致）；绑定 owner 且默认 idle 600s",
        }),
      ),
      idle_close_sec: Type.Optional(
        Type.Integer({ description: "空闲关断秒数，默认集成 600 / 否则 300" }),
      ),
    }),
    async execute(_id: string, params: { host_id: number; session_id?: number; idle_close_sec?: number }) {
      const data = await getClient().sshChannelCreate({
        host_id: params.host_id,
        session_id: params.session_id,
        idle_close_sec: params.idle_close_sec,
      });
      return okResult(data);
    },
  });

  api.registerTool({
    name: "edgeops_ssh_channel_list",
    label: "Moso · SSH 通道列表",
    description: `${channelDesc} all_open=true 列出当前用户全部 open 通道。`,
    parameters: Type.Object({
      all_open: Type.Optional(Type.Boolean({ description: "List all open channels for user" })),
      owner_type: Type.Optional(Type.String()),
      owner_id: Type.Optional(Type.String()),
    }),
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    async execute(_id: string, params: any) {
      const data = await getClient().sshChannelList({
        all_open: params.all_open,
        owner_type: params.owner_type,
        owner_id: params.owner_id,
      });
      return okResult(data);
    },
  });

  api.registerTool({
    name: "edgeops_ssh_channel_info",
    label: "Moso · SSH 通道详情",
    description: `${channelDesc} 含行号范围、别名、用途、主机提示词摘要。`,
    parameters: Type.Object({
      channel_id: Type.Integer(),
      check_alive: Type.Optional(Type.Boolean()),
    }),
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    async execute(_id: string, params: any) {
      const data = await getClient().sshChannelInfo(
        params.channel_id,
        params.check_alive,
      );
      return okResult(data);
    },
  });

  api.registerTool({
    name: "edgeops_ssh_channel_send",
    label: "Moso · SSH 通道写入",
    description: `${channelDesc} 向通道 stdin 发送文本或控制键（如 Ctrl+C）。`,
    parameters: Type.Object({
      channel_id: Type.Integer(),
      content: Type.String({ description: "Text to send; supports control sequences" }),
    }),
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    async execute(_id: string, params: any) {
      const data = await getClient().sshChannelSend(
        params.channel_id,
        params.content,
      );
      return okResult(data);
    },
  });

  api.registerTool({
    name: "edgeops_ssh_channel_read_lines",
    label: "Moso · SSH 通道按行读",
    description: `${channelDesc} since_line/last_n 轮询交互输出；过大自动 spill。`,
    parameters: Type.Object({
      channel_id: Type.Integer(),
      since_line: Type.Optional(Type.Integer()),
      last_n: Type.Optional(Type.Integer()),
      from_line: Type.Optional(Type.Integer()),
      to_line: Type.Optional(Type.Integer()),
      session_id: Type.Optional(Type.Integer({ description: "Spill 关联会话" })),
    }),
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    async execute(_id: string, params: any) {
      const data = await getClient().sshChannelReadLines(params.channel_id, {
        since_line: params.since_line,
        last_n: params.last_n,
        from_line: params.from_line,
        to_line: params.to_line,
        session_id: params.session_id,
      });
      return okResult(data);
    },
  });

  api.registerTool({
    name: "edgeops_ssh_channel_read",
    label: "Moso · SSH 通道按字符读",
    description: `${channelDesc} 读最近 max_chars 字符；过大自动 spill。`,
    parameters: Type.Object({
      channel_id: Type.Integer(),
      max_chars: Type.Optional(Type.Integer({ minimum: 1, maximum: 1048576 })),
      session_id: Type.Optional(Type.Integer()),
    }),
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    async execute(_id: string, params: any) {
      const data = await getClient().sshChannelRead(params.channel_id, {
        max_chars: params.max_chars,
        session_id: params.session_id,
      });
      return okResult(data);
    },
  });

  api.registerTool({
    name: "edgeops_ssh_channel_has_new",
    label: "Moso · SSH 通道有新输出?",
    description: `${channelDesc} 轮询 after_line 之后是否有新行。`,
    parameters: Type.Object({
      channel_id: Type.Integer(),
      after_line: Type.Optional(Type.Integer({ description: "Default 0" })),
    }),
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    async execute(_id: string, params: any) {
      const data = await getClient().sshChannelHasNew(
        params.channel_id,
        params.after_line,
      );
      return okResult(data);
    },
  });

  api.registerTool({
    name: "edgeops_ssh_channel_close",
    label: "Moso · SSH 通道关闭",
    description: `${channelDesc} 手工关闭指定 channel_id。`,
    parameters: Type.Object({
      channel_id: Type.Integer(),
    }),
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    async execute(_id: string, params: any) {
      const data = await getClient().sshChannelClose(params.channel_id);
      return okResult(data);
    },
  });

  api.registerTool({
    name: "edgeops_ssh_channel_dump",
    label: "Moso · SSH 通道导出缓冲",
    description: `${channelDesc} 将当前缓冲全文导出到 spill 文件，避免大输出占内存/上下文。`,
    parameters: Type.Object({
      channel_id: Type.Integer(),
      session_id: Type.Optional(Type.Integer()),
      max_chars: Type.Optional(Type.Integer({ minimum: 1024, maximum: 4000000 })),
    }),
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    async execute(_id: string, params: any) {
      const data = await getClient().sshChannelDump(params.channel_id, {
        session_id: params.session_id,
        max_chars: params.max_chars,
      });
      return okResult(data);
    },
  });

  api.registerTool({
    name: "edgeops_ssh_channel_close_batch",
    label: "Moso · SSH 通道批量关闭",
    description: `${channelDesc} 按 session_id 或 owner 关闭该会话下全部 open 通道。`,
    parameters: Type.Object({
      session_id: Type.Optional(Type.Integer()),
      owner_type: Type.Optional(Type.String()),
      owner_id: Type.Optional(Type.String()),
    }),
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    async execute(_id: string, params: any) {
      const data = await getClient().sshChannelCloseBatch({
        session_id: params.session_id,
        owner_type: params.owner_type,
        owner_id: params.owner_id,
      });
      return okResult(data);
    },
  });

  api.registerTool({
    name: "edgeops_read_chat_data",
    label: "Moso · 读取 spill 落盘",
    description:
      "分段读取 SSH 通道/工具 spill 落盘文件（GET …/integration/spill/read）。ssh_channel read 返回 spill_id + storage_subdir 后用本工具读全量。",
    parameters: Type.Object({
      spill_id: Type.String(),
      date_subdir: Type.String({ description: "storage_subdir，如 2026/05/22" }),
      mode: Type.Optional(
        Type.String({ description: "head | tail | head_tail | range，默认 head_tail" }),
      ),
      session_id: Type.Optional(Type.Integer()),
      range_start: Type.Optional(Type.Integer()),
      max_chars: Type.Optional(Type.Integer()),
    }),
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    async execute(_id: string, params: any) {
      const data = await getClient().readSpill({
        spill_id: params.spill_id,
        date_subdir: params.date_subdir,
        mode: params.mode,
        session_id: params.session_id,
        range_start: params.range_start,
        max_chars: params.max_chars,
      });
      return okResult(data);
    },
  });
}
