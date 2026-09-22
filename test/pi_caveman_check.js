// Run without model calls: pi -ne -ns -np -nc --no-tools --no-session
//   -e test/pi_caveman_check.js -p /check-caveman
import assert from "node:assert/strict";
import caveman from "../src/kiro_crew/config/pi-caveman.ts";

export default function (pi) {
  pi.registerCommand("check-caveman", {
    handler: async () => {
      const hooks = new Map();
      const commands = new Map();
      const entries = [];
      caveman({
        on: (name, fn) => hooks.set(name, fn),
        registerCommand: (name, command) => commands.set(name, command),
        appendEntry: (customType, data) => entries.push({ type: "custom", customType, data }),
      });
      const ctx = {
        sessionManager: { getEntries: () => entries },
        ui: { setStatus() {}, notify() {}, theme: { fg: (_, text) => text } },
      };
      await hooks.get("session_start")({}, ctx);
      const messages = [{ role: "user", content: "Explain reference equality", timestamp: 1 }];
      for (const level of ["off", "full", "ultra", "off"]) {
        await commands.get("caveman").handler(level, ctx);
        const beforeCount = entries.length;
        const prompt = await hooks.get("before_agent_start")({ systemPrompt: "base" }, ctx);
        const context = await hooks.get("context")({ messages }, ctx);
        assert.equal(entries.length, beforeCount, "prompt/reminder must not persist state");
        assert.equal(messages.length, 1, "context source must not be mutated");
        if (level === "off") {
          assert.equal(prompt, undefined);
          assert.equal(context, undefined);
        } else {
          assert.ok(prompt.systemPrompt.startsWith("base\n\n"));
          assert.ok(prompt.systemPrompt.includes(level.toUpperCase() + ":"));
          assert.equal(context.messages.length, 2);
          assert.equal(context.messages[0], messages[0]);
          const reminder = context.messages[1];
          assert.equal(reminder.customType, "caveman-style-reminder");
          assert.equal(reminder.display, false);
          assert.ok(reminder.content.includes(level.toUpperCase() + ":"));
          assert.ok(reminder.content.includes("security warnings"));
          assert.ok(reminder.content.includes("required output formats"));
        }
      }
      // Native session state remains authoritative after restarting/resuming.
      for (const level of ["full", "ultra", "off"]) {
        entries.push({ type: "custom", customType: "caveman-level", data: { level } });
        await hooks.get("session_start")({}, ctx);
        const result = await hooks.get("context")({ messages }, ctx);
        assert.equal(Boolean(result), level !== "off");
        if (result) assert.ok(result.messages[1].content.includes(level.toUpperCase() + ":"));
      }
      console.log("PASS: Caveman full/ultra/off, transient reminders, native state restore");
    },
  });
}
