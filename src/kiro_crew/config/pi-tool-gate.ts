type ToolCallEvent = { toolName: string; toolCallId: string };
type ToolCallContext = {
	hasUI: boolean;
	ui: { confirm(title: string, message: string): Promise<boolean> };
};
type ToolCallDecision = { block: true; reason: string } | undefined;
type ExtensionAPI = {
	on(
		event: "tool_call",
		handler: (
			event: ToolCallEvent,
			ctx: ToolCallContext,
		) => Promise<ToolCallDecision>,
	): void;
};

const CORRELATION_PREFIX = "kirocrew-tool-call:";
const BLOCKED_PROXY_TOOLS = new Set(["mcp", "mcpScript"]);

export default function (pi: ExtensionAPI) {
	pi.on("tool_call", async (event, ctx) => {
		if (BLOCKED_PROXY_TOOLS.has(event.toolName)) {
			return {
				block: true,
				reason: "Use a direct MCP tool so KiroCrew can apply per-tool policy.",
			};
		}
		if (!ctx.hasUI) {
			return { block: true, reason: "KiroCrew approval UI is unavailable." };
		}

		const approved = await ctx.ui.confirm(
			`Run ${event.toolName}`,
			`${CORRELATION_PREFIX}${event.toolCallId}`,
		);
		if (!approved) {
			return { block: true, reason: "Blocked by KiroCrew approval policy." };
		}
	});
}
