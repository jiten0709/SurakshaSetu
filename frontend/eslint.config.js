import js from "@eslint/js";
import { defineConfig } from "eslint/config";
import reactHooks from "eslint-plugin-react-hooks";
import tseslint from "typescript-eslint";

export default defineConfig(
  // The generated types are never hand-edited (make ui-types).
  { ignores: ["dist", "src/conversation-api.ts"] },
  js.configs.recommended,
  tseslint.configs.recommended,
  reactHooks.configs.flat.recommended,
  // The UI logs nothing: a console line could carry the session token or customer text.
  { rules: { "no-console": "error" } },
  // ...except the console guard and its self-test, which spy on console to enforce that.
  { files: ["src/test-setup.ts", "src/no-leak.test.ts"], rules: { "no-console": "off" } },
);
