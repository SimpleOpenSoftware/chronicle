// Native Pi tools compute against one virtual buffer. They never receive a vault path.
import readline from "node:readline";

const { createReadTool, createEditTool } = await import(process.argv[2]);
const target = "/__chronicle_text__/note.md";

async function execute(request) {
  let content = request.content;
  const guard = (path) => {
    if (path !== target) throw new Error("Virtual file scope violation");
  };
  const operations = {
    access: async (path) => guard(path),
    readFile: async (path) => { guard(path); return Buffer.from(content, "utf8"); },
    writeFile: async (path, value) => { guard(path); content = value; },
    detectImageMimeType: async (path) => { guard(path); return undefined; },
  };
  if (request.operation === "read") {
    const result = await createReadTool("/__chronicle_text__", { operations }).execute(
      "chronicle-read", { path: target, offset: request.offset, limit: request.limit },
    );
    return { result };
  }
  if (request.operation === "edit") {
    await createEditTool("/__chronicle_text__", { operations }).execute(
      "chronicle-edit", { path: target, edits: request.edits },
    );
    return { content };
  }
  throw new Error("Unknown native text operation");
}

for await (const line of readline.createInterface({ input: process.stdin })) {
  try {
    process.stdout.write(JSON.stringify({ ok: true, ...await execute(JSON.parse(line)) }) + "\n");
  } catch (error) {
    process.stdout.write(JSON.stringify({ ok: false, error: String(error.message ?? error) }) + "\n");
  }
}
